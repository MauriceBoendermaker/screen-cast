"""Screen capture, encoding and serving the HLS stream."""

from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from castlib.audio import (
    FFMPEG_SAMPLE_FORMAT,
    AudioDevice,
    LoopbackFormat,
    _no_window_flag,
)


FPS = 30
WIDTH = 1920
HEIGHT = 1080

# gdigrab copies the screen through GDI and measured only 17.6 frames a
# second on this machine: ffmpeg then duplicates frames to reach 30, so
# the output claims 30fps while the motion is half that. ddagrab uses
# the Desktop Duplication API on the GPU and delivered a true 30.
DDAGRAB = "ddagrab"
GDIGRAB = "gdigrab"

# Encoder choice is measured, not assumed. With capture moved to the
# GPU, every libx264 preset better than ultrafast still misses real
# time (superfast 0.909x, veryfast 0.888x), and h264_amf scored within
# noise of x264 (SSIM 0.979 vs 0.977), so the hardware encoder buys no
# quality while adding a GPU dependency.
PRESET = "ultrafast"

# Sized for the link, not the encoder. A Chromecast on wireless can sit
# at ~60ms RTT on its own LAN, and TCP throughput is bounded by window
# over round-trip time, so a 6Mbps stream starves a device that pings
# like that. libx264 ultrafast is kept because it is the only preset
# measured to hold real time here: veryfast managed 0.88x, and NVENC
# was slower still on hybrid graphics, where frames must cross to the
# discrete GPU.
BITRATE = "6000k"
BUFSIZE = "12000k"
AUDIO_BITRATE = "128k"

# Named so the picker reads as a choice about the picture rather than a
# number. Bitrate, then how much buffer the encoder may swing into.
QUALITY_PRESETS: dict[str, tuple[str, str]] = {
    "Smooth (3 Mbps)": ("3000k", "6000k"),
    "Balanced (6 Mbps)": ("6000k", "12000k"),
    "Sharp (10 Mbps)": ("10000k", "20000k"),
}

DEFAULT_QUALITY = "Balanced (6 Mbps)"

# A segment cannot be published until it is complete, and a player joins
# a few segments behind the end of the playlist, so segment length is
# what sets the delay. Measured with a real HLS client: 2s segments put
# a viewer ~4s behind live, 1s segments ~2s, and a two-entry playlist
# ~1s with almost no buffer left to absorb a hiccup. Shorter segments
# need more keyframes but cost little on desktop content: 6.36 Mbps at
# 2s versus 6.12 Mbps at 1s.
# Segment length is the only part of this that decides whether a device
# can keep up: a player must fetch each segment within its own duration
# or it stalls, and buffering exactly once a second is the signature of
# one-second segments arriving late. An earlier ladder had two of three
# settings on one-second segments, differing only in playlist length,
# which barely moved the join point — so they were the same fragile
# choice under two names. Each step here doubles the segment instead.
LATENCY_PRESETS: dict[str, tuple[int, int]] = {
    "Low (~2s)": (1, 6),
    "Balanced (~4s)": (2, 8),
    "Smooth (~8s)": (4, 8),
}

# In live HLS the delay IS the buffer: a player joins a couple of
# segments from the end and stays there, so shortening the delay
# shortens the only cushion it has. On a Chromecast that pings at 58ms
# with 117ms spikes, two seconds of cushion was not enough and the
# stream stuttered, while four seconds ran clean. Smooth is therefore
# the default, and the shorter settings are there for better links.
DEFAULT_LATENCY = "Balanced (~4s)"

HLS_TIME, HLS_LIST_SIZE = LATENCY_PRESETS[DEFAULT_LATENCY]

# Segments linger past their turn in the playlist, so a player that
# falls behind re-reads them instead of hitting a 404 and rebuffering.
HLS_DELETE_THRESHOLD = 4

THREAD_QUEUE_SIZE = "1024"
PLAYLIST_NAME = "live.m3u8"


@dataclass
class StreamSettings:
    directory: Path
    capture: str = DDAGRAB
    system_audio: bool = True
    loopback: LoopbackFormat | None = None
    microphone: AudioDevice | None = None
    fps: int = FPS
    width: int = WIDTH
    height: int = HEIGHT
    bitrate: str = BITRATE
    bufsize: str = BUFSIZE
    audio_bitrate: str = AUDIO_BITRATE
    hls_time: int = HLS_TIME
    hls_list_size: int = HLS_LIST_SIZE
    hls_delete_threshold: int = HLS_DELETE_THRESHOLD

    def apply_quality(self, name: str) -> None:
        self.bitrate, self.bufsize = QUALITY_PRESETS[resolve_quality(name)]

    def apply_latency(self, name: str) -> None:
        self.hls_time, self.hls_list_size = LATENCY_PRESETS[resolve_latency(name)]

    @property
    def has_system_audio(self) -> bool:
        return self.system_audio and self.loopback is not None

    @property
    def has_audio(self) -> bool:
        return self.has_system_audio or self.microphone is not None


