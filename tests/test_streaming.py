"""The ffmpeg command line is pure data, so assert on it directly."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from castlib.audio import AudioDevice, LoopbackFormat
from castlib import streaming
from castlib.streaming import (
    TEMP_PREFIX,
    StreamSettings,
    build_ffmpeg_command,
    clean_stale_directories,
)


LOOPBACK = LoopbackFormat(rate=48000, channels=2)
MIC = AudioDevice("Microphone (Test)", "@device_cm_{GUID}\\wave_{MIC}")


def settings(**kwargs: object) -> StreamSettings:
    return StreamSettings(directory=Path("C:/tmp/cast"), **kwargs)


def argument_after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


class VideoOnly(unittest.TestCase):
    def setUp(self) -> None:
        self.command = build_ffmpeg_command(settings(system_audio=False))

    def test_audio_is_disabled(self) -> None:
        self.assertIn("-an", self.command)

    def test_no_audio_codec(self) -> None:
        self.assertNotIn("-c:a", self.command)

    def test_captures_the_desktop(self) -> None:
        # Which grabber is used is CaptureBackend's business; here it
        # only matters that a screen source is opened at all.
        self.assertTrue(
            any("ddagrab" in part for part in self.command)
            or "gdigrab" in self.command
        )

    def test_maps_only_video(self) -> None:
        self.assertEqual(self.command.count("-map"), 1)
        self.assertIn("[v]", self.command)


class SystemAudio(unittest.TestCase):
    def setUp(self) -> None:
        self.command = build_ffmpeg_command(
            settings(system_audio=True, loopback=LOOPBACK)
        )

    def test_audio_is_not_disabled(self) -> None:
        self.assertNotIn("-an", self.command)

    def test_reads_pcm_from_the_pipe(self) -> None:
        self.assertIn("pipe:0", self.command)
        self.assertIn("s16le", self.command)

    def test_declares_the_capture_format(self) -> None:
        self.assertIn("48000", self.command)

    def test_encodes_aac(self) -> None:
        self.assertEqual(argument_after(self.command, "-c:a"), "aac")

    def test_maps_video_and_audio(self) -> None:
        self.assertEqual(self.command.count("-map"), 2)
        self.assertIn("[a]", self.command)

    def test_resamples_to_correct_drift(self) -> None:
        graph = argument_after(self.command, "-filter_complex")

        self.assertIn("aresample=async=1", graph)

    def test_does_not_mix(self) -> None:
        graph = argument_after(self.command, "-filter_complex")

        self.assertNotIn("amix", graph)


class SystemAudioAndMicrophone(unittest.TestCase):
    def setUp(self) -> None:
        self.command = build_ffmpeg_command(
            settings(system_audio=True, loopback=LOOPBACK, microphone=MIC)
        )
        self.graph = argument_after(self.command, "-filter_complex")

    def test_opens_the_microphone_through_dshow(self) -> None:
        self.assertIn("dshow", self.command)
        self.assertIn(f"audio={MIC.moniker}", self.command)

    def test_mixes_both_sources(self) -> None:
        self.assertIn("amix=inputs=2", self.graph)

    def test_mixing_does_not_halve_the_volume(self) -> None:
        self.assertIn("normalize=0", self.graph)

    def test_system_audio_is_the_master_clock(self) -> None:
        self.assertIn("duration=first", self.graph)

    def test_inputs_are_numbered_in_order(self) -> None:
        self.assertIn("[1:a]", self.graph)
        self.assertIn("[2:a]", self.graph)

    def test_every_input_gets_a_thread_queue(self) -> None:
        self.assertEqual(self.command.count("-thread_queue_size"), 3)


class MicrophoneOnly(unittest.TestCase):
    def setUp(self) -> None:
        self.command = build_ffmpeg_command(
            settings(system_audio=False, microphone=MIC)
        )
        self.graph = argument_after(self.command, "-filter_complex")

    def test_audio_is_not_disabled(self) -> None:
        self.assertNotIn("-an", self.command)

    def test_does_not_open_the_pipe(self) -> None:
        self.assertNotIn("pipe:0", self.command)

    def test_microphone_becomes_the_first_audio_input(self) -> None:
        self.assertIn("[1:a]", self.graph)
        self.assertNotIn("[2:a]", self.graph)


class SystemAudioRequestedButUnavailable(unittest.TestCase):
    def test_falls_back_to_silence(self) -> None:
        # The session clears the flag when no loopback device resolves.
        command = build_ffmpeg_command(settings(system_audio=True, loopback=None))

        self.assertIn("-an", command)
        self.assertNotIn("pipe:0", command)


class CaptureBackend(unittest.TestCase):
    """gdigrab delivered 17.6fps here; ffmpeg then duplicates up to 30,
    so the output reports 30fps while the motion is half that."""

    def test_defaults_to_the_gpu_grabber(self) -> None:
        command = build_ffmpeg_command(settings())

        self.assertIn("lavfi", command)
        self.assertTrue(any("ddagrab" in part for part in command))
        self.assertNotIn("gdigrab", command)

    def test_gpu_frames_are_brought_back_to_the_cpu(self) -> None:
        graph = argument_after(build_ffmpeg_command(settings()), "-filter_complex")

        self.assertIn("hwdownload", graph)
        self.assertIn("format=yuv420p", graph)

    def test_falls_back_to_gdigrab(self) -> None:
        command = build_ffmpeg_command(settings(capture="gdigrab"))

        self.assertIn("gdigrab", command)
        self.assertIn("desktop", command)
        self.assertFalse(any("ddagrab" in part for part in command))

    def test_fallback_does_not_download_frames(self) -> None:
        graph = argument_after(
            build_ffmpeg_command(settings(capture="gdigrab")), "-filter_complex"
        )

        self.assertNotIn("hwdownload", graph)

    def test_both_backends_request_the_mouse_cursor(self) -> None:
        for backend in ("ddagrab", "gdigrab"):
            command = build_ffmpeg_command(settings(capture=backend))

            self.assertTrue(
                any("draw_mouse" in part for part in command),
                f"{backend} did not ask for the cursor",
            )


class Quality(unittest.TestCase):
    def test_presets_raise_the_bitrate_in_order(self) -> None:
        rates = [
            int(bitrate.rstrip("k"))
            for bitrate, _ in streaming.QUALITY_PRESETS.values()
        ]

        self.assertEqual(rates, sorted(rates))

    def test_default_is_a_real_preset(self) -> None:
        self.assertIn(streaming.DEFAULT_QUALITY, streaming.QUALITY_PRESETS)

    def test_applying_a_preset_changes_the_bitrate(self) -> None:
        chosen = settings()
        chosen.apply_quality("Sharp (10 Mbps)")

        command = build_ffmpeg_command(chosen)

        self.assertEqual(argument_after(command, "-b:v"), "10000k")
        self.assertEqual(argument_after(command, "-bufsize"), "20000k")

    def test_unknown_preset_is_ignored(self) -> None:
        chosen = settings()
        before = chosen.bitrate

        chosen.apply_quality("Nonsense")

        self.assertEqual(chosen.bitrate, before)


class LinkFriendliness(unittest.TestCase):
    """Settings that keep a weak Chromecast link from starving.

    The device measured 58ms average RTT on the LAN, which caps TCP
    throughput near the stream's own bitrate.
    """

    def setUp(self) -> None:
        self.command = build_ffmpeg_command(
            settings(system_audio=True, loopback=LOOPBACK)
        )

    def test_does_not_append_to_a_playlist(self) -> None:
        """append_list writes a leading EXT-X-DISCONTINUITY.

        Every cast starts in a fresh directory, so there is never a
        playlist to append to; the tag only confuses the player.
        """
        flags = argument_after(self.command, "-hls_flags")

        self.assertNotIn("append_list", flags)

    def test_still_deletes_old_segments(self) -> None:
        flags = argument_after(self.command, "-hls_flags")

        self.assertIn("delete_segments", flags)

    def test_keeps_segments_around_after_rotation(self) -> None:
        """A player that falls behind must not hit a 404."""
        self.assertIn("-hls_delete_threshold", self.command)

        threshold = int(argument_after(self.command, "-hls_delete_threshold"))

        self.assertGreaterEqual(threshold, 3)

    def test_keyframes_align_with_segments(self) -> None:
        """A segment must start on a keyframe, whatever its length."""
        for preset, (hls_time, _) in streaming.LATENCY_PRESETS.items():
            chosen = settings()
            chosen.apply_latency(preset)

            command = build_ffmpeg_command(chosen)
            gop = int(argument_after(command, "-g"))
            fps = int(argument_after(command, "-r"))

            self.assertEqual(gop, fps * hls_time, preset)


class Latency(unittest.TestCase):
    def test_default_is_a_real_preset(self) -> None:
        self.assertIn(streaming.DEFAULT_LATENCY, streaming.LATENCY_PRESETS)

    def test_default_is_never_the_shortest_delay(self) -> None:
        """Delay and buffer are the same number in live HLS.

        The default has to survive an ordinary wireless Chromecast, so
        it must not be the most aggressive rung on the ladder.
        """
        depths = {
            name: hls_time * list_size
            for name, (hls_time, list_size) in streaming.LATENCY_PRESETS.items()
        }

        self.assertNotEqual(
            streaming.DEFAULT_LATENCY,
            min(depths, key=depths.get),
        )

    def test_default_segments_are_long_enough_to_fetch(self) -> None:
        """One-second segments stalled once a second on a real device."""
        hls_time, _ = streaming.LATENCY_PRESETS[streaming.DEFAULT_LATENCY]

        self.assertGreaterEqual(hls_time, 2)

    def test_each_step_changes_the_segment_length(self) -> None:
        """Presets differing only in playlist length are the same choice
        under two names, which is what the earlier ladder did."""
        lengths = [hls_time for hls_time, _ in streaming.LATENCY_PRESETS.values()]

        self.assertEqual(len(lengths), len(set(lengths)))

    def test_a_stale_setting_falls_back_to_the_default(self) -> None:
        """Never to whatever happens to be first in the list."""
        self.assertEqual(
            streaming.resolve_latency("Balanced (~2s)"),
            streaming.DEFAULT_LATENCY,
        )
        self.assertEqual(
            streaming.resolve_quality("Ludicrous"), streaming.DEFAULT_QUALITY
        )

    def test_a_stale_setting_does_not_become_the_fastest(self) -> None:
        chosen = settings()
        chosen.apply_latency("Balanced (~2s)")

        self.assertGreaterEqual(chosen.hls_time, 2)

    def test_presets_are_ordered_shortest_delay_first(self) -> None:
        delays = [
            hls_time * list_size
            for hls_time, list_size in streaming.LATENCY_PRESETS.values()
        ]

        self.assertEqual(delays, sorted(delays))

    def test_applying_a_preset_changes_segmenting(self) -> None:
        chosen = settings()
        chosen.apply_latency("Smooth (~8s)")

        command = build_ffmpeg_command(chosen)

        self.assertEqual(argument_after(command, "-hls_time"), "4")
        self.assertEqual(argument_after(command, "-hls_list_size"), "8")

    def test_low_delay_uses_short_segments(self) -> None:
        chosen = settings()
        chosen.apply_latency("Low (~2s)")

        self.assertEqual(chosen.hls_time, 1)

    def test_unknown_preset_is_ignored(self) -> None:
        chosen = settings()
        before = (chosen.hls_time, chosen.hls_list_size)

        chosen.apply_latency("Nonsense")

        self.assertEqual((chosen.hls_time, chosen.hls_list_size), before)

    def test_segments_outlive_the_playlist_at_every_preset(self) -> None:
        """Short playlists are only safe if rotated segments linger."""
        for preset in streaming.LATENCY_PRESETS:
            chosen = settings()
            chosen.apply_latency(preset)

            command = build_ffmpeg_command(chosen)
            threshold = int(argument_after(command, "-hls_delete_threshold"))

            self.assertGreaterEqual(threshold, 3, preset)


class Preroll(unittest.TestCase):
    """Handing over a one-segment playlist pins the player to the live
    edge, where a real-time producer can never get ahead of it."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="preroll-"))
        self.alive = SimpleNamespace(poll=lambda: None, returncode=None)

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def write_playlist(self, count: int) -> None:
        lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:2"]

        for index in range(count):
            lines += ["#EXTINF:2.000000,", f"segment_{index:05d}.ts"]

        (self.directory / "live.m3u8").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    def test_counts_only_listed_segments(self) -> None:
        self.write_playlist(3)

        self.assertEqual(streaming.listed_segments(self.directory), 3)

    def test_missing_playlist_counts_as_none(self) -> None:
        self.assertEqual(streaming.listed_segments(self.directory), 0)

    def test_waits_for_the_requested_depth(self) -> None:
        self.write_playlist(2)

        with self.assertRaises(RuntimeError):
            streaming.wait_for_stream(
                self.alive, self.directory, segments=4, timeout=0.8
            )

    def test_returns_once_deep_enough(self) -> None:
        self.write_playlist(4)

        streaming.wait_for_stream(
            self.alive, self.directory, segments=4, timeout=2
        )

    def test_a_dead_encoder_is_reported_not_waited_out(self) -> None:
        dead = SimpleNamespace(poll=lambda: 1, returncode=1)

        with self.assertRaises(RuntimeError) as caught:
            streaming.wait_for_stream(dead, self.directory, segments=4, timeout=5)

        self.assertIn("exit code 1", str(caught.exception))

    def test_preroll_is_deep_enough_to_matter(self) -> None:
        self.assertGreaterEqual(streaming.PREROLL_SEGMENTS, 3)


class StaleDirectories(unittest.TestCase):
    """A hard-killed cast cannot clean up after itself."""

    def setUp(self) -> None:
        self.temp = Path(tempfile.gettempdir())

        self.old = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=self.temp))
        self.fresh = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=self.temp))
        self.unrelated = Path(tempfile.mkdtemp(prefix="something-else-", dir=self.temp))

        (self.old / "segment_00000.ts").write_bytes(b"stale")

        ancient = time.time() - 7200
        os.utime(self.old, (ancient, ancient))

    def tearDown(self) -> None:
        for directory in (self.old, self.fresh, self.unrelated):
            shutil.rmtree(directory, ignore_errors=True)

    def test_removes_directories_left_behind(self) -> None:
        clean_stale_directories()

        self.assertFalse(self.old.exists())

    def test_leaves_a_running_cast_alone(self) -> None:
        clean_stale_directories()

        self.assertTrue(self.fresh.exists())

    def test_ignores_directories_that_are_not_ours(self) -> None:
        clean_stale_directories()

        self.assertTrue(self.unrelated.exists())


if __name__ == "__main__":
    unittest.main()
