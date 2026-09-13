"""A bad settings file must never stop the app from starting."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from castlib import settings as settings_store
from castlib.settings import Settings


class RoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="settings-test-"))
        self.path = self.directory / "settings.json"

    def test_saved_values_come_back(self) -> None:
        original = Settings(
            device="Huiskamer 4K",
            system_audio=False,
            audio_device="Speakers (Realtek(R) Audio)",
            microphone="Microphone (Test)",
            port=9000,
        )

        settings_store.save(original, self.path)

        self.assertEqual(settings_store.load(self.path), original)

    def test_save_creates_missing_directories(self) -> None:
        nested = self.directory / "a" / "b" / "settings.json"

        settings_store.save(Settings(device="X"), nested)

        self.assertTrue(nested.exists())


class Recovery(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="settings-test-"))
        self.path = self.directory / "settings.json"

    def test_missing_file_gives_defaults(self) -> None:
        self.assertEqual(settings_store.load(self.path), Settings())

    def test_corrupt_json_gives_defaults(self) -> None:
        self.path.write_text("{not json at all", encoding="utf-8")

        self.assertEqual(settings_store.load(self.path), Settings())

    def test_wrong_top_level_type_gives_defaults(self) -> None:
        self.path.write_text('["a", "list"]', encoding="utf-8")

        self.assertEqual(settings_store.load(self.path), Settings())

    def test_wrong_field_type_falls_back_per_field(self) -> None:
        self.path.write_text(
            json.dumps({"device": "Keep me", "port": "not a number"}),
            encoding="utf-8",
        )

        loaded = settings_store.load(self.path)

        self.assertEqual(loaded.device, "Keep me")
        self.assertEqual(loaded.port, Settings().port)

    def test_unknown_keys_are_ignored(self) -> None:
        self.path.write_text(
            json.dumps({"device": "Keep me", "from_a_later_version": 1}),
            encoding="utf-8",
        )

        self.assertEqual(settings_store.load(self.path).device, "Keep me")

    def test_unwritable_path_does_not_raise(self) -> None:
        # A directory where the file should be: saving must give up quietly.
        blocked = self.directory / "blocked"
        blocked.mkdir()

        settings_store.save(Settings(), blocked)


class Corrections(unittest.TestCase):
    """A stored value beats a default — unless it *is* a bad default."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="settings-test-"))
        self.path = self.directory / "settings.json"

    def write(self, payload: dict) -> None:
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def test_the_short_delay_default_is_corrected(self) -> None:
        self.write({"latency": "Balanced (~2s)", "device": "Huiskamer 4K"})

        loaded = settings_store.load(self.path)

        self.assertEqual(loaded.latency, "Balanced (~4s)")

    def test_correcting_leaves_everything_else_alone(self) -> None:
        self.write({"latency": "Balanced (~2s)", "device": "Huiskamer 4K"})

        self.assertEqual(settings_store.load(self.path).device, "Huiskamer 4K")

    def test_a_deliberate_choice_afterwards_is_kept(self) -> None:
        """Once corrected, picking it on purpose has to stick."""
        self.write(
            {
                "version": settings_store.SETTINGS_VERSION,
                "latency": "Balanced (~2s)",
            }
        )

        self.assertEqual(settings_store.load(self.path).latency, "Balanced (~2s)")

    def test_other_delays_are_untouched(self) -> None:
        self.write({"latency": "Low (~2s)"})

        self.assertEqual(settings_store.load(self.path).latency, "Low (~2s)")

    def test_loading_stamps_the_current_version(self) -> None:
        self.write({"latency": "Balanced (~2s)"})

        self.assertEqual(
            settings_store.load(self.path).version,
            settings_store.SETTINGS_VERSION,
        )

    def test_the_correction_survives_a_round_trip(self) -> None:
        self.write({"latency": "Balanced (~2s)"})

        settings_store.save(settings_store.load(self.path), self.path)

        self.assertEqual(settings_store.load(self.path).latency, "Balanced (~4s)")

    def test_a_garbled_version_still_loads(self) -> None:
        self.write({"version": "not a number", "latency": "Balanced (~2s)"})

        self.assertEqual(settings_store.load(self.path).latency, "Balanced (~4s)")


class Location(unittest.TestCase):
    def test_lives_under_the_user_profile(self) -> None:
        path = settings_store.settings_path()

        self.assertEqual(path.name, "settings.json")
        self.assertEqual(path.parent.name, "ScreenCast")


if __name__ == "__main__":
    unittest.main()