class CastHttpHandler(SimpleHTTPRequestHandler):
    requests_served = threading.Event()

    # SimpleHTTPRequestHandler defaults to HTTP/1.0, which closes the
    # connection after every response. A Chromecast then opens a fresh
    # TCP connection per segment, and on a link with tens of
    # milliseconds of round trip each one restarts slow start at about
    # 14KB. The window never grows, so the stream starves no matter how
    # low the bitrate goes. HTTP/1.1 keeps the connection warm.
    protocol_version = "HTTP/1.1"

    # Persistent connections hold a thread each, so do not let a player
    # that wandered off hold one forever.
    timeout = 30

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def send_response(self, code: int, message: str | None = None) -> None:
        CastHttpHandler.requests_served.set()
        super().send_response(code, message)

    def log_message(self, format: str, *args: object) -> None:
        return


class CastHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    # Persistent connections mean a client can vanish mid-segment at any
    # time. That is ordinary for a player that rebuffers or seeks, so it
    # should not print a stack trace per occurrence.
    def handle_error(self, request: object, client_address: object) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return

        super().handle_error(request, client_address)


def start_http_server(directory: Path, port: int) -> ThreadingHTTPServer:
    CastHttpHandler.requests_served.clear()

    handler = partial(CastHttpHandler, directory=str(directory))

    try:
        server = CastHttpServer(("0.0.0.0", port), handler)
    except OSError as error:
        raise RuntimeError(
            f"Could not listen on port {port}: {error}. "
            "Use another port."
        ) from error

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server


