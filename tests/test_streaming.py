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
    measured_bitrate_kbps,
    parse_progress,
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


class CapturePollRate(unittest.TestCase):
    """Measured during real viewing, 10.4% of frames reached the device
    held — 2.6 frozen pictures a second. A source and a sampler running
    at the same nominal rate on different clocks beat against each other,
    so the sampler has to run above the source and keep what it catches:
    one capture, one frame sent, no second resampling stage."""

    def test_captures_at_the_output_rate(self) -> None:
        command = build_ffmpeg_command(settings(fps=50))
        source = next(part for part in command if "ddagrab" in part)

        self.assertIn("framerate=50", source)
        self.assertEqual(argument_after(command, "-r"), "50")

    def test_capture_rate_follows_the_output_rate(self) -> None:
        command = build_ffmpeg_command(settings(fps=30))
        source = next(part for part in command if "ddagrab" in part)

        self.assertIn("framerate=30", source)

    def test_nothing_is_decimated_between_capture_and_encode(self) -> None:
        """A decimation stage is a second resampling, and a second
        chance to land on the wrong frame."""
        command = build_ffmpeg_command(settings(fps=50))
        graph = argument_after(command, "-filter_complex")

        self.assertIn("fps=50", graph)
        self.assertEqual(argument_after(command, "-r"), "50")

    def test_the_rate_filter_precedes_the_readback(self) -> None:
        """Behind hwdownload, a discarded frame would still have cost a
        full native-resolution trip across PCIe first."""
        graph = argument_after(build_ffmpeg_command(settings(fps=30)), "-filter_complex")

        self.assertLess(graph.index("fps=30"), graph.index("hwdownload"))

    def test_defaults_to_fifty(self) -> None:
        """50 divides a 50Hz television exactly, and samples a 25fps
        broadcast at twice its rate."""
        command = build_ffmpeg_command(settings())

        self.assertEqual(argument_after(command, "-r"), "50")

    def test_the_slow_grabber_is_not_polled_faster(self) -> None:
        """gdigrab cannot reach 30 here, let alone 60; asking would only
        spend CPU duplicating frames earlier in the chain."""
        command = build_ffmpeg_command(settings(capture="gdigrab", fps=30))
        graph = argument_after(command, "-filter_complex")

        self.assertEqual(argument_after(command, "-framerate"), "30")
        self.assertNotIn("fps=", graph)


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


# Captured from ffmpeg 8.1 writing HLS. Two things worth keeping: the
# speed value arrives padded, and bitrate is N/A because an HLS output
# is a stream of files with no single size to divide.
PROGRESS = """\
frame=45
fps=0.00
stream_0_0_q=-1.0
bitrate=N/A
total_size=N/A
out_time_us=1500000
out_time_ms=1500000
out_time=00:00:01.500000
dup_frames=0
drop_frames=0
speed=1.99x
progress=continue
frame=105
fps=29.94
stream_0_0_q=28.0
bitrate=N/A
total_size=N/A
out_time_us=3500000
out_time_ms=3500000
out_time=00:00:03.500000
dup_frames=7
drop_frames=2
speed=  1.1x
progress=continue
"""


class Progress(unittest.TestCase):
    """Parsing what ffmpeg says about itself, no process required."""

    def setUp(self) -> None:
        self.progress = parse_progress(PROGRESS)

    def test_the_command_asks_ffmpeg_to_report(self) -> None:
        command = build_ffmpeg_command(settings())

        self.assertEqual(argument_after(command, "-progress"), "pipe:1")

    def test_reads_the_frame_rate(self) -> None:
        self.assertAlmostEqual(self.progress.fps, 29.94)

    def test_reads_the_speed_through_its_padding(self) -> None:
        self.assertAlmostEqual(self.progress.speed, 1.1)

    def test_counts_frames_thrown_away(self) -> None:
        self.assertEqual(self.progress.dropped, 2)
        self.assertEqual(self.progress.duplicated, 7)

    def test_the_last_block_wins(self) -> None:
        """The first block reported 0.00 fps and nothing dropped."""
        self.assertNotAlmostEqual(self.progress.fps, 0.0)

    def test_a_half_written_block_is_ignored(self) -> None:
        """A read can land mid-block, and mixing halves of two blocks
        would report a frame rate against the wrong frame count."""
        torn = parse_progress(PROGRESS + "frame=200\nfps=99.00\ndup_frames=99\n")

        self.assertAlmostEqual(torn.fps, 29.94)
        self.assertEqual(torn.duplicated, 7)

    def test_nothing_yet_reads_as_zero_not_a_crash(self) -> None:
        self.assertEqual(parse_progress(""), streaming.EncodeProgress())

    def test_a_value_ffmpeg_cannot_answer_keeps_the_last_known(self) -> None:
        """N/A means "not worked out yet", not "it is zero"."""
        later = parse_progress(
            PROGRESS + "frame=200\nfps=N/A\ndup_frames=9\nprogress=continue\n"
        )

        self.assertAlmostEqual(later.fps, 29.94)
        self.assertEqual(later.duplicated, 9)

    def test_the_closing_block_still_counts(self) -> None:
        ended = parse_progress(PROGRESS.replace("progress=continue", "progress=end"))

        self.assertAlmostEqual(ended.fps, 29.94)


