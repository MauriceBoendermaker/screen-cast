"""Cancellation and device hand-over, the two things that strand a user."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

from pychromecast.config import APP_MEDIA_RECEIVER

from castlib.session import CastSession, SessionState
from castlib.streaming import EncodeProgress


class FakeCast:
    """Stands in for a Chromecast that is busy with something else."""

    def __init__(self, app_id: str, display_name: str = "") -> None:
        self.app_id = app_id
        self.status = SimpleNamespace(display_name=display_name)
        self.quit_calls = 0
        self.releases_after = 0

    def quit_app(self) -> None:
        self.quit_calls += 1

        if self.releases_after == 0:
            self.app_id = ""


def silent_session() -> CastSession:
    return CastSession(on_event=lambda event: None)


class CancellableWaiting(unittest.TestCase):
    """Stop and Close must not wait out the device's own timeout."""

    def test_wait_returns_at_once_when_stopping(self) -> None:
        session = silent_session()
        session._stopping.set()

        controller = SimpleNamespace(session_active_event=threading.Event())

        started = time.monotonic()
        result = session._wait_for_session(controller, timeout=30)
        elapsed = time.monotonic() - started

        self.assertFalse(result)
        self.assertLess(elapsed, 1.0, "waiting ignored the stop signal")

    def test_wait_reacts_to_a_stop_raised_midway(self) -> None:
        session = silent_session()
        controller = SimpleNamespace(session_active_event=threading.Event())

        threading.Timer(0.3, session._stopping.set).start()

        started = time.monotonic()
        result = session._wait_for_session(controller, timeout=30)
        elapsed = time.monotonic() - started

        self.assertFalse(result)
        self.assertLess(elapsed, 2.0)

    def test_wait_succeeds_when_the_session_activates(self) -> None:
        session = silent_session()
        event = threading.Event()
        controller = SimpleNamespace(session_active_event=event)

        threading.Timer(0.2, event.set).start()

        self.assertTrue(session._wait_for_session(controller, timeout=10))

    def test_wait_gives_up_at_the_timeout(self) -> None:
        session = silent_session()
        controller = SimpleNamespace(session_active_event=threading.Event())

        started = time.monotonic()
        result = session._wait_for_session(controller, timeout=0.6)

        self.assertFalse(result)
        self.assertLess(time.monotonic() - started, 2.0)


class TakingOverTheDevice(unittest.TestCase):
    """A busy Chromecast drops the launch that play_media triggers."""

    def test_a_foreign_app_is_quit(self) -> None:
        session = silent_session()
        cast = FakeCast("2C6A6E3D", "YouTube")

        session._release_device(cast)

        self.assertEqual(cast.quit_calls, 1)

    def test_a_leftover_receiver_is_also_quit(self) -> None:
        """A spent receiver accepts the stream and then stalls.

        That is why a first cast played perfectly and every later one
        stuttered: the session was never handed back.
        """
        session = silent_session()
        cast = FakeCast(APP_MEDIA_RECEIVER, "Default Media Receiver")

        session._release_device(cast)

        self.assertEqual(cast.quit_calls, 1)

    def test_an_idle_device_is_left_alone(self) -> None:
        session = silent_session()
        cast = FakeCast("", "")

        session._release_device(cast)

        self.assertEqual(cast.quit_calls, 0)

    def test_release_stops_when_cancelled(self) -> None:
        session = silent_session()

        cast = FakeCast("2C6A6E3D", "YouTube")
        cast.releases_after = 999  # never lets go

        session._stopping.set()

        started = time.monotonic()
        session._release_device(cast)

        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_stuck_app_does_not_hang_forever(self) -> None:
        session = silent_session()

        cast = FakeCast("2C6A6E3D", "YouTube")
        cast.releases_after = 999

        # Shorten the wait rather than sit through the real one.
        import castlib.session as module

        original = module.APP_RELEASE_TIMEOUT
        module.APP_RELEASE_TIMEOUT = 0.5

        try:
            started = time.monotonic()
            session._release_device(cast)

            self.assertLess(time.monotonic() - started, 3.0)
        finally:
            module.APP_RELEASE_TIMEOUT = original

    def test_teardown_hands_the_device_back_idle(self) -> None:
        """The next cast depends on it: a receiver left running makes
        every later session stall."""
        session = silent_session()
        cast = FakeCast(APP_MEDIA_RECEIVER, "Default Media Receiver")

        stopped = []
        cast.media_controller = SimpleNamespace(stop=lambda: stopped.append(True))
        cast.disconnect = lambda: None

        session._teardown(
            cast=cast,
            browser=None,
            server=None,
            ffmpeg=None,
            capture=None,
            directory=Path(tempfile.mkdtemp(prefix="teardown-")),
        )

        self.assertTrue(stopped, "media was not stopped")
        self.assertEqual(cast.quit_calls, 1, "receiver was left running")

    def test_a_borrowed_device_is_not_disconnected(self) -> None:
        """pychromecast cannot restart a socket client, so disconnecting
        a device the caller reuses kills every later cast with
        "threads can only be started once"."""
        session = silent_session()
        cast = FakeCast(APP_MEDIA_RECEIVER)

        disconnects = []
        cast.media_controller = SimpleNamespace(stop=lambda: None)
        cast.disconnect = lambda: disconnects.append(True)

        session._teardown(
            cast=cast,
            browser=None,
            server=None,
            ffmpeg=None,
            capture=None,
            directory=Path(tempfile.mkdtemp(prefix="teardown-")),
            disconnect=False,
        )

        self.assertEqual(disconnects, [], "borrowed device was disconnected")
        self.assertEqual(cast.quit_calls, 1, "receiver should still be released")

    def test_a_discovered_device_is_disconnected(self) -> None:
        session = silent_session()
        cast = FakeCast(APP_MEDIA_RECEIVER)

        disconnects = []
        cast.media_controller = SimpleNamespace(stop=lambda: None)
        cast.disconnect = lambda: disconnects.append(True)

        session._teardown(
            cast=cast,
            browser=None,
            server=None,
            ffmpeg=None,
            capture=None,
            directory=Path(tempfile.mkdtemp(prefix="teardown-")),
            disconnect=True,
        )

        self.assertEqual(disconnects, [True])

    def test_running_app_survives_a_broken_status(self) -> None:
        session = silent_session()

        self.assertEqual(session.running_app(SimpleNamespace(status=None)), "")


