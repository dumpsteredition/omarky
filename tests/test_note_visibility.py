from __future__ import annotations

import io
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cairo

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import canvas  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


class TestMonitor:
    def __init__(self, monitor) -> None:
        self.geometry = monitor.get_geometry()

    def get_geometry(self):
        return self.geometry

    def get_number(self) -> int:
        return 0


def png_image() -> bytes:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 24, 16)
    stream = io.BytesIO()
    surface.write_to_png(stream)
    return stream.getvalue()


class NoteVisibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        initialized, _argv = Gtk.init_check(None)
        if not initialized:
            raise RuntimeError("GTK display initialization failed")
        display = Gdk.Display.get_default()
        if display is None:
            raise RuntimeError("GTK display is unavailable")
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            raise RuntimeError("GTK monitor is unavailable")
        cls.monitor = TestMonitor(monitor)

    def _new_overlay(self, note: str, *, practice: bool) -> canvas.AnnotateOverlay:
        with patch.object(canvas, "GtkLayerShell", None):
            overlay = canvas.AnnotateOverlay(
                png_image(), "", lambda *_args: {"status": "ok"}, None,
                self.monitor, GLib.MainLoop.new(None, False),
                practice=practice, note=note,
            )
        self.addCleanup(overlay.window.destroy)
        return overlay

    def _wait_until(self, predicate) -> None:
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            if predicate():
                return
            time.sleep(0.01)
        self.fail("GTK did not map/focus the note entry in time")

    def test_initial_visibility_mapped_focus_and_text_survive_toggle(self) -> None:
        # Practice annotations with an existing note start expanded and visible.
        practice = self._new_overlay("practice note", practice=True)
        self.assertFalse(practice.note_row.get_no_show_all())
        self.assertTrue(practice.note_row.get_visible())
        self.assertTrue(practice.note_entry.get_visible())
        self.assertEqual(practice.note_entry.get_text(), "practice note")
        self._wait_until(practice.note_entry.get_mapped)

        practice.on_note_toggle_clicked(practice.note_toggle_button)
        self.assertFalse(practice.note_row.get_visible())
        self.assertEqual(practice.note_entry.get_text(), "practice note")
        practice.on_note_toggle_clicked(practice.note_toggle_button)
        self._wait_until(lambda: (practice.note_entry.get_mapped()
                                  and practice.window.get_focus() is practice.note_entry))
        self.assertEqual(practice.note_entry.get_text(), "practice note")
        practice.window.destroy()

        # An empty routine note starts hidden; expanding maps before taking focus.
        routine = self._new_overlay("", practice=False)
        self.assertFalse(routine.note_row.get_visible())
        routine.on_note_toggle_clicked(routine.note_toggle_button)
        self._wait_until(lambda: (routine.note_entry.get_mapped()
                                  and routine.window.get_focus() is routine.note_entry))
        routine.note_entry.set_text("typed note survives")
        routine.on_note_toggle_clicked(routine.note_toggle_button)
        self.assertFalse(routine.note_row.get_visible())
        routine.on_note_toggle_clicked(routine.note_toggle_button)
        self._wait_until(lambda: (routine.note_entry.get_mapped()
                                  and routine.window.get_focus() is routine.note_entry))
        self.assertEqual(routine.note_entry.get_text(), "typed note survives")


if __name__ == "__main__":
    unittest.main()
