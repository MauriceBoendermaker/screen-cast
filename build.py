"""Build ScreenCast.exe.

PyInstaller cannot bundle the Microsoft Store build of Python, so this
must run under a python.org interpreter. ffmpeg stays a PATH dependency:
bundling it would add roughly 80MB for something already installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from castlib import __version__
from make_icon import write_icon


ROOT = Path(__file__).parent
NAME = "ScreenCast"
ENTRY = "screen_cast_gui.py"

SELFTEST_REPORT = Path(tempfile.gettempdir()) / "screencast_selftest.txt"


def check_interpreter() -> None:
    if "WindowsApps" in sys.executable:
        raise SystemExit(
            "This is the Microsoft Store Python, which PyInstaller cannot "
            "bundle.\nRun the build with a python.org interpreter, e.g.\n"
            "  %LOCALAPPDATA%\\Programs\\Python\\Python314\\python.exe build.py"
        )

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            f"PyInstaller is missing. Install the build dependencies:\n"
            f"  {sys.executable} -m pip install pychromecast pyaudiowpatch pyinstaller"
        )


def build(icon: Path) -> Path:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed",
        "--clean",
        "--noconfirm",
        "--name",
        NAME,
        "--icon",
        str(icon),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "build"),
        str(ROOT / ENTRY),
    ]

    print("building...\n")

    result = subprocess.run(command, cwd=ROOT)

    if result.returncode != 0:
        raise SystemExit(f"PyInstaller failed with exit code {result.returncode}")

    executable = ROOT / "dist" / f"{NAME}.exe"

    if not executable.exists():
        raise SystemExit(f"Build reported success but {executable} is missing.")

    return executable


def verify(executable: Path) -> None:
    """Run the frozen build's self-test.

    A one-file exe can lose bundled native libraries without any build
    error; the first symptom would otherwise be an empty device list.
    """
    SELFTEST_REPORT.unlink(missing_ok=True)

    print("\nverifying the bundle...\n")

    result = subprocess.run([str(executable), "--selftest"], timeout=180)

    report = (
        SELFTEST_REPORT.read_text(encoding="utf-8")
        if SELFTEST_REPORT.exists()
        else "(the build produced no report)"
    )

    print(report)

    if result.returncode != 0:
        raise SystemExit("\nThe frozen build failed its self-test.")


def main() -> None:
    check_interpreter()

    icon = write_icon(ROOT / "screencast.ico")

    print(f"version : {__version__}")
    print(f"icon    : {icon.name}")
    print(f"python  : {sys.executable}")
    print(f"ffmpeg  : {shutil.which('ffmpeg') or 'NOT FOUND (needed at run time)'}\n")

    executable = build(icon)

    verify(executable)

    size = executable.stat().st_size / (1024 * 1024)

    print(f"\nBuilt {executable}  {__version__}  ({size:.1f} MB)")
    print("Double-click it, or pin it to the taskbar.")


if __name__ == "__main__":
    main()