ENCODING = EncodeProgress(fps=29.7, speed=0.99, dropped=2, duplicated=41)


class Statistics(unittest.TestCase):
    """The window watches these while a cast runs; the CLI ignores them."""

    def snapshot(self, capture: object = None, rebuffers: int = 0) -> object:
        return CastSession._snapshot(
            elapsed=95.4,
            progress=ENCODING,
            bitrate_kbps=5904.2,
            capture=capture,
            rebuffers=rebuffers,
        )

    def test_there_is_nothing_to_report_before_a_cast(self) -> None:
        self.assertIsNone(silent_session().stats)

    def test_carries_what_ffmpeg_reported(self) -> None:
        stats = self.snapshot()

        self.assertAlmostEqual(stats.fps, 29.7)
        self.assertAlmostEqual(stats.speed, 0.99)
        self.assertEqual(stats.dropped, 2)
        self.assertEqual(stats.duplicated, 41)

    def test_carries_the_measured_bitrate_and_the_clock(self) -> None:
        stats = self.snapshot()

        self.assertAlmostEqual(stats.bitrate_kbps, 5904.2)
        self.assertAlmostEqual(stats.elapsed, 95.4)

    def test_carries_the_rebuffer_count(self) -> None:
        self.assertEqual(self.snapshot(rebuffers=3).rebuffers, 3)

    def test_no_system_audio_is_not_the_same_as_silence(self) -> None:
        """A missing level has to read as "off", not as "0 dB"."""
        self.assertIsNone(self.snapshot().audio_peak_dbfs)

    def test_the_level_comes_from_the_capture(self) -> None:
        capture = SimpleNamespace(peak_dbfs=-12.5)

        self.assertAlmostEqual(self.snapshot(capture).audio_peak_dbfs, -12.5)

    def test_a_snapshot_cannot_be_edited_from_under_a_reader(self) -> None:
        """The window reads these off the Tk thread while the monitor
        writes them; a whole frozen object swapped in is what makes
        that safe without a lock."""
        with self.assertRaises(FrozenInstanceError):
            self.snapshot().fps = 1.0

    def test_starting_again_drops_the_previous_cast(self) -> None:
        from castlib.session import CastOptions

        session = silent_session()
        session._stats = self.snapshot()
        session._run = lambda options: None

        session.start(CastOptions())
        session.stop()

        self.assertIsNone(session.stats)


class Lifecycle(unittest.TestCase):
    def test_starts_idle(self) -> None:
        self.assertIs(silent_session().state, SessionState.IDLE)

    def test_stop_on_an_idle_session_returns_immediately(self) -> None:
        session = silent_session()

        started = time.monotonic()
        session.stop()

        self.assertLess(time.monotonic() - started, 1.0)

    def test_stop_returns_even_if_the_worker_ignores_it(self) -> None:
        """Close leans on this: a stubborn worker must not trap the window."""
        from castlib.session import CastOptions

        session = silent_session()
        session._run = lambda options: time.sleep(5)

        session.start(CastOptions())

        started = time.monotonic()
        session.stop(timeout=1)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 2.5, "stop waited past its own timeout")

    def test_stop_is_repeatable(self) -> None:
        session = silent_session()

        session.stop()
        session.stop()

        self.assertFalse(session.is_running)


if __name__ == "__main__":
    unittest.main()