class ProgressReader(unittest.TestCase):
    """The reader owns the pipe it is handed, teardown included."""

    def setUp(self) -> None:
        read, self.write = os.pipe()

        self.source = os.fdopen(read, "rb")
        self.progress = streaming.FfmpegProgress()

    def tearDown(self) -> None:
        # Let go of the write end first, so a reader left blocked by the
        # test above reaches EOF and the rest of this can finish.
        try:
            os.close(self.write)
        except OSError:
            pass

        self.progress.stop()
        self.source.close()

    def test_picks_up_blocks_as_they_arrive(self) -> None:
        self.progress.start(self.source)

        os.write(self.write, PROGRESS.encode())
        os.close(self.write)

        self.progress.stop()

        self.assertAlmostEqual(self.progress.latest.fps, 29.94)
        self.assertEqual(self.progress.latest.duplicated, 7)

    def test_nothing_read_yet_is_still_a_usable_answer(self) -> None:
        self.assertEqual(self.progress.latest, streaming.EncodeProgress())

    def test_stop_without_a_start_does_nothing(self) -> None:
        self.progress.stop()

    def test_stop_does_not_wait_out_a_reader_still_stuck_on_the_pipe(self) -> None:
        """A killed ffmpeg dies asynchronously on Windows, so the read
        can outlive it. Closing the pipe under the blocked reader would
        wait for the process too — and Stop is what someone presses
        when they have had enough of waiting."""
        self.progress.start(self.source)

        started = time.monotonic()
        self.progress.stop()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 4.0, "stop() blocked on the wedged reader")
        self.assertFalse(self.source.closed, "closing it would have hung")


class MeasuredBitrate(unittest.TestCase):
    """ffmpeg answers bitrate=N/A for an HLS output, every block, for as
    long as the cast runs: it is writing a stream of files rather than
    one, so it has no total size to divide. The segments themselves are
    the only honest source, and the better one — they are what the
    device is actually being asked to pull."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="bitrate-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)

    def write_playlist(self, segments: list[tuple[float, int]]) -> None:
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2"]

        for index, (duration, size) in enumerate(segments):
            name = f"segment_{index:05d}.ts"

            (self.directory / name).write_bytes(bytes(size))

            lines += [f"#EXTINF:{duration:.6f},", name]

        (self.directory / "live.m3u8").write_text(
            "\n".join(lines), encoding="utf-8"
        )

    def test_weighs_the_listed_segments(self) -> None:
        # 150,000 bytes across two seconds is 600 kbit/s.
        self.write_playlist([(2.0, 150_000)])

        self.assertAlmostEqual(measured_bitrate_kbps(self.directory), 600.0)

    def test_averages_across_the_playlist(self) -> None:
        self.write_playlist([(2.0, 150_000), (2.0, 50_000)])

        self.assertAlmostEqual(measured_bitrate_kbps(self.directory), 400.0)

    def test_a_segment_rotated_away_is_left_out_of_both_halves(self) -> None:
        """Counting its duration but not its bytes would report a
        collapse in bitrate every time a segment aged out."""
        self.write_playlist([(2.0, 150_000), (2.0, 50_000)])

        (self.directory / "segment_00000.ts").unlink()

        self.assertAlmostEqual(measured_bitrate_kbps(self.directory), 200.0)

    def test_a_missing_playlist_is_not_an_error(self) -> None:
        self.assertEqual(measured_bitrate_kbps(self.directory), 0.0)

    def test_a_playlist_with_no_segments_yet_is_zero(self) -> None:
        (self.directory / "live.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

        self.assertEqual(measured_bitrate_kbps(self.directory), 0.0)


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
