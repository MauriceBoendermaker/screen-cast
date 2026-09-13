"""Remembered choices, kept beside the user's profile rather than the exe.

The exe may well end up somewhere unwritable, so nothing is stored next
to it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path


APP_DIRECTORY = "ScreenCast"
FILE_NAME = "settings.json"

SETTINGS_VERSION = 1

# "Balanced (~2s)" was briefly the default delay. It halves the player's
# buffer, which stutters on an ordinary wireless Chromecast, and anyone
# carrying it in their settings is carrying a default they never picked
# rather than a preference. Correct it once — and only once, so that
# choosing it deliberately afterwards sticks.
CORRECTIONS: dict[int, dict[str, dict[str, str]]] = {
    0: {"latency": {"Balanced (~2s)": "Balanced (~4s)"}},
}


@dataclass
class Settings:
    version: int = SETTINGS_VERSION
    device: str = ""
    quality: str = "Balanced (6 Mbps)"
    latency: str = "Smooth (~4s)"
    system_audio: bool = True
    audio_device: str = ""
    microphone: str = ""
    port: int = 8765


def settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA")

    root = Path(base) if base else Path.home()

    return root / APP_DIRECTORY / FILE_NAME


def load(path: Path | None = None) -> Settings:
    """Never fail to start over a bad settings file."""
    path = path or settings_path()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()

    if not isinstance(raw, dict):
        return Settings()

    known = {field.name for field in fields(Settings)}
    defaults = Settings()

    values = {}

    for name in known:
        value = raw.get(name, getattr(defaults, name))
        expected = type(getattr(defaults, name))

        values[name] = value if isinstance(value, expected) else getattr(defaults, name)

    # A file with no version predates them, so start below the first.
    values["version"] = raw.get("version", 0) if isinstance(raw, dict) else 0

    if not isinstance(values["version"], int):
        values["version"] = 0

    return _correct(Settings(**values))


def _correct(settings: Settings) -> Settings:
    """Undo defaults that turned out to be wrong, once each.

    A stored value normally outranks a default, which is right up until
    the stored value *is* a default the user never chose.
    """
    for version, fixes in sorted(CORRECTIONS.items()):
        if settings.version > version:
            continue

        for name, replacements in fixes.items():
            current = getattr(settings, name, None)

            if current in replacements:
                setattr(settings, name, replacements[current])

    settings.version = SETTINGS_VERSION

    return settings


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    except OSError:
        pass
