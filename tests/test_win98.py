"""Widget behaviour that is easy to break and invisible in a screenshot."""

from __future__ import annotations

import ctypes
import tkinter as tk
import unittest
from ctypes import wintypes
from types import SimpleNamespace

from castlib import win98


def pump(root: tk.Tk, times: int = 25) -> None:
    """Let Tk deliver queued events, including focus changes."""
    for _ in range(times):
        root.update()


def window_rect(window: tk.Misc) -> wintypes.RECT:
    """Ask Windows where a window actually is and how big it actually is.

    Tk reports the geometry it was asked for, not the geometry it got:
    a frameless Toplevel can sit at 0,0 while winfo_rootx() insists it
    is exactly where it was placed.
    """
    window.update_idletasks()

    user32 = ctypes.windll.user32
    handle = user32.GetParent(window.winfo_id()) or window.winfo_id()

    rect = wintypes.RECT()
    user32.GetWindowRect(handle, ctypes.byref(rect))

    return rect


def real_position(window: tk.Misc) -> tuple[int, int]:
    rect = window_rect(window)

    return rect.left, rect.top


def real_size(window: tk.Misc) -> tuple[int, int]:
    rect = window_rect(window)

    return rect.right - rect.left, rect.bottom - rect.top


def handle(window: tk.Misc) -> int:
    user32 = ctypes.windll.user32

    return user32.GetParent(window.winfo_id()) or window.winfo_id()


def owner_of(window: tk.Misc) -> int:
    """Which window Windows thinks this one belongs to.

    Tk will not tell you: transient() on a frameless Toplevel leaves the
    owner at zero, and nothing in Tk reports it.
    """
    GW_OWNER = 4

    get_window = ctypes.windll.user32.GetWindow
    get_window.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    get_window.restype = ctypes.c_void_p

    return get_window(handle(window), GW_OWNER) or 0


def above(window: tk.Misc, other: tk.Misc) -> bool:
    """Is this window in front of that one?"""
    user32 = ctypes.windll.user32

    get_next = user32.GetWindow
    get_next.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    get_next.restype = ctypes.c_void_p

    GW_HWNDNEXT = 2
    target = handle(other)
    walker = handle(window)

    while walker:
        walker = get_next(walker, GW_HWNDNEXT)

        if walker == target:
            return True

    return False


