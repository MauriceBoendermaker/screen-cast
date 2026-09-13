"""The version number, and the one place that shows it."""

from __future__ import annotations

import contextlib
import io
import sys
import tkinter as tk
import unittest

import cast_screen
import screen_cast_gui as gui
from castlib import __version__, win98


def pump(root: tk.Tk, times: int = 25) -> None:
    for _ in range(times):
        root.update()


def label_texts(widget: tk.Misc) -> list[str]:
    found = []

    for child in widget.winfo_children():
        if isinstance(child, tk.Label):
            found.append(child.cget("text"))

        found += label_texts(child)

    return found


class Version(unittest.TestCase):
    """One string, so "was this the build with the fix" has one answer."""

    def parse(self, argv: list[str]) -> object:
        original = sys.argv
        sys.argv = ["cast_screen.py", *argv]

        try:
            return cast_screen.parse_args()
        finally:
            sys.argv = original

    def test_it_looks_like_a_version(self) -> None:
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_the_command_line_reports_it(self) -> None:
        printed = io.StringIO()

        with contextlib.redirect_stdout(printed):
            with self.assertRaises(SystemExit) as caught:
                self.parse(["--version"])

        self.assertEqual(caught.exception.code, 0)
        self.assertIn(__version__, printed.getvalue())

    def test_asking_for_it_does_not_disturb_the_other_arguments(self) -> None:
        self.assertEqual(self.parse(["--port", "9000"]).port, 9000)


class About(unittest.TestCase):
    def setUp(self) -> None:
        self.main = win98.AppWindow("Main", gui.WIDTH, gui.HEIGHT)

        # Focus only changes hands in a window that genuinely holds it,
        # the same trap the Dropdown tests document: without this the
        # parent never had focus to lose and the title bar assertions
        # below pass whatever the dialog does.
        self.main.root.update()
        self.main.root.focus_force()

        pump(self.main.root)

        self.closed: list[bool] = []
        self.dialog = gui.AboutDialog(
            self.main, on_closed=lambda: self.closed.append(True)
        )

        pump(self.main.root)

    def tearDown(self) -> None:
        self.main.root.destroy()

    def test_it_shows_the_version(self) -> None:
        """The title bar deliberately does not, so this is the only
        place left to read it."""
        self.assertTrue(
            any(__version__ in text for text in label_texts(self.dialog.window.body)),
            label_texts(self.dialog.window.body),
        )

    def test_it_names_the_app_and_says_what_it_does(self) -> None:
        texts = label_texts(self.dialog.window.body)

        self.assertIn(gui.TITLE, texts)
        self.assertIn(gui.DESCRIPTION, texts)

    def test_it_holds_the_parent_until_it_goes(self) -> None:
        self.assertEqual(
            str(self.main.root.grab_current()), str(self.dialog.window.root)
        )

    def test_closing_it_hands_the_parent_back(self) -> None:
        self.dialog.close()

        pump(self.main.root)

        self.assertIsNone(self.main.root.grab_current())
        self.assertEqual(self.closed, [True])

    def test_escape_closes_it(self) -> None:
        self.dialog.window.root.event_generate("<Escape>")

        pump(self.main.root)

        self.assertEqual(self.closed, [True])

    def test_the_close_button_closes_it(self) -> None:
        buttons = [
            child
            for child in self.dialog.window.body.winfo_children()
            if isinstance(child, win98.Button)
        ]

        self.assertEqual(len(buttons), 1, "expected exactly one Close button")

        buttons[0].event_generate("<Button-1>", x=5, y=5)
        buttons[0].event_generate("<ButtonRelease-1>", x=5, y=5)

        pump(self.main.root)

        self.assertEqual(self.closed, [True])

    def test_closing_twice_only_counts_once(self) -> None:
        """Escape and the Close button can both land on a slow click."""
        self.dialog.close()
        self.dialog.close()

        pump(self.main.root)

        self.assertEqual(self.closed, [True])

    def test_it_takes_the_parents_title_bar_while_it_is_open(self) -> None:
        self.assertFalse(self.main.titlebar._active)

    def test_closing_it_gives_the_parent_its_focus_back(self) -> None:
        """The main window draws its own title bar from Tk's focus, so
        without handing it back it stays greyed out as though something
        else still had the window."""
        self.dialog.close()

        pump(self.main.root)

        self.assertTrue(self.main.titlebar._active)

    def test_a_dialog_destroyed_from_outside_still_reports_back(self) -> None:
        """Otherwise the caller keeps a dead window and never offers
        About again for the rest of the session."""
        handler = self.dialog.window.root.protocol("WM_DELETE_WINDOW")

        self.assertTrue(handler, "nothing is listening for an outside close")

        self.dialog.window.root.tk.call(handler)

        pump(self.main.root)

        self.assertEqual(self.closed, [True])
        self.assertIsNone(self.main.root.grab_current())


class AboutFromTheWindow(unittest.TestCase):
    """The ? button, as the app itself wires it up."""

    def setUp(self) -> None:
        gui.ScreenCastApp._start_discovery = lambda app: None
        gui.ScreenCastApp._load_audio_devices = lambda app: None

        self.app = gui.ScreenCastApp()

        pump(self.app.window.root)

    def tearDown(self) -> None:
        self.app.closing = True
        self.app._destroy()

    def test_the_question_mark_opens_it(self) -> None:
        self.app.window.titlebar._handlers["about"]()

        pump(self.app.window.root)

        self.assertIsNotNone(self.app.about)

    def test_closing_it_lets_it_be_opened_again(self) -> None:
        self.app._show_about()
        pump(self.app.window.root)

        self.app.about.close()
        pump(self.app.window.root)

        self.assertIsNone(self.app.about)

        self.app._show_about()
        pump(self.app.window.root)

        self.assertIsNotNone(self.app.about)

    def test_a_dialog_that_went_away_behind_our_back_is_not_fatal(self) -> None:
        """A stale reference would leave the button dead for the rest
        of the session, and throw into the Tk callback each time."""
        self.app._show_about()
        pump(self.app.window.root)

        self.app.about.window.root.destroy()

        pump(self.app.window.root)

        self.app._show_about()

        pump(self.app.window.root)

        self.assertTrue(self.app.about.window.root.winfo_exists())


if __name__ == "__main__":
    unittest.main()
