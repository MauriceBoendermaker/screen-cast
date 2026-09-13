"""Orchestration: one cast, from discovery to teardown.

Emits events rather than printing, so a terminal and a window can both
drive it.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

import pychromecast
from pychromecast.config import APP_MEDIA_RECEIVER

from castlib import discovery, streaming
from castlib.audio import (
    AudioDevice,
    AudioUnavailable,
    LoopbackCapture,
    OutputDevice,
)
from castlib.streaming import PLAYLIST_NAME, StreamSettings


SESSION_TIMEOUT = 30
PLAYBACK_TIMEOUT = 20
CONNECT_TIMEOUT = 15
APP_RELEASE_TIMEOUT = 10

# How often a long wait looks up to see whether it has been cancelled.
# Waiting on the whole timeout in one call would make Stop and Close
# appear frozen for as long as the device takes to answer.
CANCEL_POLL = 0.25

# How long to let a silent capture run before saying so. Long enough not
# to nag during a quiet passage, short enough to catch a muted endpoint
# before the whole film has played out.
SILENCE_WARNING_AFTER = 15

# Two stalls can be a passing hiccup; four is the link telling you the
# settings are beyond it.
REBUFFER_LIMIT = 4


class SessionState(str, Enum):
    IDLE = "idle"
    STARTING = "starting"
    CASTING = "casting"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True)
class SessionEvent:
    state: SessionState
    message: str


@dataclass(frozen=True)
class CastStats:
    """How the cast is going, as of the last look around.

    Everything here is already known to something: ffmpeg reports the
    encode figures, the playlist weighs its own segments, the device
    reports its own stalls, and the loopback capture tracks its level
    for the silence warning. This is that scattered knowledge gathered
    into one snapshot for anyone who wants to watch it live. The
    command line ignores it.
    """

    elapsed: float
    fps: float
    speed: float
    bitrate_kbps: float
    dropped: int
    duplicated: int
    rebuffers: int

    # None when system audio is off, which is not the same as silence.
    audio_peak_dbfs: float | None


@dataclass
class CastOptions:
    host: str | None = None
    name: str | None = None
    port: int = 8765
    system_audio: bool = True
    quality: str = streaming.DEFAULT_QUALITY
    latency: str = streaming.DEFAULT_LATENCY
    audio_device: OutputDevice | None = None
    microphone: AudioDevice | None = None
    device: pychromecast.Chromecast | None = None


class CastSession:
    """Runs a cast on a background thread.

    start() and stop() are both idempotent and safe to call from a UI
    event handler; neither blocks on the cast itself.
    """

    def __init__(self, on_event: Callable[[SessionEvent], None]) -> None:
        self._on_event = on_event
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()

        self._state = SessionState.IDLE
        self._stats: CastStats | None = None

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._state in (SessionState.STARTING, SessionState.CASTING)

    @property
    def stats(self) -> CastStats | None:
        """The latest snapshot, or None until a cast is actually running.

        Read from whatever thread likes. The monitor swaps in a whole
        frozen object rather than editing one in place, so a reader can
        never catch it half updated and there is nothing to lock.
        """
        return self._stats

    def _emit(self, state: SessionState, message: str) -> None:
        self._state = state
        self._on_event(SessionEvent(state, message))

    def start(self, options: CastOptions) -> None:
        with self._lock:
            if self.is_running:
                return

            self._stopping.clear()
            self._state = SessionState.STARTING
            self._stats = None

            self._worker = threading.Thread(
                target=self._run,
                args=(options,),
                daemon=True,
            )

            self._worker.start()

    def stop(self, timeout: float = 12) -> None:
        self._stopping.set()

        worker = self._worker

        if worker is not None and worker.is_alive():
            worker.join(timeout=timeout)

    def _run(self, options: CastOptions) -> None:
        directory = Path(tempfile.mkdtemp(prefix="cast-screen-"))

        cast = None
        browser = None
        owns_browser = False
        server = None
        ffmpeg = None
        capture = None
        progress = None

        try:
            if not streaming.ffmpeg_available():
                raise RuntimeError(
                    "FFmpeg was not found. Install it and make sure it is on PATH."
                )

            cast = options.device

            if cast is None:
                self._emit(SessionState.STARTING, "Searching for Cast devices...")

                devices, browser = discovery.discover_devices(
                    options.host,
                    on_status=lambda message: self._emit(
                        SessionState.STARTING, message
                    ),
                )

                owns_browser = True

                if options.name is not None:
                    cast = discovery.select_by_name(devices, options.name)
                elif len(devices) == 1:
                    cast = devices[0]
                else:
                    names = ", ".join(discovery.device_label(d) for d in devices)

                    raise RuntimeError(
                        f"Several devices found ({names}). Choose one with --name."
                    )

            name = discovery.device_label(cast)

            self._emit(SessionState.STARTING, f"Connecting to {name}...")

            cast.wait(timeout=CONNECT_TIMEOUT)

            if self._stopping.is_set():
                return

            capture, settings = self._prepare_audio(options, directory)

            server = streaming.start_http_server(directory, options.port)

            self._emit(SessionState.STARTING, "Starting desktop capture...")

            ffmpeg = streaming.start_ffmpeg(settings)

            # Started here rather than when something wants to watch,
            # and started even for the command line, which never looks:
            # the pipe holds about eleven seconds of blocks and silently
            # drops everything after that, so a reader that arrives late
            # inherits numbers that stopped moving before it existed.
            progress = streaming.FfmpegProgress()
            progress.start(ffmpeg.stdout)

            if capture is not None:
                capture.start(ffmpeg.stdin)

            preroll = min(streaming.PREROLL_SEGMENTS, settings.hls_list_size)

            self._emit(
                SessionState.STARTING,
                f"Building a {preroll * settings.hls_time}s head start "
                "so the device has something to buffer...",
            )

            streaming.wait_for_stream(ffmpeg, directory, segments=preroll)

            if self._stopping.is_set():
                return

            local_ip = discovery.get_local_ip()
            stream_url = f"http://{local_ip}:{options.port}/{PLAYLIST_NAME}"

            self._emit(SessionState.STARTING, f"Casting to {name}...")

            if not self._start_playback(cast, stream_url):
                blocking = self.running_app(cast)
                culprit = (
                    f" It is still running {blocking}." if blocking else ""
                )

                raise RuntimeError(
                    f"{name} never started a media session.{culprit} "
                    "Try stopping playback on the device itself, then cast "
                    "again."
                )

            self._await_playback(cast, options.port, name)
            self._monitor(ffmpeg, capture, progress, directory, cast, name)
        except Exception as error:
            if not self._stopping.is_set():
                self._emit(SessionState.FAILED, str(error))
        finally:
            self._teardown(
                cast=cast,
                browser=browser if owns_browser else None,
                disconnect=options.device is None,
                server=server,
                ffmpeg=ffmpeg,
                capture=capture,
                progress=progress,
                directory=directory,
            )

    def _prepare_audio(
        self,
        options: CastOptions,
        directory: Path,
    ) -> tuple[LoopbackCapture | None, StreamSettings]:
        capture = None
        loopback = None

        if options.system_audio:
            try:
                capture = LoopbackCapture(on_error=self._on_capture_error)
                loopback = capture.prepare(options.audio_device)

                self._emit(
                    SessionState.STARTING,
                    f"Capturing audio from {capture.device_name}.",
                )
            except AudioUnavailable as error:
                capture = None
                loopback = None

                self._emit(
                    SessionState.STARTING,
                    f"{error} Casting without system audio.",
                )

        settings = StreamSettings(
            directory=directory,
            capture=streaming.detect_capture_backend(),
            system_audio=capture is not None,
            loopback=loopback,
            microphone=options.microphone,
        )

        settings.apply_quality(options.quality)
        settings.apply_latency(options.latency)

        return capture, settings

    def _on_capture_error(self, error: Exception) -> None:
        if not self._stopping.is_set():
            self._emit(SessionState.FAILED, f"Audio capture stopped: {error}")

    @staticmethod
    def running_app(cast: pychromecast.Chromecast) -> str:
        try:
            status = cast.status
            return (status.display_name if status else "") or ""
        except Exception:
            return ""

    def _release_device(self, cast: pychromecast.Chromecast) -> None:
        """Return the device to idle before claiming it.

        Two separate problems, one cure. A busy Chromecast asked to play
        media tears the running app down first and often loses the launch
        that follows, which looks like a flat refusal. And a *media
        receiver* left over from an earlier cast accepts the new stream
        into a spent session, where it stalls every couple of seconds —
        which is why a first cast played perfectly and every one after it
        stuttered. Quitting whatever is there, receiver included, and
        waiting for the device to let go avoids both.
        """
        try:
            running = cast.app_id
        except Exception:
            return

        if not running:
            return

        name = self.running_app(cast) or running

        if running != APP_MEDIA_RECEIVER:
            self._emit(SessionState.STARTING, f"Closing {name} on the device...")
        else:
            self._emit(SessionState.STARTING, "Clearing the previous session...")

        try:
            cast.quit_app()
        except Exception:
            return

        deadline = time.time() + APP_RELEASE_TIMEOUT

        while time.time() < deadline and not self._stopping.is_set():
            try:
                if not cast.app_id:
                    return
            except Exception:
                return

            time.sleep(CANCEL_POLL)

    def _wait_for_session(self, controller: object, timeout: float) -> bool:
        """Wait for a media session, but stay cancellable while doing it."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._stopping.is_set():
                return False

            if controller.session_active_event.wait(timeout=CANCEL_POLL):
                return True

        return False

    def _start_playback(
        self,
        cast: pychromecast.Chromecast,
        stream_url: str,
        attempts: int = 3,
    ) -> bool:
        controller = cast.media_controller

        for attempt in range(1, attempts + 1):
            self._release_device(cast)

            if self._stopping.is_set():
                return False

            controller.play_media(
                stream_url,
                "application/x-mpegURL",
                title="Desktop",
                stream_type="LIVE",
                autoplay=True,
            )

            if self._wait_for_session(controller, SESSION_TIMEOUT):
                return True

            if self._stopping.is_set():
                return False

            if attempt < attempts:
                self._emit(
                    SessionState.STARTING,
                    "The device did not accept the stream. Retrying...",
                )

                time.sleep(2)

        return False

    def _await_playback(
        self,
        cast: pychromecast.Chromecast,
        port: int,
        name: str,
    ) -> None:
        deadline = time.time() + PLAYBACK_TIMEOUT

        while time.time() < deadline and not self._stopping.is_set():
            if cast.media_controller.status.player_state in ("PLAYING", "BUFFERING"):
                self._emit(SessionState.CASTING, f"Casting to {name}.")
                return

            time.sleep(0.5)

        if streaming.CastHttpHandler.requests_served.is_set():
            self._emit(
                SessionState.CASTING,
                "The device fetched the stream but has not started playing.",
            )

            return

        self._emit(
            SessionState.FAILED,
            f"The device never requested the stream. Windows Firewall is "
            f"probably blocking inbound TCP port {port} for this app.",
        )

    def _monitor(
        self,
        ffmpeg: subprocess.Popen,
        capture: LoopbackCapture | None,
        progress: streaming.FfmpegProgress,
        directory: Path,
        cast: pychromecast.Chromecast,
        name: str,
    ) -> None:
        started = time.monotonic()
        warned = False

        rebuffers = 0
        was_buffering = False
        rebuffer_warned = False

        while not self._stopping.is_set():
            # The device reports its own stalls, so say what is wrong
            # rather than leaving someone to describe the symptom.
            try:
                state = cast.media_controller.status.player_state
            except Exception:
                state = None

            buffering = state == "BUFFERING"

            if buffering and not was_buffering:
                rebuffers += 1

            was_buffering = buffering

            if rebuffers >= REBUFFER_LIMIT and not rebuffer_warned:
                rebuffer_warned = True

                self._emit(
                    SessionState.CASTING,
                    f"{name} keeps rebuffering ({rebuffers} times). Stop, then "
                    "choose a longer Delay or a lower Quality.",
                )

            self._stats = self._snapshot(
                elapsed=time.monotonic() - started,
                progress=progress.latest,
                bitrate_kbps=streaming.measured_bitrate_kbps(directory),
                capture=capture,
                rebuffers=rebuffers,
            )

            if ffmpeg.poll() is not None:
                raise RuntimeError(
                    f"FFmpeg stopped unexpectedly with exit code "
                    f"{ffmpeg.returncode}."
                )

            if (
                capture is not None
                and not warned
                and time.monotonic() - started > SILENCE_WARNING_AFTER
            ):
                complaint = self._audio_complaint(capture, name)

                if complaint is not None:
                    warned = True

                    self._emit(SessionState.CASTING, complaint)

            time.sleep(0.5)

    @staticmethod
    def _snapshot(
        elapsed: float,
        progress: streaming.EncodeProgress,
        bitrate_kbps: float,
        capture: LoopbackCapture | None,
        rebuffers: int,
    ) -> CastStats:
        return CastStats(
            elapsed=elapsed,
            fps=progress.fps,
            speed=progress.speed,
            bitrate_kbps=bitrate_kbps,
            dropped=progress.dropped,
            duplicated=progress.duplicated,
            rebuffers=rebuffers,
            audio_peak_dbfs=capture.peak_dbfs if capture is not None else None,
        )

    @staticmethod
    def _audio_complaint(capture: LoopbackCapture, name: str) -> str | None:
        """Say what is wrong with the sound, since the stream cannot.

        Loopback sits downstream of the endpoint's volume control, so a
        muted or quiet device produces a technically valid audio track
        that nobody can hear.
        """
        if not capture.heard_audio:
            return (
                f"Casting to {name}, but {capture.device_name} is silent. "
                "Check it is not muted, or pick another audio source."
            )

        if capture.is_too_quiet:
            return (
                f"Casting to {name}, but the sound is very quiet "
                f"({capture.peak_dbfs:.0f} dB). Turn Windows volume up and "
                "use the TV's volume instead."
            )

        return None

    def _teardown(
        self,
        cast: pychromecast.Chromecast | None,
        browser: object,
        server: object,
        ffmpeg: subprocess.Popen | None,
        capture: LoopbackCapture | None,
        directory: Path,
        progress: streaming.FfmpegProgress | None = None,
        disconnect: bool = True,
    ) -> None:
        if self._state is not SessionState.FAILED:
            self._emit(SessionState.STOPPING, "Stopping cast...")

        # Audio first: it writes into ffmpeg's stdin, and closing that
        # pipe out from under it would look like a capture failure.
        if capture is not None:
            capture.stop()

        if cast is not None:
            try:
                cast.media_controller.stop()
            except Exception:
                pass

            # Leaving the receiver running poisons the next cast: it
            # accepts the new stream into a spent session that stalls
            # every couple of seconds. Hand the device back idle.
            try:
                cast.quit_app()
            except Exception:
                pass

            # Only a device this session discovered may be disconnected.
            # A caller that handed one in keeps reusing it, and
            # pychromecast cannot restart a socket client once it has
            # been shut down — the next cast dies with "threads can only
            # be started once".
            if disconnect:
                try:
                    cast.disconnect()
                except Exception:
                    pass

        if ffmpeg is not None:
            if ffmpeg.stdin is not None and not ffmpeg.stdin.closed:
                try:
                    ffmpeg.stdin.close()
                except OSError:
                    pass

            if ffmpeg.poll() is None:
                ffmpeg.terminate()

                try:
                    ffmpeg.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ffmpeg.kill()

        # After ffmpeg, not before: the reader is sitting on a read that
        # only ends when the process exits and closes the pipe.
        if progress is not None:
            progress.stop()

        if server is not None:
            server.shutdown()
            server.server_close()

        if browser is not None:
            try:
                browser.stop_discovery()
            except Exception:
                pass

        shutil.rmtree(directory, ignore_errors=True)

        if self._state is not SessionState.FAILED:
            self._emit(SessionState.IDLE, "Idle")
