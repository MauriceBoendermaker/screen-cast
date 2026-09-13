"""The audio pump's real-time pacing is what keeps A/V in sync."""

from __future__ import annotations

import io
import struct
import threading
import time
import unittest

from castlib.audio import SAMPLE_WIDTH, AudioDevice, LoopbackCapture, LoopbackFormat


FORMAT = LoopbackFormat(rate=48000, channels=2)
RUN_SECONDS = 0.6

# Thread scheduling makes exact byte counts impossible; the property
# under test is "tracks real time", not "to the sample".
TOLERANCE = 0.25


class ThreadSafeSink(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.lock = threading.Lock()

    def write(self, data: bytes) -> int:  # type: ignore[override]
        with self.lock:
            return super().write(data)

    def snapshot(self) -> bytes:
        with self.lock:
            return self.getvalue()


def run_pump(capture: LoopbackCapture, seconds: float) -> bytes:
    sink = ThreadSafeSink()

    pump = threading.Thread(target=capture._pump, args=(sink, FORMAT), daemon=True)
    pump.start()

    time.sleep(seconds)

    capture._stopping.set()
    pump.join(timeout=2)

    return sink.snapshot()


class SilentSource(unittest.TestCase):
    """WASAPI loopback delivers nothing while the endpoint is idle.

    If the pump simply forwarded what it received, the pipe would stall
    and audio would fall permanently behind the video.
    """

    def setUp(self) -> None:
        self.capture = LoopbackCapture()
        self.capture.format = FORMAT

        self.written = run_pump(self.capture, RUN_SECONDS)

    def test_keeps_writing_anyway(self) -> None:
        self.assertGreater(len(self.written), 0)

    def test_writes_at_real_time_rate(self) -> None:
        expected = RUN_SECONDS * FORMAT.bytes_per_second
        ratio = len(self.written) / expected

        self.assertAlmostEqual(ratio, 1.0, delta=TOLERANCE)

    def test_what_it_writes_is_silence(self) -> None:
        self.assertEqual(self.written.strip(b"\x00"), b"")

    def test_counts_the_padding(self) -> None:
        self.assertGreater(self.capture.silence_frames, 0)


class CapturedSource(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = LoopbackCapture()
        self.capture.format = FORMAT

        # Half a second of a non-zero signal, already captured.
        frames = int(FORMAT.rate * 0.5)
        self.captured = b"\x11\x22" * FORMAT.channels * frames

        self.capture._buffer.extend(self.captured)

        self.written = run_pump(self.capture, RUN_SECONDS)

    def test_forwards_the_captured_audio(self) -> None:
        self.assertIn(b"\x11\x22\x11\x22", self.written)

    def test_still_tracks_real_time(self) -> None:
        expected = RUN_SECONDS * FORMAT.bytes_per_second
        ratio = len(self.written) / expected

        self.assertAlmostEqual(ratio, 1.0, delta=TOLERANCE)

    def test_drains_the_buffer(self) -> None:
        self.assertLess(len(self.capture._buffer), len(self.captured))


class BufferBounds(unittest.TestCase):
    def test_backlog_is_capped(self) -> None:
        """A consumer that stops reading must not grow latency forever."""
        capture = LoopbackCapture()
        capture.format = FORMAT

        one_second = b"\x01\x02" * FORMAT.channels * FORMAT.rate

        for _ in range(6):
            capture._on_audio(one_second, FORMAT.rate, None, 0)

        self.assertLessEqual(
            len(capture._buffer),
            2.0 * FORMAT.bytes_per_second,
        )


class LevelReporting(unittest.TestCase):
    """Loopback is post-volume, so a valid track can still be unhearable."""

    def capture_with(self, amplitude: int) -> LoopbackCapture:
        capture = LoopbackCapture()
        capture.format = FORMAT

        payload = struct.pack("<h", amplitude) * (FORMAT.channels * 512)
        capture._on_audio(payload, 512, None, 0)

        return capture

    def test_silence_is_not_heard(self) -> None:
        capture = self.capture_with(0)

        self.assertFalse(capture.heard_audio)
        self.assertEqual(capture.peak_dbfs, float("-inf"))

    def test_full_scale_is_zero_dbfs(self) -> None:
        capture = self.capture_with(32767)

        self.assertTrue(capture.heard_audio)
        self.assertAlmostEqual(capture.peak_dbfs, 0.0, delta=0.01)
        self.assertFalse(capture.is_too_quiet)

    def test_quiet_audio_is_flagged(self) -> None:
        # About -40 dBFS: present, but lost across a room.
        capture = self.capture_with(328)

        self.assertTrue(capture.heard_audio)
        self.assertTrue(capture.is_too_quiet)

    def test_healthy_level_is_not_flagged(self) -> None:
        # About -12 dBFS.
        capture = self.capture_with(8230)

        self.assertFalse(capture.is_too_quiet)

    def test_peak_only_rises(self) -> None:
        capture = self.capture_with(20000)
        loud = capture.peak

        capture._on_audio(struct.pack("<h", 5) * (FORMAT.channels * 512), 512, None, 0)

        self.assertEqual(capture.peak, loud)


class Formats(unittest.TestCase):
    def test_bytes_per_second(self) -> None:
        self.assertEqual(
            FORMAT.bytes_per_second,
            48000 * 2 * SAMPLE_WIDTH,
        )

    def test_device_renders_an_ffmpeg_input(self) -> None:
        device = AudioDevice("Microphone (Test)", "@device_cm_{GUID}")

        self.assertEqual(device.ffmpeg_input(), "audio=@device_cm_{GUID}")


if __name__ == "__main__":
    unittest.main()
