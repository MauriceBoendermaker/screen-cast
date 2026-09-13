"""A small Windows 98 widget set for tkinter.

ttk is deliberately unused: its themes fight explicit colour control, and
the 3D chrome of the era is a two-tone double bevel that no modern theme
engine will reproduce. Plain tk widgets with hand-drawn edges are both
more accurate and simpler to reason about.

Depends on nothing else in the package.
"""

from __future__ import annotations

import ctypes
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Sequence


FACE = "#C0C0C0"
HIGHLIGHT = "#FFFFFF"
LIGHT = "#DFDFDF"
SHADOW = "#808080"
DARK_SHADOW = "#0A0A0A"
FIELD = "#FFFFFF"
TEXT = "#000000"
DISABLED_TEXT = "#808080"
SELECT_BG = "#000080"
SELECT_FG = "#FFFFFF"
TITLE_ACTIVE = ("#000080", "#1084D0")
TITLE_INACTIVE = ("#808080", "#B5B5B5")
TITLE_TEXT = "#FFFFFF"

FONT_CANDIDATES = ("MS Sans Serif", "Microsoft Sans Serif", "Tahoma")

RAISED = "raised"
SUNKEN = "sunken"
PRESSED = "pressed"

# Outer edge first, then inner: (top-left colour, bottom-right colour).
_EDGES = {
    RAISED: ((HIGHLIGHT, DARK_SHADOW), (LIGHT, SHADOW)),
    PRESSED: ((DARK_SHADOW, HIGHLIGHT), (SHADOW, LIGHT)),
    SUNKEN: ((SHADOW, HIGHLIGHT), (DARK_SHADOW, LIGHT)),
}

CHECK_GLYPH = (
    "             ",
    "             ",
    "          #  ",
    "         ##  ",
    "        ###  ",
    "  #    ###   ",
    "  ##  ###    ",
    "  ######     ",
    "   ####      ",
    "    ##       ",
    "             ",
    "             ",
    "             ",
)

_scale = 1
_font_family = "Microsoft Sans Serif"


def enable_dpi_awareness() -> None:
    """Must run before the Tk root exists, or Windows scales us blurrily."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def system_dpi(root: tk.Misc) -> float:
    """Ask Windows for the real DPI.

    Tk reports a flat 96 on Windows whatever the monitor is actually
    doing, so winfo_fpixels() would silently pin the UI to single scale
    on a high-DPI screen.
    """
    root.update_idletasks()

    try:
        user32 = ctypes.windll.user32
        handle = user32.GetParent(root.winfo_id()) or root.winfo_id()
        dpi = user32.GetDpiForWindow(handle)

        if dpi:
            return float(dpi)
    except (AttributeError, OSError):
        pass

    try:
        device = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(device, 88)
        ctypes.windll.user32.ReleaseDC(0, device)

        if dpi:
            return float(dpi)
    except (AttributeError, OSError):
        pass

    return float(root.winfo_fpixels("1i"))


def configure(root: tk.Misc) -> int:
    """Pin the integer UI scale and pick the closest surviving font.

    Fractional scaling would smear the one-pixel bevels these widgets are
    built from, so the factor is rounded to a whole number and every
    metric is a multiple of it.
    """
    global _scale, _font_family

    _scale = max(1, round(system_dpi(root) / 96))

    available = set(tkfont.families(root))

    for candidate in FONT_CANDIDATES:
        if candidate in available:
            _font_family = candidate
            break

    return _scale


def scale(value: int = 1) -> int:
    return value * _scale


def ui_font(size: int = 8, bold: bool = False) -> tuple[str, int, str]:
    return (_font_family, size * _scale, "bold" if bold else "normal")


def draw_bevel(
    canvas: tk.Canvas,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    style: str,
    unit: int | None = None,
) -> None:
    """Paint the era's two-tone double edge around the given box."""
    unit = unit if unit is not None else scale()

    for layer, (top_left, bottom_right) in enumerate(_EDGES[style]):
        offset = layer * unit
        left = x0 + offset
        top = y0 + offset
        right = x1 - offset
        bottom = y1 - offset

        canvas.create_rectangle(
            left, top, right, top + unit, fill=top_left, outline=""
        )
        canvas.create_rectangle(
            left, top, left + unit, bottom, fill=top_left, outline=""
        )
        canvas.create_rectangle(
            left, bottom - unit, right, bottom, fill=bottom_right, outline=""
        )
        canvas.create_rectangle(
            right - unit, top, right, bottom, fill=bottom_right, outline=""
        )


