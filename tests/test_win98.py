"""Widget behaviour that is easy to break and invisible in a screenshot."""

from __future__ import annotations

import ctypes
import tkinter as tk
import unittest
from ctypes import wintypes

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
