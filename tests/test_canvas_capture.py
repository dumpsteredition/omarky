from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cairo
import io

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import canvas  # noqa: E402


def png_bytes() -> bytes:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 32, 24)
    context = cairo.Context(surface)
    context.set_source_rgb(0.1, 0.2, 0.3)
    context.paint()
    stream = io.BytesIO()
    surface.write_to_png(stream)
    return stream.getvalue()


class FakeMonitor:
    def __init__(self, number: int, x: int, y: int, width: int, height: int):
        self.number = number
        self.geometry = SimpleNamespace(x=x, y=y, width=width, height=height)

    def get_geometry(self):
        return self.geometry

    def get_number(self):
        return self.number


class FakeDisplay:
    def __init__(self, monitors):
        self.monitors = monitors

    def get_n_monitors(self):
        return len(self.monitors)

    def get_monitor(self, index):
        return self.monitors[index]


class FocusedCaptureTests(unittest.TestCase):
    def test_focused_output_is_selected_by_name_and_capture_uses_only_that_output(self) -> None:
        output = {"name": "DP-3", "x": 1920, "y": 0, "width": 2560,
                  "height": 1440, "scale": 1.5, "focused": True}
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            if command[:2] == ["hyprctl", "-j"]:
                return subprocess.CompletedProcess(command, 0, json.dumps([output]), "")
            if command[0] == "grim":
                return subprocess.CompletedProcess(command, 0, png_bytes(), b"")
            raise AssertionError(f"unexpected command: {command}")

        selected = canvas.read_focused_output(runner)
        image = canvas.read_screenshot(selected, runner)
        self.assertEqual(selected["name"], "DP-3")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(commands[1], ["grim", "-o", "DP-3", "-"])

    def test_single_output_is_safe_but_multi_output_without_focus_is_rejected(self) -> None:
        only = {"name": "eDP-1", "x": 0, "y": 0, "width": 1920,
                "height": 1080, "scale": 1.0}
        chosen = canvas.read_focused_output(lambda *_a, **_kw: subprocess.CompletedProcess(
            ["hyprctl"], 0, json.dumps([only]), ""))
        self.assertEqual(chosen["name"], "eDP-1")

        outputs = [dict(only), {**only, "name": "HDMI-A-1", "x": 1920}]
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            canvas.read_focused_output(lambda *_a, **_kw: subprocess.CompletedProcess(
                ["hyprctl"], 0, json.dumps(outputs), ""))

    def test_multiple_focused_outputs_fail_closed(self) -> None:
        outputs = [
            {"name": "DP-1", "x": 0, "y": 0, "width": 1000, "height": 800,
             "scale": 1, "focused": True},
            {"name": "DP-2", "x": 1000, "y": 0, "width": 1000, "height": 800,
             "scale": 1, "focused": True},
        ]
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            canvas.read_focused_output(lambda *_a, **_kw: subprocess.CompletedProcess(
                ["hyprctl"], 0, json.dumps(outputs), ""))

    def test_unavailable_compositor_does_not_fall_back_to_all_screens(self) -> None:
        def unavailable(*_args, **_kwargs):
            raise FileNotFoundError("hyprctl")

        with self.assertRaisesRegex(RuntimeError, "refusing an all-display capture"):
            canvas.read_focused_output(unavailable)
        with self.assertRaisesRegex(RuntimeError, "refusing an all-display capture"):
            canvas.read_screenshot({}, lambda *_args, **_kwargs: self.fail("grim must not run"))

    def test_routine_prepare_uses_focused_output_only(self) -> None:
        output = {"name": "DP-2", "x": 0, "y": 0, "width": 1920,
                  "height": 1080, "scale": 1, "focused": True}
        png = png_bytes()
        with patch.object(canvas, "read_focused_output", return_value=output) as resolve, \
             patch.object(canvas, "read_screenshot", return_value=png) as capture:
            image, selected = canvas.prepare_source(None)
        resolve.assert_called_once_with()
        capture.assert_called_once_with(output)
        self.assertEqual(image, png)
        self.assertIs(selected, output)

    def test_output_monitor_uses_geometry_not_monitor_list_index(self) -> None:
        left = FakeMonitor(0, 0, 0, 1920, 1080)
        right = FakeMonitor(1, 1920, 0, 1707, 960)
        output = {"name": "DP-3", "x": 1920, "y": 0, "width": 2560,
                  "height": 1440, "scale": 1.5}
        self.assertIs(canvas.resolve_output_monitor(output, FakeDisplay([left, right])), right)
        self.assertIs(canvas.resolve_output_monitor(output, FakeDisplay([right])), right)
        with self.assertRaisesRegex(RuntimeError, "uniquely matched"):
            canvas.resolve_output_monitor(output, FakeDisplay([left]))

    def test_unmatched_or_ambiguous_monitor_geometry_is_an_error(self) -> None:
        output = {"name": "DP-3", "x": 1920, "y": 0, "width": 2560,
                  "height": 1440, "scale": 1.5}
        one = FakeMonitor(0, 0, 0, 1920, 1080)
        other = FakeMonitor(1, 1920, 0, 1600, 900)
        with self.assertRaisesRegex(RuntimeError, "uniquely matched"):
            canvas.resolve_output_monitor(output, FakeDisplay([one, other]))
        duplicate = FakeMonitor(2, 1920, 0, 1707, 960)
        other_match = FakeMonitor(3, 1920, 0, 1706, 960)
        with self.assertRaisesRegex(RuntimeError, "uniquely matched"):
            canvas.resolve_output_monitor(output, FakeDisplay([one, duplicate, other_match]))

    def test_focus_output_validation_rejects_missing_or_nonfinite_geometry(self) -> None:
        missing_name = {"x": 0, "y": 0, "width": 100, "height": 100, "focused": True}
        with self.assertRaisesRegex(RuntimeError, "incomplete name/geometry"):
            canvas.read_focused_output(lambda *_a, **_kw: subprocess.CompletedProcess(
                ["hyprctl"], 0, json.dumps([missing_name]), ""))
        nan_output = {"name": "DP-1", "x": 0, "y": 0, "width": float("nan"),
                      "height": 100, "focused": True}
        with self.assertRaisesRegex(RuntimeError, "incomplete name/geometry"):
            canvas.read_focused_output(lambda *_a, **_kw: subprocess.CompletedProcess(
                ["hyprctl"], 0, json.dumps([nan_output]), ""))


if __name__ == "__main__":
    unittest.main()