def draw_glyph(
    canvas: tk.Canvas,
    rows: Sequence[str],
    left: int,
    top: int,
    colour: str,
    unit: int | None = None,
    tag: str = "",
) -> None:
    """Paint a pixel-grid glyph, one rectangle per lit pixel."""
    unit = unit if unit is not None else scale()

    for y, row in enumerate(rows):
        for x, pixel in enumerate(row):
            if pixel == " ":
                continue

            canvas.create_rectangle(
                left + x * unit,
                top + y * unit,
                left + (x + 1) * unit,
                top + (y + 1) * unit,
                fill=colour,
                outline="",
                tags=tag,
            )


class Button(tk.Canvas):
    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None] | None = None,
        width: int = 100,
        height: int = 23,
        bold: bool = False,
    ) -> None:
        super().__init__(
            parent,
            width=scale(width),
            height=scale(height),
            bg=FACE,
            highlightthickness=0,
            bd=0,
        )

        self._text = text
        self._command = command
        self._bold = bold
        self._enabled = True
        self._pressed = False

        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Leave>", self._on_leave)

        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")

        width = int(self["width"])
        height = int(self["height"])

        self.create_rectangle(0, 0, width, height, fill=FACE, outline="")
        draw_bevel(self, 0, 0, width, height, PRESSED if self._pressed else RAISED)

        nudge = scale() if self._pressed else 0
        colour = TEXT if self._enabled else DISABLED_TEXT

        if not self._enabled:
            self.create_text(
                width / 2 + scale(),
                height / 2 + scale(),
                text=self._text,
                fill=HIGHLIGHT,
                font=ui_font(bold=self._bold),
            )

        self.create_text(
            width / 2 + nudge,
            height / 2 + nudge,
            text=self._text,
            fill=colour,
            font=ui_font(bold=self._bold),
        )

    def _on_press(self, _event: tk.Event) -> None:
        if not self._enabled:
            return

        self._pressed = True
        self._redraw()

    def _on_release(self, event: tk.Event) -> None:
        if not self._enabled or not self._pressed:
            return

        self._pressed = False
        self._redraw()

        inside = (
            0 <= event.x < int(self["width"]) and 0 <= event.y < int(self["height"])
        )

        if inside and self._command is not None:
            self._command()

    def _on_leave(self, _event: tk.Event) -> None:
        if self._pressed:
            self._pressed = False
            self._redraw()

    def set_text(self, text: str) -> None:
        self._text = text
        self._redraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._redraw()


class Checkbox(tk.Canvas):
    BOX = 13

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        variable: tk.BooleanVar,
        command: Callable[[], None] | None = None,
        width: int = 260,
    ) -> None:
        super().__init__(
            parent,
            width=scale(width),
            height=scale(self.BOX + 2),
            bg=FACE,
            highlightthickness=0,
            bd=0,
        )

        self._text = text
        self._variable = variable
        self._command = command
        self._enabled = True

        self.bind("<Button-1>", self._on_click)
        variable.trace_add("write", lambda *_: self._redraw())

        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")

        box = scale(self.BOX)
        top = scale(1)

        self.create_rectangle(
            0, 0, int(self["width"]), int(self["height"]), fill=FACE, outline=""
        )

        field = FIELD if self._enabled else FACE

        self.create_rectangle(0, top, box, top + box, fill=field, outline="")

        draw_bevel(self, 0, top, box, top + box, SUNKEN)

        if self._variable.get():
            draw_glyph(
                self,
                CHECK_GLYPH,
                0,
                top,
                TEXT if self._enabled else DISABLED_TEXT,
            )

        self.create_text(
            box + scale(6),
            int(self["height"]) / 2,
            text=self._text,
            anchor="w",
            fill=TEXT if self._enabled else DISABLED_TEXT,
            font=ui_font(),
        )

    def _on_click(self, _event: tk.Event) -> None:
        if not self._enabled:
            return

        self._variable.set(not self._variable.get())

        if self._command is not None:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._redraw()


