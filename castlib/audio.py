"""System audio capture via WASAPI loopback, and microphone enumeration.

Windows offers ffmpeg no native route to system audio: there is no WASAPI
loopback demuxer, and DirectShow exposes only real capture devices. So the
desktop's own output is captured here and piped into ffmpeg as raw PCM.
"""

from __future__ import annotations

import math
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable

import pyaudiowpatch as pyaudio


SAMPLE_WIDTH = 2
FFMPEG_SAMPLE_FORMAT = "s16le"
FULL_SCALE = 32768

# Peaking below this over a whole warning window means the TV is getting
# audio no one will hear over the room.
QUIET_PEAK_DBFS = -30.0

CHUNK_FRAMES = 1024
WRITE_INTERVAL = 0.05

# Dropping the oldest audio past this point keeps a slow consumer from
# turning into ever-growing latency.
MAX_BUFFER_SECONDS = 2.0


@dataclass(frozen=True)
class AudioDevice:
    """A DirectShow capture device, as ffmpeg sees it."""

    label: str
    moniker: str

    def ffmpeg_input(self) -> str:
        return f"audio={self.moniker}"


@dataclass(frozen=True)
class LoopbackFormat:
    rate: int
    channels: int

    @property
    def bytes_per_second(self) -> int:
        return self.rate * self.channels * SAMPLE_WIDTH


@dataclass(frozen=True)
class OutputDevice:
    """A speaker endpoint whose output can be captured back."""

    index: int
    label: str
    rate: int
    channels: int
    is_default: bool = False

    @property
    def format(self) -> LoopbackFormat:
        return LoopbackFormat(rate=self.rate, channels=self.channels)


class AudioUnavailable(RuntimeError):
    """No usable capture device, so the cast has to go out silent."""


def list_microphones(ffmpeg: str = "ffmpeg") -> list[AudioDevice]:
    """Enumerate microphones by asking ffmpeg itself.

    PyAudio's WASAPI names and DirectShow's names are not always the same
    string, and only DirectShow's is valid on an ffmpeg command line. The
    alternative name is preferred: it is unique even when two devices share
    a friendly name.
    """
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_no_window_flag(),
        )
    except (OSError, subprocess.SubprocessError):
        return []

    devices: list[AudioDevice] = []
    pending: str | None = None

    for line in result.stderr.splitlines():
        audio_match = re.search(r'"([^"]+)"\s+\(audio\)', line)

        if audio_match:
            pending = audio_match.group(1)
            continue

        if pending is None:
            continue

        alternative = re.search(r'Alternative name\s+"([^"]+)"', line)

        if alternative:
            devices.append(AudioDevice(pending, alternative.group(1)))
            pending = None

    return devices