class DropdownPopup(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry("400x150+300+300")

        win98.configure(self.root)

        self.dropdown = win98.Dropdown(self.root, width=180)
        self.dropdown.set_values(["Alpha", "Beta", "Gamma"])
        self.dropdown.pack(padx=20, pady=20)

        # The bug only appears in a window that genuinely holds focus:
        # without this the focus events never fire and the test passes
        # while the real dropdown is broken.
        self.root.update()
        self.root.focus_force()

        pump(self.root)

    def tearDown(self) -> None:
        self.root.destroy()

    def test_popup_stays_open_after_clicking(self) -> None:
        """Focusing the list fires FocusOut on the popup that contains it.

        Dismissing on FocusOut therefore closed the popup the instant it
        appeared, which looked like the dropdown doing nothing at all.
        """
        self.dropdown._on_click(None)

        pump(self.root)

        self.assertIsNotNone(
            self.dropdown._popup,
            "popup closed itself immediately after opening",
        )

    def test_popup_lands_under_the_field(self) -> None:
        """A popup stranded at 0,0 looks exactly like one that never opened."""
        self.dropdown._on_click(None)

        pump(self.root)

        left, top = real_position(self.dropdown._popup)

        self.assertNotEqual(
            (left, top), (0, 0), "popup was stranded in the screen corner"
        )

        self.assertAlmostEqual(left, self.dropdown.winfo_rootx(), delta=4)
        self.assertAlmostEqual(
            top,
            self.dropdown.winfo_rooty() + self.dropdown.winfo_height(),
            delta=6,
        )

    def test_clicking_again_closes_it(self) -> None:
        self.dropdown._on_click(None)
        pump(self.root)

        self.dropdown._on_click(None)
        pump(self.root)

        self.assertIsNone(self.dropdown._popup)

    def test_escape_closes_it(self) -> None:
        self.dropdown._on_click(None)
        pump(self.root)

        self.dropdown._popup.event_generate("<Escape>")
        pump(self.root)

        self.assertIsNone(self.dropdown._popup)

    def test_choosing_a_value_updates_the_field(self) -> None:
        self.dropdown._on_click(None)
        pump(self.root)

        listbox = self.dropdown._listbox

        listbox.selection_clear(0, "end")
        listbox.selection_set(2)

        self.dropdown._choose(listbox)
        pump(self.root)

        self.assertEqual(self.dropdown.get(), "Gamma")
        self.assertIsNone(self.dropdown._popup)

    def test_disabled_dropdown_does_not_open(self) -> None:
        self.dropdown.set_enabled(False)

        self.dropdown._on_click(None)
        pump(self.root)

        self.assertIsNone(self.dropdown._popup)

    def test_empty_dropdown_does_not_open(self) -> None:
        self.dropdown.set_values([])

        self.dropdown._on_click(None)
        pump(self.root)

        self.assertIsNone(self.dropdown._popup)


class WindowResizing(unittest.TestCase):
    """The stats panel is only worth packing if the window makes room.

    A frameless window has no OS chrome to negotiate the change with,
    so this is asserted against what Windows says the window became,
    not against the geometry Tk was handed.
    """

    WIDTH = 300
    HEIGHT = 200
    TALLER = 280

    def setUp(self) -> None:
        self.window = win98.AppWindow("Resize test", self.WIDTH, self.HEIGHT)

        pump(self.window.root)

    def tearDown(self) -> None:
        self.window.root.destroy()

    def test_growing_reaches_the_real_window(self) -> None:
        self.window.resize(self.WIDTH, self.TALLER)

        pump(self.window.root)

        width, height = real_size(self.window.root)

        self.assertAlmostEqual(width, win98.scale(self.WIDTH), delta=2)
        self.assertAlmostEqual(height, win98.scale(self.TALLER), delta=2)

    def test_shrinking_puts_it_back(self) -> None:
        self.window.resize(self.WIDTH, self.TALLER)
        pump(self.window.root)

        self.window.resize(self.WIDTH, self.HEIGHT)
        pump(self.window.root)

        _, height = real_size(self.window.root)

        self.assertAlmostEqual(height, win98.scale(self.HEIGHT), delta=2)

    def test_resizing_does_not_move_the_window(self) -> None:
        """Growing must not also teleport the window back to centre."""
        self.window.root.geometry("+220+140")
        pump(self.window.root)

        before = real_position(self.window.root)

        self.window.resize(self.WIDTH, self.TALLER)
        pump(self.window.root)

        self.assertEqual(real_position(self.window.root), before)

    def test_the_body_grows_with_it(self) -> None:
        """A window that reports the new size but does not re-lay-out
        would show the panel over the top of the controls."""
        before = self.window.body.winfo_height()

        self.window.resize(self.WIDTH, self.TALLER)
        pump(self.window.root)

        self.assertGreater(self.window.body.winfo_height(), before)


class TitleBarButtons(unittest.TestCase):
    """Three optional glyph buttons, and the space an absent one leaves."""

    def setUp(self) -> None:
        self.fired: list[str] = []

        self.window = win98.AppWindow(
            "Buttons",
            300,
            200,
            on_close=lambda: self.fired.append("close"),
            on_about=lambda: self.fired.append("about"),
        )

        pump(self.window.root)

        self.bar = self.window.titlebar

    def tearDown(self) -> None:
        self.window.root.destroy()

    def centre_of(self, name: str) -> SimpleNamespace:
        x0, y0, x1, y1 = self.bar._button_boxes()[name]

        return SimpleNamespace(x=(x0 + x1) // 2, y=(y0 + y1) // 2)

    def click(self, name: str) -> None:
        where = self.centre_of(name)

        self.bar.event_generate("<Button-1>", x=where.x, y=where.y)
        self.bar.event_generate("<ButtonRelease-1>", x=where.x, y=where.y)

        pump(self.window.root)

    def test_the_main_window_offers_all_three(self) -> None:
        self.assertEqual(
            set(self.bar._button_boxes()), {"close", "minimize", "about"}
        )

    def test_close_stays_flush_right(self) -> None:
        boxes = self.bar._button_boxes()

        self.assertEqual(
            boxes["close"][0], max(box[0] for box in boxes.values())
        )

    def test_help_sits_outside_minimize(self) -> None:
        boxes = self.bar._button_boxes()

        self.assertLess(boxes["about"][0], boxes["minimize"][0])

    def test_none_of_them_overlap(self) -> None:
        boxes = sorted(self.bar._button_boxes().values())

        for left, right in zip(boxes, boxes[1:]):
            self.assertLessEqual(left[2], right[0], f"{left} runs into {right}")

    def test_the_help_button_runs_its_callback(self) -> None:
        self.click("about")

        self.assertEqual(self.fired, ["about"])

    def test_the_close_button_still_runs_its_own(self) -> None:
        self.click("close")

        self.assertEqual(self.fired, ["close"])

    def test_a_window_given_no_about_has_no_third_button(self) -> None:
        self.bar._handlers["about"] = None

        self.assertNotIn("about", self.bar._button_boxes())

    def test_the_space_an_absent_button_would_take_stays_draggable(self) -> None:
        """A button that is not drawn but still answers _hit swallows
        the click, leaving a dead strip the window cannot be dragged by
        and no visible reason why."""
        where = self.centre_of("about")

        self.bar._handlers["about"] = None

        self.assertIsNone(self.bar._hit(where))

    def test_every_glyph_has_a_button_and_the_reverse(self) -> None:
        self.assertEqual(set(self.bar.GLYPHS), set(self.bar._handlers))


class DialogWindows(unittest.TestCase):
    """A second window is a Toplevel, not a second Tk root."""

    def setUp(self) -> None:
        self.main = win98.AppWindow("Main", 300, 200)
        self.main.root.geometry("+200+150")

        pump(self.main.root)

        self.dialog = win98.AppWindow("Dialog", 200, 100, parent=self.main.root)

        pump(self.main.root)

    def tearDown(self) -> None:
        self.main.root.destroy()

    def test_a_parent_gives_a_toplevel(self) -> None:
        """Only one real root belongs to a process."""
        self.assertIsInstance(self.dialog.root, tk.Toplevel)
        self.assertNotIsInstance(self.dialog.root, tk.Tk)

    def test_a_dialog_has_nowhere_to_minimize_to(self) -> None:
        self.assertNotIn("minimize", self.dialog.titlebar._button_boxes())
        self.assertIn("minimize", self.main.titlebar._button_boxes())

    def test_it_stays_hidden_until_it_is_shown(self) -> None:
        """Positioning a frameless Toplevel before it is mapped does
        not stick, so it is built out of sight first."""
        self.assertEqual(self.dialog.root.state(), "withdrawn")

    def test_showing_it_takes_the_grab(self) -> None:
        self.dialog.show_modal()

        pump(self.main.root)

        self.assertEqual(
            str(self.main.root.grab_current()), str(self.dialog.root)
        )

    def test_it_centres_over_its_parent_rather_than_the_screen(self) -> None:
        self.dialog.show_modal()

        pump(self.main.root)

        parent, parent_size = real_position(self.main.root), real_size(self.main.root)
        child, child_size = real_position(self.dialog.root), real_size(self.dialog.root)

        self.assertAlmostEqual(
            child[0] + child_size[0] // 2,
            parent[0] + parent_size[0] // 2,
            delta=2,
        )
        self.assertAlmostEqual(
            child[1] + child_size[1] // 2,
            parent[1] + parent_size[1] // 2,
            delta=2,
        )

    def test_windows_is_told_who_the_dialog_belongs_to(self) -> None:
        """transient() alone leaves the owner at zero, and an unowned
        modal sinks behind its parent the moment anything raises it —
        still grabbing, with nothing visible to dismiss."""
        self.dialog.show_modal()

        pump(self.main.root)

        self.assertEqual(owner_of(self.dialog.root), handle(self.main.root))

    def test_it_stays_in_front_when_the_parent_is_raised(self) -> None:
        self.dialog.show_modal()

        pump(self.main.root)

        ctypes.windll.user32.BringWindowToTop(handle(self.main.root))

        pump(self.main.root)

        self.assertTrue(
            above(self.dialog.root, self.main.root),
            "the parent covered its own modal dialog",
        )

    def test_it_stays_on_screen_when_the_parent_is_in_a_corner(self) -> None:
        """The main window is frameless and drags without constraint,
        so it can sit somewhere a dialog centred on it would open right
        off the edge — while holding every click."""
        left, top, right, bottom = win98.work_area(self.main.root)

        self.main.root.geometry(f"+{right - win98.scale(40)}+{bottom - win98.scale(40)}")

        pump(self.main.root)

        self.dialog.show_modal()

        pump(self.main.root)

        x, y = real_position(self.dialog.root)
        width, height = real_size(self.dialog.root)

        self.assertGreaterEqual(x, left)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(x + width, right)
        self.assertLessEqual(y + height, bottom)

    def test_it_is_placed_before_it_is_ever_shown(self) -> None:
        """Mapped first and positioned after, it is genuinely on screen
        in the corner for an event loop pass before it jumps to where it
        belongs — Tk maps a frameless Toplevel the moment anything asks
        it to settle, which centring does."""
        visible: list[bool] = []
        original = win98.AppWindow.center

        def watched(window: win98.AppWindow) -> None:
            visible.append(
                bool(ctypes.windll.user32.IsWindowVisible(handle(window.root)))
            )

            original(window)

        win98.AppWindow.center = watched

        try:
            self.dialog.show_modal()

            pump(self.main.root)
        finally:
            win98.AppWindow.center = original

        self.assertEqual(
            visible, [False], "the dialog was on screen before it was placed"
        )

    def test_fit_takes_the_height_the_contents_need(self) -> None:
        """A dialog pinned to a constant clips its own buttons on a
        screen whose font comes out taller."""
        for index in range(6):
            self.dialog.label(self.dialog.body, f"Line {index}").pack(anchor="w")

        self.dialog.fit(200)
        self.dialog.show_modal()

        pump(self.main.root)

        _, height = real_size(self.dialog.root)

        self.assertGreater(height, win98.scale(100), "did not grow at all")
        self.assertGreaterEqual(height, self.dialog.root.winfo_reqheight())


class Scaling(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)

    def tearDown(self) -> None:
        self.root.destroy()

    def test_scale_is_a_whole_number(self) -> None:
        """Fractional scaling smears the one-pixel bevels."""
        factor = win98.configure(self.root)

        self.assertGreaterEqual(factor, 1)
        self.assertEqual(factor, int(factor))

    def test_dpi_does_not_come_from_tk(self) -> None:
        """Tk reports a flat 96 on Windows whatever the monitor does."""
        self.assertGreater(win98.system_dpi(self.root), 0)


if __name__ == "__main__":
    unittest.main()