class Dropdown(tk.Canvas):
    """A sunken field plus an arrow button, with a hand-rolled popup list.

    tk.Menu renders natively on Windows and cannot be repainted, so the
    list is an overrideredirect Toplevel instead.
    """

    ARROW = ("#######", " ##### ", "  ###  ", "   #   ")

    def __init__(
        self,
        parent: tk.Misc,
        width: int = 200,
        height: int = 21,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            parent,
            width=scale(width),
            height=scale(height),
            bg=FACE,
            highlightthickness=0,
            bd=0,
        )

        self._values: list[str] = []
        self._selected: str = ""
        self._enabled = True
        self._popup: tk.Toplevel | None = None
        self._listbox: tk.Listbox | None = None
        self._on_change = on_change

        self.bind("<Button-1>", self._on_click)

        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")

        width = int(self["width"])
        height = int(self["height"])
        button = scale(17)

        self.create_rectangle(0, 0, width, height, fill=FACE, outline="")
        self.create_rectangle(
            0, 0, width - button, height,
            fill=FIELD if self._enabled else FACE,
            outline="",
        )

        draw_bevel(self, 0, 0, width - button, height, SUNKEN)

        self.create_text(
            scale(4),
            height / 2,
            text=self._selected,
            anchor="w",
            fill=TEXT if self._enabled else DISABLED_TEXT,
            font=ui_font(),
        )

        self.create_rectangle(
            width - button, 0, width, height, fill=FACE, outline=""
        )

        draw_bevel(self, width - button, 0, width, height, RAISED)

        draw_glyph(
            self,
            self.ARROW,
            width - button + scale(5),
            height // 2 - scale(2),
            TEXT if self._enabled else DISABLED_TEXT,
        )

    def set_values(self, values: list[str], selected: str | None = None) -> None:
        self._values = list(values)

        if selected is not None and selected in self._values:
            self._selected = selected
        elif self._selected not in self._values:
            self._selected = self._values[0] if self._values else ""

        self._redraw()

    def get(self) -> str:
        return self._selected

    def set(self, value: str) -> None:
        self._selected = value
        self._redraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

        if not enabled:
            self._close_popup()

        self._redraw()

    def _on_click(self, _event: tk.Event) -> None:
        if not self._enabled or not self._values:
            return

        if self._popup is not None:
            self._close_popup()
            return

        self._open_popup()

    def _open_popup(self) -> None:
        popup = tk.Toplevel(self)

        # Built hidden, placed, then shown. Positioning a frameless
        # Toplevel before it is mapped is unreliable.
        popup.withdraw()
        popup.overrideredirect(True)

        rows = min(len(self._values), 8)

        # The frame is the one-pixel black surround; the list is a real
        # child of it rather than packed in_ a sibling, which left the
        # list unmapped and showed nothing but the black border.
        border = tk.Frame(popup, bg=TEXT)
        border.pack(fill="both", expand=True)

        listbox = tk.Listbox(
            border,
            bg=FIELD,
            fg=TEXT,
            selectbackground=SELECT_BG,
            selectforeground=SELECT_FG,
            font=ui_font(),
            highlightthickness=0,
            bd=0,
            relief="flat",
            activestyle="none",
            height=rows,
            exportselection=False,
        )

        for value in self._values:
            listbox.insert("end", value)

        if self._selected in self._values:
            listbox.selection_set(self._values.index(self._selected))

        listbox.pack(fill="both", expand=True, padx=scale(), pady=scale())

        popup.update_idletasks()

        geometry = (
            f"{int(self['width'])}x{popup.winfo_reqheight()}"
            f"+{self.winfo_rootx()}+{self.winfo_rooty() + int(self['height'])}"
        )

        popup.geometry(geometry)
        popup.deiconify()
        popup.update_idletasks()

        # Mapping discards the position it was just given, and Tk keeps
        # reporting the requested coordinates either way — so the window
        # ends up in the screen corner while winfo_rootx() insists it is
        # under the field. Setting it again after mapping is what sticks.
        popup.geometry(geometry)

        # The list overlaps the window it drops out of, and a frameless
        # Toplevel is not reliably stacked above its parent.
        popup.attributes("-topmost", True)
        popup.lift()

        listbox.bind("<ButtonRelease-1>", lambda _event: self._choose(listbox))
        listbox.bind("<Return>", lambda _event: self._choose(listbox))

        # Escape has to work wherever focus actually landed.
        for widget in (popup, listbox):
            widget.bind("<Escape>", lambda _event: self._close_popup())

        # Dismissing on <FocusOut> cannot work: focusing the list fires
        # FocusOut on the popup that contains it, so the popup closed
        # itself the instant it opened. A grab routes stray clicks here
        # instead, and anything landing outside the bounds dismisses it.
        popup.grab_set()
        popup.bind("<Button-1>", self._click_outside)

        listbox.focus_set()

        self._popup = popup
        self._listbox = listbox

    def _click_outside(self, event: tk.Event) -> None:
        popup = self._popup

        if popup is None:
            return

        left = popup.winfo_rootx()
        top = popup.winfo_rooty()

        inside = (
            left <= event.x_root < left + popup.winfo_width()
            and top <= event.y_root < top + popup.winfo_height()
        )

        if not inside:
            self._close_popup()

    def _choose(self, listbox: tk.Listbox) -> None:
        selection = listbox.curselection()

        if selection:
            self._selected = self._values[selection[0]]
            self._redraw()

            if self._on_change is not None:
                self._on_change(self._selected)

        self._close_popup()

    def _close_popup(self) -> None:
        if self._popup is not None:
            try:
                self._popup.grab_release()
            except tk.TclError:
                pass

            self._popup.destroy()

            self._popup = None
            self._listbox = None