def build_ffmpeg_command(settings: StreamSettings) -> list[str]:
    """Assemble the ffmpeg invocation for the requested capture.

    Kept pure so the argument list can be asserted on without running
    anything.
    """
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-thread_queue_size",
        THREAD_QUEUE_SIZE,
    ]

    if settings.capture == DDAGRAB:
        command += [
            "-f",
            "lavfi",
            "-i",
            f"ddagrab=0:framerate={settings.fps}:draw_mouse=1",
        ]
    else:
        command += [
            "-f",
            "gdigrab",
            "-framerate",
            str(settings.fps),
            "-draw_mouse",
            "1",
            "-i",
            "desktop",
        ]

    audio_inputs: list[str] = []

    if settings.has_system_audio:
        loopback = settings.loopback

        command += [
            "-thread_queue_size",
            THREAD_QUEUE_SIZE,
            "-f",
            FFMPEG_SAMPLE_FORMAT,
            "-ar",
            str(loopback.rate),
            "-ac",
            str(loopback.channels),
            "-i",
            "pipe:0",
        ]

        audio_inputs.append(f"{len(audio_inputs) + 1}:a")

    if settings.microphone is not None:
        command += [
            "-thread_queue_size",
            THREAD_QUEUE_SIZE,
            "-f",
            "dshow",
            "-i",
            settings.microphone.ffmpeg_input(),
        ]

        audio_inputs.append(f"{len(audio_inputs) + 1}:a")

    # ddagrab hands back frames living on the GPU, so they have to come
    # down to system memory before libx264 can see them.
    source = (
        "[0:v]hwdownload,format=bgra," if settings.capture == DDAGRAB else "[0:v]"
    )

    scale = (
        f"{source}scale={settings.width}:{settings.height}"
        ":force_original_aspect_ratio=decrease,"
        f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2,"
        "format=yuv420p[v]"
    )

    chains = [scale]

    if len(audio_inputs) == 1:
        chains.append(f"[{audio_inputs[0]}]aresample=async=1[a]")
    elif len(audio_inputs) == 2:
        # The mic runs on its own clock and will drift against the
        # loopback stream; resampling it keeps the mix aligned.
        chains.append(f"[{audio_inputs[1]}]aresample=async=1[mic]")
        chains.append(
            f"[{audio_inputs[0]}][mic]"
            "amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
        )

    command += ["-filter_complex", ";".join(chains), "-map", "[v]"]

    if audio_inputs:
        command += ["-map", "[a]"]

    command += [
        "-c:v",
        "libx264",
        "-preset",
        PRESET,
        "-tune",
        "zerolatency",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(settings.fps),
        "-g",
        str(settings.fps * settings.hls_time),
        "-keyint_min",
        str(settings.fps),
        "-sc_threshold",
        "0",
        "-b:v",
        settings.bitrate,
        "-maxrate",
        settings.bitrate,
        "-bufsize",
        settings.bufsize,
    ]

    if audio_inputs:
        command += [
            "-c:a",
            "aac",
            "-b:a",
            settings.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
    else:
        command.append("-an")

    command += [
        "-f",
        "hls",
        "-hls_time",
        str(settings.hls_time),
        "-hls_list_size",
        str(settings.hls_list_size),
        "-hls_delete_threshold",
        str(settings.hls_delete_threshold),
        "-hls_flags",
        "delete_segments+omit_endlist+independent_segments",
        "-hls_segment_filename",
        str(settings.directory / "segment_%05d.ts"),
        str(settings.directory / PLAYLIST_NAME),
    ]

    return command


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# Held for the life of the process: closing the last handle is what
# triggers the kill, so this must not be garbage collected.
_job_handle: int | None = None

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_EXTENDED_LIMIT_INFORMATION = 9


def _containment_job() -> int | None:
    """A job object that kills whatever it holds when this process dies.

    Without it, killing the app strands ffmpeg still capturing the
    screen, with no window and no obvious way to notice.
    """
    global _job_handle

    if _job_handle is not None:
        return _job_handle

    try:
        kernel32 = ctypes.windll.kernel32

        handle = kernel32.CreateJobObjectW(None, None)

        if not handle:
            return None

        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        if not kernel32.SetInformationJobObject(
            handle,
            JOB_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            kernel32.CloseHandle(handle)
            return None

        _job_handle = handle

        return handle
    except (AttributeError, OSError):
        return None


def _contain(process: subprocess.Popen) -> None:
    job = _containment_job()

    if job is None:
        return

    try:
        ctypes.windll.kernel32.AssignProcessToJobObject(job, int(process._handle))
    except (AttributeError, OSError, ValueError):
        pass


def start_ffmpeg(settings: StreamSettings) -> subprocess.Popen:
    command = build_ffmpeg_command(settings)

    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if settings.has_system_audio else subprocess.DEVNULL,
        creationflags=_no_window_flag(),
    )

    _contain(process)

    return process


# A player joining a live playlist starts near its end, and a stream
# produced in real time can never get ahead of a player sitting at the
# live edge: it drains whatever exists, starves, and shows a spinner
# every few seconds forever. Handing over a playlist that already holds
# several segments is what gives it somewhere behind live to sit.
PREROLL_SEGMENTS = 4


def listed_segments(directory: Path) -> int:
    try:
        playlist = (directory / PLAYLIST_NAME).read_text()
    except OSError:
        return 0

    return sum(1 for line in playlist.splitlines() if line.strip().endswith(".ts"))


def wait_for_stream(
    process: subprocess.Popen,
    directory: Path,
    segments: int = 1,
    timeout: float | None = None,
) -> None:
    """Block until the playlist holds enough segments to hand over."""
    if timeout is None:
        timeout = 20 + segments * 4

    deadline = time.time() + timeout

    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"FFmpeg stopped unexpectedly with exit code {process.returncode}."
            )

        if listed_segments(directory) >= segments:
            return

        time.sleep(0.25)

    raise RuntimeError("Timed out waiting for FFmpeg to produce a stream.")


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def resolve_latency(name: str) -> str:
    """Fall back to the default, never to whatever happens to be first.

    A stale or renamed setting resolving to the head of the list would
    silently pick the most aggressive option, which is the last thing a
    struggling link needs.
    """
    return name if name in LATENCY_PRESETS else DEFAULT_LATENCY


def resolve_quality(name: str) -> str:
    return name if name in QUALITY_PRESETS else DEFAULT_QUALITY


_capture_backend: str | None = None


def detect_capture_backend(ffmpeg: str = "ffmpeg") -> str:
    """Prefer the GPU capture, but only if it actually runs here.

    Desktop Duplication is unavailable over some remote sessions and on
    machines without a D3D11 device, and failing at cast time would be
    far worse than falling back to the slower grabber.
    """
    global _capture_backend

    if _capture_backend is not None:
        return _capture_backend

    try:
        result = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "ddagrab=0:framerate=30",
                "-t", "0.3", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_no_window_flag(),
        )

        _capture_backend = DDAGRAB if result.returncode == 0 else GDIGRAB
    except (OSError, subprocess.SubprocessError):
        _capture_backend = GDIGRAB

    return _capture_backend


TEMP_PREFIX = "cast-screen-"
STALE_AFTER = 3600


def clean_stale_directories(older_than: float = STALE_AFTER) -> int:
    """Discard segment directories left behind by a hard kill.

    Normal teardown removes its own directory, but a killed process
    cannot, and each one holds video segments. Only directories older
    than an hour are touched, so a concurrently running cast is safe.
    """
    removed = 0
    cutoff = time.time() - older_than

    try:
        candidates = Path(tempfile.gettempdir()).glob(f"{TEMP_PREFIX}*")
    except OSError:
        return 0

    for directory in candidates:
        try:
            if not directory.is_dir() or directory.stat().st_mtime > cutoff:
                continue

            shutil.rmtree(directory, ignore_errors=True)

            removed += 1
        except OSError:
            continue

    return removed
