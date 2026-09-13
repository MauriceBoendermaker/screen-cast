"""Windows 98 flavoured front end for casting the desktop."""

from __future__ import annotations

import math
import queue
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from typing import Callable

import pychromecast

from castlib import __version__, audio, discovery, streaming, win98
from castlib import settings as settings_store
from castlib.audio import AudioDevice, OutputDevice
from castlib.session import (
    CastOptions,
    CastSession,
    CastStats,
    SessionEvent,
    SessionState,
)
from castlib.win98 import scale


TITLE = "Screen Cast"
WIDTH = 360
HEIGHT = 328

# The panel opens with the cast, but the monitor that fills it only
# starts once the device is actually playing — a good ten seconds later,
# behind the head start. Say so rather than showing three blank lines.
WAITING_FOR_STATS = ("Waiting for the encoder...", "", "")

MIC_WARNING = "Microphone (picks up the TV)"

DESCRIPTION = "Casts the Windows desktop, with sound, to a Chromecast."

# How long Close waits for a tidy shutdown before going anyway. A cast
# that is still negotiating with the device can otherwise hold the
# window open for the length of the device's own timeout.
CLOSE_GRACE = 3.0


def format_elapsed(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, remainder = divmod(rest, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{remainder:02d}"

    return f"{minutes}:{remainder:02d}"


def plural(count: int, noun: str) -> str:
    """One dropped frame is not "1 frames"."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def format_peak(dbfs: float | None) -> str:
    """Nothing at all when system audio is off, which is not silence."""
    if dbfs is None:
        return ""

    if dbfs == float("-inf"):
        return "no sound"

    return f"peak {dbfs:.0f} dB"


def format_stats(stats: CastStats | None) -> tuple[str, str, str]:
    """Three lines: how fast it encodes, what it throws away, how it lands."""
    if stats is None:
        return WAITING_FOR_STATS

    landing = [
        format_elapsed(stats.elapsed),
        plural(stats.rebuffers, "rebuffer"),
    ]

    peak = format_peak(stats.audio_peak_dbfs)

    if peak:
        landing.append(peak)

    return (
        f"{stats.fps:.1f} fps at {stats.speed:.2f}x, "
        f"{stats.bitrate_kbps / 1000:.1f} Mbps",
        f"{plural(stats.dropped, 'frame')} dropped, "
        f"{stats.duplicated} duplicated",
        ", ".join(landing),
    )


class AboutDialog:
    """Where the version lives, since the title bar deliberately does not.

    Period-correct Win98 apps put it here rather than in the caption,
    and it is the one place to answer "was this the build with the fix".
    """

    WIDTH = 236

    def __init__(
        self,
        parent: win98.AppWindow,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self._parent = parent
        self._on_closed = on_closed
        self._closed = False

        # Height is whatever the four lines of content come to; only the
        # width is a choice, and it is what the description wraps to.
        self.window = win98.AppWindow(
            f"About {TITLE}",
            self.WIDTH,
            0,
            on_close=self.close,
            parent=parent.root,
        )

        body = self.window.body

        name = self.window.label(body, TITLE)
        name.configure(font=win98.ui_font(bold=True))
        name.pack(anchor="w")

        self.window.label(body, f"Version {__version__}").pack(
            anchor="w", pady=(scale(2), scale(8))
        )

        description = self.window.label(body, DESCRIPTION)
        description.configure(
            wraplength=scale(self.WIDTH - 26), justify="left"
        )
        description.pack(anchor="w")

        win98.Button(body, "Close", self.close, width=64, height=23).pack(
            anchor="e", pady=(scale(12), 0)
        )

        # Escape closes it, the way the Dropdown popup's own grab does.
        self.window.root.bind("<Escape>", lambda _event: self.close())

        # Anything that closes this window from outside Tk must still
        # come through here. Destroyed behind our back, the caller goes
        # on believing the dialog is open and never offers it again.
        self.window.root.protocol("WM_DELETE_WINDOW", self.close)

        self.window.fit(self.WIDTH)
        self.window.show_modal()

    def close(self) -> None:
        if self._closed:
            return

        self._closed = True

        try:
            self.window.root.grab_release()
            self.window.root.destroy()
        except tk.TclError:
            pass

        # The parent draws its own title bar from Tk's focus, so without
        # this it stays greyed out as though something else still had
        # the window.
        try:
            self._parent.root.focus_force()
        except tk.TclError:
            pass

        if self._on_closed is not None:
            self._on_closed()


class ScreenCastApp:
    def __init__(self) -> None:
        streaming.clean_stale_directories()

        self.settings = settings_store.load()

        self.events: queue.Queue[SessionEvent] = queue.Queue()
        self.session = CastSession(on_event=self.events.put)

        self.devices: list[pychromecast.Chromecast] = []
        self.browser: object = None
        self.microphones: list[AudioDevice] = []
        self.outputs: list[OutputDevice] = []
        self.closing = False
        self.destroyed = False
        self.stats_showing = False
        self.about: AboutDialog | None = None

        self.window = win98.AppWindow(
            TITLE,
            WIDTH,
            HEIGHT,
            on_close=self.close,
            on_about=self._show_about,
        )

        self._build()

        self._tick = self.window.root.after(100, self._drain)

        self._start_discovery()
        self._load_audio_devices()

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        body = self.window.body

        row = tk.Frame(body, bg=win98.FACE)
        row.pack(fill="x", pady=(0, scale(8)))

        self.window.label(row, "Device:").pack(side="left", padx=(0, scale(6)))

        self.device_box = win98.Dropdown(row, width=192)
        self.device_box.pack(side="left")

        self.refresh_button = win98.Button(
            row, "Refresh", self._start_discovery, width=64, height=21
        )

        self.refresh_button.pack(side="left", padx=(scale(6), 0))

        quality_row = tk.Frame(body, bg=win98.FACE)
        quality_row.pack(fill="x", pady=(0, scale(8)))

        self.window.label(quality_row, "Quality:").pack(
            side="left", padx=(0, scale(6))
        )

        self.quality_box = win98.Dropdown(quality_row, width=192)
        self.quality_box.set_values(
            list(streaming.QUALITY_PRESETS),
            selected=streaming.resolve_quality(self.settings.quality),
        )
        self.quality_box.pack(side="left")

        delay_row = tk.Frame(body, bg=win98.FACE)
        delay_row.pack(fill="x", pady=(0, scale(8)))

        self.window.label(delay_row, "Delay:").pack(side="left", padx=(0, scale(6)))

        self.delay_box = win98.Dropdown(delay_row, width=192)
        self.delay_box.set_values(
            list(streaming.LATENCY_PRESETS),
            selected=streaming.resolve_latency(self.settings.latency),
        )
        self.delay_box.pack(side="left")

        group = win98.GroupBox(body, "Audio")
        group.pack(fill="x", pady=(0, scale(8)))

        self.system_audio = tk.BooleanVar(value=self.settings.system_audio)
        self.use_microphone = tk.BooleanVar(value=bool(self.settings.microphone))

        self.system_check = win98.Checkbox(
            group.content,
            "System audio",
            self.system_audio,
            command=self._update_controls,
            width=300,
        )

        self.system_check.pack(anchor="w")

        self.output_box = win98.Dropdown(group.content, width=300)
        self.output_box.pack(anchor="w", pady=(scale(3), scale(6)))

        self.mic_check = win98.Checkbox(
            group.content,
            MIC_WARNING,
            self.use_microphone,
            command=self._update_controls,
            width=300,
        )

        self.mic_check.pack(anchor="w")

        self.mic_box = win98.Dropdown(group.content, width=300)
        self.mic_box.pack(anchor="w", pady=(scale(3), 0))

        # Built with everything else but left unpacked: there is nothing
        # to report until a cast is running, and the window is sized for
        # its absence.
        self.stats_group = win98.GroupBox(body, "Stats")
        self.stats_lines: list[tk.Label] = []

        for text in WAITING_FOR_STATS:
            line = self.window.label(self.stats_group.content, text)
            line.pack(anchor="w")

            self.stats_lines.append(line)

        self.status = win98.StatusField(body, height=36)
        self.status.pack(fill="x", pady=(0, scale(10)))

        self.start_button = win98.Button(
            body, "Start casting", self._toggle, width=118, height=23
        )

        self.start_button.pack()

    def _update_controls(self) -> None:
        running = self.session.is_running

        self.device_box.set_enabled(not running and bool(self.devices))
        self.refresh_button.set_enabled(not running)
        self.quality_box.set_enabled(not running)
        self.delay_box.set_enabled(not running)

        self.system_check.set_enabled(not running)
        self.output_box.set_enabled(
            not running and self.system_audio.get() and bool(self.outputs)
        )

        self.mic_check.set_enabled(not running)
        self.mic_box.set_enabled(
            not running and self.use_microphone.get() and bool(self.microphones)
        )

        self.start_button.set_enabled(running or bool(self.devices))
        self.start_button.set_text("Stop casting" if running else "Start casting")

    def _show_about(self) -> None:
        # The dialog's own grab should make a second one impossible, but
        # a window this one cannot dismiss is not a failure worth
        # risking on that. A stale reference to one that went away
        # without telling us would be worse still: the button would
        # simply stop working for the rest of the session.
        if self.about is not None:
            if self.about.window.root.winfo_exists():
                self.about.window.root.lift()
                return

            self.about = None

        self.about = AboutDialog(self.window, on_closed=self._about_closed)

    def _about_closed(self) -> None:
        self.about = None

    def _stats_height(self) -> int:
        """Ask the panel how much room it wants, in unscaled units.

        Text height follows the font, which follows the monitor, so a
        constant that looked right on one screen leaves a band of empty
        grey — or a Start button pushed off the bottom — on another.
        """
        self.stats_group.update_idletasks()

        return math.ceil(
            (self.stats_group.winfo_reqheight() + scale(8)) / scale()
        )

    def _show_stats(self, showing: bool) -> None:
        """Make room first, then fill it — and the reverse going back.

        Packing the panel into a window that has not grown yet squeezes
        every other control, which reads as a flinch.
        """
        if showing == self.stats_showing:
            return

        self.stats_showing = showing

        if showing:
            self.window.resize(WIDTH, HEIGHT + self._stats_height())
            self.stats_group.pack(fill="x", pady=(0, scale(8)), before=self.status)
        else:
            self.stats_group.pack_forget()
            self.window.resize(WIDTH, HEIGHT)

    def _refresh_stats(self) -> None:
        if not self.stats_showing:
            return

        for line, text in zip(self.stats_lines, format_stats(self.session.stats)):
            line.configure(text=text)

    # ----------------------------------------------------------- discovery

    def _start_discovery(self) -> None:
        if self.session.is_running:
            return

        self.status.set_text("Searching for Cast devices...")
        self.refresh_button.set_enabled(False)
        self.start_button.set_enabled(False)

        threading.Thread(target=self._discover, daemon=True).start()

    def _discover(self) -> None:
        try:
            devices, browser = discovery.discover_devices()
        except Exception as error:
            self._post(lambda error=error: self._discovery_failed(error))
            return

        self._post(lambda: self._devices_found(devices, browser))

    def _devices_found(
        self,
        devices: list[pychromecast.Chromecast],
        browser: object,
    ) -> None:
        self._stop_browser()

        self.devices = devices
        self.browser = browser

        labels = [discovery.device_label(device) for device in devices]

        self.device_box.set_values(labels, selected=self.settings.device)
        self.status.set_text(f"Found {len(labels)} device(s). Ready.")

        self._update_controls()

    def _discovery_failed(self, error: Exception) -> None:
        self.devices = []
        self.device_box.set_values([])
        self.status.set_text(str(error))

        self._update_controls()

    def _load_audio_devices(self) -> None:
        threading.Thread(target=self._find_audio_devices, daemon=True).start()

    def _find_audio_devices(self) -> None:
        outputs = audio.list_loopback_devices()
        microphones = audio.list_microphones()

        self._post(lambda: self._audio_devices_found(outputs, microphones))

    def _audio_devices_found(
        self,
        outputs: list[OutputDevice],
        microphones: list[AudioDevice],
    ) -> None:
        # The default endpoint first, since that is what most playback
        # actually goes to.
        self.outputs = sorted(outputs, key=lambda device: not device.is_default)
        self.microphones = microphones

        self.output_box.set_values(
            [device.label for device in self.outputs],
            selected=self.settings.audio_device,
        )

        self.mic_box.set_values(
            [device.label for device in microphones],
            selected=self.settings.microphone,
        )

        self._update_controls()

    # ------------------------------------------------------------- casting

    def _selected_device(self) -> pychromecast.Chromecast | None:
        label = self.device_box.get()

        for device in self.devices:
            if discovery.device_label(device) == label:
                return device

        return None

    def _selected_output(self) -> OutputDevice | None:
        label = self.output_box.get()

        for device in self.outputs:
            if device.label == label:
                return device

        return None

    def _selected_microphone(self) -> AudioDevice | None:
        if not self.use_microphone.get():
            return None

        label = self.mic_box.get()

        for device in self.microphones:
            if device.label == label:
                return device

        return None

    def _toggle(self) -> None:
        if self.session.is_running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        device = self._selected_device()

        if device is None:
            self.status.set_text("Choose a device first.")
            return

        self._remember()

        self.session.start(
            CastOptions(
                device=device,
                port=self.settings.port,
                quality=self.quality_box.get(),
                latency=self.delay_box.get(),
                system_audio=self.system_audio.get(),
                audio_device=self._selected_output(),
                microphone=self._selected_microphone(),
            )
        )

        self._show_stats(True)
        self._update_controls()

    def _stop(self) -> None:
        self.status.set_text("Stopping cast...")
        self.start_button.set_enabled(False)

        # stop() joins the worker, which can take seconds; the UI thread
        # must stay free to keep draining events in the meantime.
        threading.Thread(target=self.session.stop, daemon=True).start()

    def _drain(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break

            self.status.set_text(event.message)

            if event.state in (SessionState.IDLE, SessionState.FAILED):
                self._show_stats(False)
                self._update_controls()
            elif event.state is SessionState.CASTING:
                self._update_controls()

        self._refresh_stats()

        self._tick = (
            self.window.root.after(100, self._drain) if not self.closing else None
        )

    # ------------------------------------------------------------ lifetime

    def _post(self, action: object) -> None:
        if not self.closing:
            self.window.root.after(0, action)

    def _remember(self) -> None:
        self.settings.device = self.device_box.get()
        self.settings.quality = self.quality_box.get()
        self.settings.latency = self.delay_box.get()
        self.settings.system_audio = self.system_audio.get()
        self.settings.audio_device = self.output_box.get()
        self.settings.microphone = (
            self.mic_box.get() if self.use_microphone.get() else ""
        )

        settings_store.save(self.settings)

    def _stop_browser(self) -> None:
        if self.browser is not None:
            try:
                self.browser.stop_discovery()
            except Exception:
                pass

            self.browser = None

    def _destroy(self) -> None:
        if self.destroyed:
            return

        self.destroyed = True

        # A tick still queued against a window that is about to go away
        # fires into nothing and Tk complains about it afterwards.
        if self._tick is not None:
            try:
                self.window.root.after_cancel(self._tick)
            except tk.TclError:
                pass

            self._tick = None

        try:
            self.window.root.destroy()
        except tk.TclError:
            pass

    def close(self) -> None:
        if self.closing:
            return

        self.closing = True

        self._remember()
        self.status.set_text("Closing...")
        self.start_button.set_enabled(False)

        def shutdown() -> None:
            self.session.stop(timeout=CLOSE_GRACE)
            self._stop_browser()

            try:
                self.window.root.after(0, self._destroy)
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=shutdown, daemon=True).start()

        # A teardown that stalls must not trap the window open. Waiting
        # is only a courtesy: ffmpeg is held in a job object and dies
        # with this process, and stale segment directories are swept on
        # the next launch.
        self.window.root.after(int(CLOSE_GRACE * 1000) + 500, self._destroy)

    def run(self) -> None:
        self._update_controls()
        self.window.run()


def selftest() -> int:
    """Prove a frozen build can still reach its dependencies.

    A one-file exe silently loses bundled DLLs, and the first symptom is
    otherwise a window that opens with an empty device list.
    """
    report = Path(tempfile.gettempdir()) / "screencast_selftest.txt"

    lines = [
        f"version        : {__version__}",
        f"frozen         : {getattr(sys, 'frozen', False)}",
    ]
    failures = []

    try:
        outputs = audio.list_loopback_devices()
        microphones = audio.list_microphones()

        lines.append(f"ffmpeg on PATH : {streaming.ffmpeg_available()}")
        lines.append(f"output devices : {len(outputs)}")
        lines.append(f"microphones    : {len(microphones)}")

        if not outputs:
            failures.append("no loopback devices found (portaudio missing?)")
    except Exception as error:
        failures.append(f"{type(error).__name__}: {error}")

    lines += [f"FAIL: {failure}" for failure in failures]
    lines.append("OK" if not failures else "FAILED")

    report.write_text("\n".join(lines), encoding="utf-8")

    return 1 if failures else 0


def main() -> None:
    if "--selftest" in sys.argv:
        sys.exit(selftest())

    ScreenCastApp().run()


if __name__ == "__main__":
    main()