class GroupBox(tk.Frame):
    """An etched rectangle with its label notched into the top edge."""

    def __init__(self, parent: tk.Misc, text: str) -> None:
        super().__init__(parent, bg=FACE)

        self._text = text

        self._canvas = tk.Canvas(self, bg=FACE, highlightthickness=0, bd=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)

        self.content = tk.Frame(self, bg=FACE)
        self.content.pack(
            fill="both",
            expand=True,
            padx=scale(9),
            pady=(scale(14), scale(9)),
        )

        self.bind("<Configure>", lambda _event: self._redraw())

    def _redraw(self) -> None:
        self._canvas.delete("all")

        width = self.winfo_width()
        height = self.winfo_height()
        top = scale(6)
        unit = scale()

        for offset, colour in ((0, SHADOW), (unit, HIGHLIGHT)):
            self._canvas.create_rectangle(
                offset, top + offset, width - unit + offset, top + offset + unit,
                fill=colour, outline="",
            )
            self._canvas.create_rectangle(
                offset, top + offset, offset + unit, height - unit + offset,
                fill=colour, outline="",
            )
            self._canvas.create_rectangle(
                offset, height - unit + offset, width - unit + offset,
                height + offset, fill=colour, outline="",
            )
            self._canvas.create_rectangle(
                width - unit + offset, top + offset, width + offset,
                height - unit + offset, fill=colour, outline="",
            )

        label = self._canvas.create_text(
            scale(9),
            top,
            text=self._text,
            anchor="w",
            fill=TEXT,
            font=ui_font(),
        )

        bounds = self._canvas.bbox(label)

        if bounds:
            self._canvas.create_rectangle(
                bounds[0] - scale(3), bounds[1], bounds[2] + scale(3), bounds[3],
                fill=FACE, outline="",
            )

            self._canvas.tag_raise(label)


class StatusField(tk.Canvas):
    def __init__(self, parent: tk.Misc, width: int = 300, height: int = 40) -> None:
        super().__init__(
            parent,
            width=scale(width),
            height=scale(height),
            bg=FACE,
            highlightthickness=0,
            bd=0,
        )

        self._text = "Idle"

        self.bind("<Configure>", lambda _event: self._redraw())

        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")

        width = self.winfo_width() or int(self["width"])
        height = self.winfo_height() or int(self["height"])

        self.create_rectangle(0, 0, width, height, fill=FIELD, outline="")
        draw_bevel(self, 0, 0, width, height, SUNKEN)

        self.create_text(
            scale(6),
            height / 2,
            text=self._text,
            anchor="w",
            width=width - scale(12),
            fill=TEXT,
            font=ui_font(),
        )

    def set_text(self, text: str) -> None:
        self._text = text
        self._redraw()


