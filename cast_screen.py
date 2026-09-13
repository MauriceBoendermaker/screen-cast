"""Cast the Windows desktop, with audio, to a Cast device."""

from __future__ import annotations

import argparse
import sys
import time

import pychromecast

from castlib import audio, discovery, streaming
from castlib.audio import AudioDevice
from castlib.session import CastOptions, CastSession, SessionState


def select_chromecast(
    chromecasts: list[pychromecast.Chromecast],
    name: str | None,
) -> pychromecast.Chromecast:
    if name is not None:
        return discovery.select_by_name(chromecasts, name)

    if len(chromecasts) == 1:
        cast = chromecasts[0]

        print(f"Found {discovery.device_label(cast)} ({cast.cast_info.host})")

        return cast

    print()

    for index, cast in enumerate(chromecasts, start=1):
        print(f"{index}. {discovery.device_label(cast)} ({cast.cast_info.host})")

    print()

    while True:
        try:
            selected = int(input("Select device: "))

            if 1 <= selected <= len(chromecasts):
                return chromecasts[selected - 1]
        except ValueError:
            pass
        except EOFError:
            raise RuntimeError("No device selected.")

        print("Invalid selection.")


def select_output(pattern: str) -> audio.OutputDevice:
    outputs = audio.list_loopback_devices()

    for device in outputs:
        if pattern.lower() in device.label.lower():
            return device

    available = ", ".join(repr(device.label) for device in outputs)

    raise RuntimeError(f"No audio source matching {pattern!r}. Found: {available}")


def select_microphone(pattern: str) -> AudioDevice:
    microphones = audio.list_microphones()

    for device in microphones:
        if pattern.lower() in device.label.lower():
            return device

    available = ", ".join(repr(device.label) for device in microphones)

    raise RuntimeError(f"No microphone matching {pattern!r}. Found: {available}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cast the desktop to a Cast device.")

    parser.add_argument(
        "--host",
        help="IP address of the Cast device, skipping discovery.",
    )

    parser.add_argument(
        "--name",
        help="Friendly name of the Cast device, skipping the prompt.",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local port used to serve the stream (default: 8765).",
    )

    parser.add_argument(
        "--quality",
        choices=list(streaming.QUALITY_PRESETS),
        default=streaming.DEFAULT_QUALITY,
        help="Picture quality (default: %(default)s).",
    )

    parser.add_argument(
        "--delay",
        choices=list(streaming.LATENCY_PRESETS),
        default=streaming.DEFAULT_LATENCY,
        help="How far behind live the TV runs (default: %(default)s).",
    )

    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="Cast without system audio.",
    )

    parser.add_argument(
        "--audio-device",
        help=(
            "Speaker endpoint to capture, matched by part of its name "
            "(default: the Windows default output)."
        ),
    )

    parser.add_argument(
        "--mic",
        help="Mix in a microphone, matched by part of its name.",
    )

    parser.add_argument(
        "--list-audio",
        action="store_true",
        help="List capturable speakers and microphones, then exit.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.list_audio:
        print("Speakers (capturable with --audio-device):")

        for device in audio.list_loopback_devices():
            default = "  [default]" if device.is_default else ""

            print(f"  {device.label}{default}")

        print()
        print("Microphones (mixable with --mic):")

        for microphone in audio.list_microphones():
            print(f"  {microphone.label}")

        return

    browser = None
    session = None

    streaming.clean_stale_directories()

    try:
        microphone = select_microphone(args.mic) if args.mic else None
        output = select_output(args.audio_device) if args.audio_device else None

        devices, browser = discovery.discover_devices(args.host, on_status=print)

        cast = select_chromecast(devices, args.name)

        session = CastSession(on_event=lambda event: print(event.message))

        session.start(
            CastOptions(
                device=cast,
                port=args.port,
                quality=args.quality,
                latency=args.delay,
                system_audio=not args.no_audio,
                audio_device=output,
                microphone=microphone,
            )
        )

        print()
        print("Press Ctrl+C to stop.")
        print()

        while session.state in (SessionState.STARTING, SessionState.CASTING):
            time.sleep(0.5)

        if session.state is SessionState.FAILED:
            sys.exit(1)
    except KeyboardInterrupt:
        print()

        if session is not None:
            session.stop()
    except Exception as error:
        print()
        print(f"Error: {error}")
        sys.exit(1)
    finally:
        if browser is not None:
            try:
                browser.stop_discovery()
            except Exception:
                pass


if __name__ == "__main__":
    main()