def _no_window_flag() -> int:
    """Keep console windows from flashing up when running as a GUI exe."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _clean_label(name: str) -> str:
    return name.replace(" [Loopback]", "")


def list_loopback_devices() -> list[OutputDevice]:
    """Every speaker endpoint that can be captured back.

    Only device metadata is read here. Opening an arbitrary endpoint can
    block indefinitely, so that is left until one is actually chosen.
    """
    audio = pyaudio.PyAudio()

    try:
        try:
            wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            return []

        default_name = ""

        try:
            default = audio.get_device_info_by_index(wasapi["defaultOutputDevice"])
            default_name = _clean_label(str(default["name"]))
        except (OSError, ValueError):
            pass

        devices = []

        for info in audio.get_loopback_device_info_generator():
            label = _clean_label(str(info["name"]))

            devices.append(
                OutputDevice(
                    index=int(info["index"]),
                    label=label,
                    rate=int(info["defaultSampleRate"]),
                    channels=int(info["maxInputChannels"]),
                    is_default=label == default_name,
                )
            )

        return devices
    finally:
        audio.terminate()


def find_loopback_device() -> tuple[int, LoopbackFormat, str]:
    """Resolve the default output device to its loopback counterpart."""
    audio = pyaudio.PyAudio()

    try:
        try:
            wasapi = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError as error:
            raise AudioUnavailable(
                "Windows audio (WASAPI) is unavailable on this machine."
            ) from error

        speakers = audio.get_device_info_by_index(wasapi["defaultOutputDevice"])

        if not speakers.get("isLoopbackDevice"):
            for candidate in audio.get_loopback_device_info_generator():
                if speakers["name"] in candidate["name"]:
                    speakers = candidate
                    break
            else:
                raise AudioUnavailable(
                    f"No loopback capture exists for {speakers['name']!r}."
                )

        fmt = LoopbackFormat(
            rate=int(speakers["defaultSampleRate"]),
            channels=int(speakers["maxInputChannels"]),
        )

        return int(speakers["index"]), fmt, str(speakers["name"])
    finally:
        audio.terminate()


class LoopbackCapture:
    """Pipes the desktop's audio output into a stream at exactly real time.

    WASAPI loopback stops delivering when the endpoint goes idle. Left
    alone that stalls the pipe, ffmpeg's audio clock falls behind the
    video, and the two never recover their sync. So a writer thread paces
    itself against the wall clock and pads any shortfall with silence,
    which makes this stream the reliable master clock for the whole cast.
    """

    def __init__(self, on_error: Callable[[Exception], None] | None = None) -> None:
        self._on_error = on_error

        self._audio: pyaudio.PyAudio | None = None
        self._stream: object = None
        self._writer: threading.Thread | None = None
        self._stopping = threading.Event()

        self._buffer = bytearray()
        self._lock = threading.Lock()

        self._index: int | None = None

        self.format: LoopbackFormat | None = None
        self.device_name: str = ""
        self.frames_written = 0
        self.silence_frames = 0

        # Loopback taps the endpoint after its volume control, so a muted
        # or zeroed device captures perfect silence while its meter still
        # shows activity, and a low slider sends the TV audio that is
        # technically present but far too quiet to hear. Nothing in the
        # stream reveals either, so the level is tracked here.
        self.heard_audio = False
        self.peak = 0

    @property
    def peak_dbfs(self) -> float:
        if self.peak <= 0:
            return float("-inf")

        return 20 * math.log10(min(self.peak, FULL_SCALE) / FULL_SCALE)

    @property
    def is_too_quiet(self) -> bool:
        """Loud enough to exist, too quiet to hear across a room."""
        return self.peak_dbfs < QUIET_PEAK_DBFS

    def prepare(self, device: OutputDevice | None = None) -> LoopbackFormat:
        """Resolve the device up front, since ffmpeg's command line has to
        declare the sample rate and channel count before anything starts."""
        if device is not None:
            self._index = device.index
            self.format = device.format
            self.device_name = device.label
        else:
            index, fmt, name = find_loopback_device()

            self._index = index
            self.format = fmt
            self.device_name = _clean_label(name)

        return self.format

    def start(self, sink: BinaryIO) -> LoopbackFormat:
        if self._index is None:
            self.prepare()

        index = self._index
        fmt = self.format
        name = self.device_name

        self._audio = pyaudio.PyAudio()

        try:
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=fmt.channels,
                rate=fmt.rate,
                input=True,
                input_device_index=index,
                frames_per_buffer=CHUNK_FRAMES,
                stream_callback=self._on_audio,
            )
        except OSError as error:
            self._audio.terminate()
            self._audio = None

            raise AudioUnavailable(f"Could not open {name!r}: {error}") from error

        self._writer = threading.Thread(
            target=self._pump,
            args=(sink, fmt),
            daemon=True,
        )

        self._writer.start()

        return fmt

    def _on_audio(
        self,
        in_data: bytes,
        frame_count: int,
        time_info: object,
        status: int,
    ) -> tuple[None, int]:
        max_bytes = int(MAX_BUFFER_SECONDS * self.format.bytes_per_second)

        samples = memoryview(in_data).cast("h")

        if samples:
            loudest = max(abs(max(samples)), abs(min(samples)))

            if loudest > self.peak:
                self.peak = loudest

            if loudest:
                self.heard_audio = True

        with self._lock:
            self._buffer.extend(in_data)

            if len(self._buffer) > max_bytes:
                del self._buffer[: len(self._buffer) - max_bytes]

        return (None, pyaudio.paContinue)

    def _take(self, wanted: int) -> bytes:
        with self._lock:
            chunk = bytes(self._buffer[:wanted])
            del self._buffer[: len(chunk)]

        return chunk

    def _pump(self, sink: BinaryIO, fmt: LoopbackFormat) -> None:
        frame_size = fmt.channels * SAMPLE_WIDTH
        started = time.monotonic()

        try:
            while not self._stopping.is_set():
                time.sleep(WRITE_INTERVAL)

                due = int((time.monotonic() - started) * fmt.rate) - self.frames_written

                if due <= 0:
                    continue

                chunk = self._take(due * frame_size)
                captured = len(chunk) // frame_size
                missing = due - captured

                if missing > 0:
                    chunk += bytes(missing * frame_size)
                    self.silence_frames += missing

                sink.write(chunk)
                sink.flush()

                self.frames_written += due
        except (BrokenPipeError, OSError, ValueError) as error:
            # ffmpeg going away closes the pipe under us; that is the
            # session's problem to report, not an audio fault.
            if not self._stopping.is_set() and self._on_error is not None:
                self._on_error(error)

    def stop(self) -> None:
        self._stopping.set()

        if self._writer is not None:
            self._writer.join(timeout=2)
            self._writer = None

        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except OSError:
                pass

            self._stream = None

        if self._audio is not None:
            self._audio.terminate()
            self._audio = None