class TitleBar(tk.Canvas):
    HEIGHT = 18

    MINIMIZE = ("      ", "      ", "      ", "      ", "######", "######")
    CLOSE = (
        "##    ##",
        "###  ###",
        " ###### ",
        "  ####  ",
        "  ####  ",
        " ###### ",
        "###  ###",
        "##    ##",
    )

    def __init__(
        self,
        parent: tk.Misc,
        window: tk.Tk,
        text: str,
        on_close: Callable[[], None],
        on_minimize: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(
            parent,
            height=scale(self.HEIGHT),
            bg=TITLE_ACTIVE[0],
            highlightthickness=0,
            bd=0,
        )

        self._window = window
        self._text = text
        self._on_close = on_close
        self._on_minimize = on_minimize
        self._active = True
        self._drag: tuple[int, int] | None = None

        self.bind("<Configure>", lambda _event: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

        window.bind("<FocusIn>", lambda _event: self._set_active(True), add="+")
        window.bind("<FocusOut>", lambda _event: self._set_active(False), add="+")

    def _button_boxes(self) -> dict[str, tuple[int, int, int, int]]:
        width = self.winfo_width() or 1
        size = scale(16)
        top = scale(2)
        right = width - scale(2)

        close = (right - size, top, right, top + size)
        minimize = (close[0] - size - scale(2), top, close[0] - scale(2), top + size)

        return {"close": close, "minimize": minimize}

    def _redraw(self) -> None:
        self.delete("all")

        width = self.winfo_width() or 1
        height = scale(self.HEIGHT)
        start, end = TITLE_ACTIVE if self._active else TITLE_INACTIVE

        self._draw_gradient(width, height, start, end)

        self.create_text(
            scale(4),
            height / 2,
            text=self._text,
            anchor="w",
            fill=TITLE_TEXT,
            font=ui_font(bold=True),
        )

        boxes = self._button_boxes()

        for name, (x0, y0, x1, y1) in boxes.items():
            if name == "minimize" and self._on_minimize is None:
                continue

            self.create_rectangle(x0, y0, x1, y1, fill=FACE, outline="")

            draw_bevel(self, x0, y0, x1, y1, RAISED)

            glyph = self.CLOSE if name == "close" else self.MINIMIZE
            glyph_width = len(glyph[0]) * scale()
            glyph_height = len(glyph) * scale()

            draw_glyph(
                self,
                glyph,
                (x0 + x1) // 2 - glyph_width // 2,
                (y0 + y1) // 2 - glyph_height // 2,
                TEXT,
            )

    def _draw_gradient(self, width: int, height: int, start: str, end: str) -> None:
        steps = max(1, width // scale(2))
        red0, green0, blue0 = self.winfo_rgb(start)
        red1, green1, blue1 = self.winfo_rgb(end)

        for step in range(steps):
            ratio = step / max(1, steps - 1)

            colour = "#%02x%02x%02x" % (
                int((red0 + (red1 - red0) * ratio) / 256),
                int((green0 + (green1 - green0) * ratio) / 256),
                int((blue0 + (blue1 - blue0) * ratio) / 256),
            )

            left = int(step * width / steps)
            right = int((step + 1) * width / steps) + 1

            self.create_rectangle(left, 0, right, height, fill=colour, outline="")

    def _hit(self, event: tk.Event) -> str | None:
        for name, (x0, y0, x1, y1) in self._button_boxes().items():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                return name

        return None

    def _on_press(self, event: tk.Event) -> None:
        if self._hit(event) is not None:
            return

        self._drag = (event.x_root, event.y_root)
        self._origin = (self._window.winfo_x(), self._window.winfo_y())

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag is None:
            return

        x = self._origin[0] + (event.x_root - self._drag[0])
        y = self._origin[1] + (event.y_root - self._drag[1])

        self._window.geometry(f"+{x}+{y}")

    def _on_release(self, event: tk.Event) -> None:
        was_dragging = self._drag is not None
        self._drag = None

        if was_dragging:
            return

        target = self._hit(event)

        if target == "close":
            self._on_close()
        elif target == "minimize" and self._on_minimize is not None:
            self._on_minimize()

    def _set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self._redraw()


def show_in_taskbar(window: tk.Tk) -> None:
    """Put an overrideredirect window back in the taskbar and Alt-Tab.

    Dropping the native frame also drops the window's taskbar button,
    which would leave a minimised window with no way back.
    """
    window.update_idletasks()

    GWL_EXSTYLE = -20
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080

    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()

        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW

        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)

        window.withdraw()
        window.after(10, window.deiconify)
    except (AttributeError, OSError):
        pass


def minimize(window: tk.Tk) -> None:
    """iconify() does nothing on an overrideredirect window, so ask Windows."""
    SW_MINIMIZE = 6

    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()

        user32.ShowWindow(hwnd, SW_MINIMIZE)
    except (AttributeError, OSError):
        window.iconify()


class AppWindow:
    """A frameless Tk window wearing the period's chrome.

    Native Windows 11 chrome above a silver dialog would undercut the
    whole effect, so the frame is drawn here instead.
    """

    def __init__(
        self,
        title: str,
        width: int,
        height: int,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        enable_dpi_awareness()

        self.root = tk.Tk()
        configure(self.root)

        self.root.title(title)
        self.root.configure(bg=FACE)
        self.root.overrideredirect(True)
        self.root.geometry(f"{scale(width)}x{scale(height)}")

        self._on_close = on_close or self.root.destroy

        self.outer = tk.Frame(self.root, bg=FACE)
        self.outer.pack(fill="both", expand=True)

        self._frame = tk.Canvas(self.outer, bg=FACE, highlightthickness=0, bd=0)
        self._frame.place(x=0, y=0, relwidth=1, relheight=1)

        self.outer.bind("<Configure>", lambda _event: self._redraw_frame())

        self.titlebar = TitleBar(
            self.outer,
            self.root,
            title,
            self._on_close,
            lambda: minimize(self.root),
        )

        self.titlebar.pack(fill="x", padx=scale(3), pady=(scale(3), 0))

        self.body = tk.Frame(self.outer, bg=FACE)
        self.body.pack(fill="both", expand=True, padx=scale(9), pady=scale(9))

    def _redraw_frame(self) -> None:
        self._frame.delete("all")

        draw_bevel(
            self._frame,
            0,
            0,
            self.outer.winfo_width(),
            self.outer.winfo_height(),
            RAISED,
        )

    def label(self, parent: tk.Misc, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg=FACE, fg=TEXT, font=ui_font())

    def center(self) -> None:
        self.root.update_idletasks()

        width = self.root.winfo_width()
        height = self.root.winfo_height()

        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3

        self.root.geometry(f"+{x}+{y}")

    def run(self) -> None:
        self.center()
        show_in_taskbar(self.root)

        self.root.mainloop()


def demo() -> None:
    """Visual harness: every widget, so the chrome can be eyeballed."""
    window = AppWindow("Widget demo", 320, 260)
    body = window.body

    dropdown = Dropdown(body, width=280)
    dropdown.set_values(["Living Room TV", "Bedroom speaker", "Office display"])
    dropdown.pack(anchor="w", pady=(0, scale(8)))

    group = GroupBox(body, "Audio")
    group.pack(fill="x", pady=(0, scale(8)))

    checked = tk.BooleanVar(value=True)
    unchecked = tk.BooleanVar(value=False)

    Checkbox(group.content, "System audio", checked).pack(anchor="w")
    Checkbox(group.content, "Microphone", unchecked).pack(anchor="w")

    status = StatusField(body, width=280, height=34)
    status.pack(fill="x", pady=(0, scale(8)))
    status.set_text("Ready.")

    Button(body, "Start casting", lambda: status.set_text("Clicked."), width=120).pack()

    window.run()


if __name__ == "__main__":
    demo()
