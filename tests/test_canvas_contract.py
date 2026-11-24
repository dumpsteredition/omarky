from __future__ import annotations

import inspect
import io
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cairo

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import canvas  # noqa: E402


def png_image(width: int, height: int, color: tuple[float, float, float] = (1, 1, 1)) -> bytes:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    context.set_source_rgb(*color)
    context.paint()
    stream = io.BytesIO()
    surface.write_to_png(stream)
    return stream.getvalue()


class CanvasContractTests(unittest.TestCase):
    def test_annotate_exports_the_exact_keyword_only_integration_shape(self) -> None:
        signature = inspect.signature(canvas.annotate)
        self.assertEqual(list(signature.parameters),
                         ["sample_png", "note", "target_label", "on_send", "on_choose"])
        self.assertTrue(all(item.kind is inspect.Parameter.KEYWORD_ONLY
                            for item in signature.parameters.values()))
        self.assertIn(signature.return_annotation, (None, "None"))

    def test_supplied_practice_image_never_resolves_or_captures_a_display(self) -> None:
        card = png_image(300, 160)
        with patch.object(canvas, "read_focused_output",
                                        side_effect=AssertionError("practice resolved a display")), \
             patch.object(canvas, "read_screenshot",
                                        side_effect=AssertionError("practice captured a screen")):
            source, output = canvas.prepare_source(card)
        self.assertIs(source, card)
        self.assertIsNone(output)

    def test_practice_card_is_letterboxed_instead_of_cropped(self) -> None:
        rect = canvas.fit_image_rect(300, 160, 900, 700)
        self.assertEqual(rect, (0.0, 110.0, 900.0, 480.0))
        # Both extreme card corners remain on the visible image rectangle.
        self.assertEqual(canvas.canvas_to_image_point((0, 110), rect, 300, 160), (0, 0))
        self.assertEqual(canvas.canvas_to_image_point((900, 590), rect, 300, 160), (300, 160))

    def test_metadata_is_original_image_pixels_even_when_export_is_cropped(self) -> None:
        rect = canvas.fit_image_rect(400, 200, 1000, 600)
        strokes = [{"points": [(500, 300), (750, 425)], "width": 6.5,
                    "color": canvas.INK_PALETTE["Coral"]}]
        mapped = canvas.canvas_strokes_to_image(strokes, rect, 400, 200)
        crop = {"x": 40, "y": 20, "width": 200, "height": 100}
        self.assertEqual(canvas.validate_capture_bounds(crop, 400, 200), crop)
        meta = canvas.build_metadata(400, 200, mapped)
        self.assertEqual(set(meta), {"width", "height", "strokes"})
        self.assertEqual(meta["width"], 400)
        self.assertEqual(meta["height"], 200)
        self.assertEqual(meta["strokes"], [[[200.0, 100.0], [300.0, 150.0]]])

    def test_render_exports_actual_ink_and_crops_only_the_marked_png(self) -> None:
        source = png_image(200, 100)
        stroke = [{"points": [(60, 50)], "width": 10,
                   "color": canvas.INK_PALETTE["Coral"]}]
        marked = canvas.render_marked_png(source, stroke)
        original_pixbuf = canvas.decode_pixbuf(source)
        marked_pixbuf = canvas.decode_pixbuf(marked)
        offset = 50 * marked_pixbuf.get_rowstride() + 60 * marked_pixbuf.get_n_channels()
        original_pixel = original_pixbuf.get_pixels()[offset:offset + 3]
        marked_pixel = marked_pixbuf.get_pixels()[offset:offset + 3]
        self.assertNotEqual(marked_pixel, original_pixel)
        cropped = canvas.render_marked_png(source, stroke,
                                           {"x": 30, "y": 25, "width": 80, "height": 50})
        cropped_pixbuf = canvas.decode_pixbuf(cropped)
        self.assertEqual((cropped_pixbuf.get_width(), cropped_pixbuf.get_height()), (80, 50))

    def test_copy_needs_no_target_but_send_does(self) -> None:
        options = {"target_label": "", "has_ink": True, "crop_mode": False,
                   "busy": False, "terminal_state": None}
        self.assertTrue(canvas.action_allowed("copy", **options))
        self.assertFalse(canvas.action_allowed("send", **options))
        uncertain = {**options, "terminal_state": "uncertain"}
        self.assertTrue(canvas.action_allowed("copy", **uncertain))
        self.assertFalse(canvas.action_allowed("send", **uncertain))
        self.assertFalse(canvas.action_allowed("copy", **{**options, "has_ink": False}))

    def test_copy_failure_is_not_reported_as_success_and_keeps_marked_image(self) -> None:
        png = png_image(20, 10)
        offered: list[bytes] = []

        def unavailable(image: bytes) -> None:
            offered.append(image)
            raise RuntimeError("clipboard unavailable")

        result = canvas.invoke_copy(unavailable, png)
        self.assertEqual(offered, [png])
        self.assertEqual(result["status"], "error")
        self.assertIn("clipboard unavailable", result["message"])

        class Label:
            def __init__(self):
                self.text = ""

            def set_text(self, value):
                self.text = value

            def set_tooltip_text(self, _value):
                pass

        overlay = object.__new__(canvas.AnnotateOverlay)
        overlay.busy = True
        overlay.action_kind = "copy"
        overlay.last_marked_png = png
        overlay.status_label = Label()
        overlay._update_controls = lambda: None
        overlay._action_complete("copy", result, "")
        self.assertIs(overlay.last_marked_png, png)
        self.assertIn("Copy failed", overlay.status_label.text)

    def test_send_outcomes_are_reported_truthfully_and_keep_failed_marks(self) -> None:
        class Label:
            def __init__(self):
                self.text = ""

            def set_text(self, value):
                self.text = value

            def set_tooltip_text(self, _value):
                pass

        for status in ("wrong", "busy", "gone", "error"):
            overlay = object.__new__(canvas.AnnotateOverlay)
            overlay.busy = True
            overlay.action_kind = "send"
            overlay.terminal_send_state = None
            overlay.last_marked_png = b"retained marked image"
            overlay.status_label = Label()
            overlay._update_controls = lambda: None
            overlay._action_complete("send", {"status": status, "message": "route result"},
                                     "Example chat")
            self.assertFalse(overlay.busy)
            self.assertIsNone(overlay.terminal_send_state)
            self.assertIn(f"Send {status}", overlay.status_label.text)
            self.assertIsNotNone(overlay.last_marked_png)

        overlay = object.__new__(canvas.AnnotateOverlay)
        overlay.busy = True
        overlay.action_kind = "send"
        overlay.terminal_send_state = None
        overlay.status_label = Label()
        overlay._update_controls = lambda: None
        overlay._action_complete("send", {"status": "ok", "message": "accepted"},
                                 "Example chat")
        self.assertEqual(overlay.terminal_send_state, "ok")
        self.assertIn("Sent to Example chat", overlay.status_label.text)

    def test_send_callback_invokes_once_and_can_be_run_on_worker_thread(self) -> None:
        observed: list[tuple[threading.Thread, bytes, str, dict]] = []
        png = png_image(20, 10)
        meta = {"width": 20, "height": 10, "strokes": [[[3.0, 4.0]]]}

        def callback(image: bytes, note: str, metadata: dict) -> dict:
            observed.append((threading.current_thread(), image, note, metadata))
            return {"status": "ok", "message": "accepted"}

        main_thread = threading.current_thread()
        result: list[dict] = []
        worker = threading.Thread(target=lambda: result.append(
            canvas.invoke_send(callback, png, "keep this note", meta)))
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(observed), 1)
        self.assertIsNot(observed[0][0], main_thread)
        self.assertEqual(observed[0][1:], (png, "keep this note", meta))
        self.assertEqual(result, [{"status": "ok", "message": "accepted"}])

    def test_send_result_statuses_are_explicit_and_invalid_results_fail_closed(self) -> None:
        for status in ("ok", "wrong", "busy", "gone", "uncertain", "error"):
            self.assertEqual(canvas.validated_send_result({"status": status, "message": "x"}),
                             {"status": status, "message": "x"})
        self.assertEqual(canvas.validated_send_result({"status": "accepted"}),
                         {"status": "error", "message": "send callback returned an invalid status"})
        self.assertEqual(canvas.validated_send_result(None)["status"], "error")

    def test_chooser_cancellation_and_change_preserve_marks(self) -> None:
        for returned, expected in ((None, "Old chat"), ("New chat", "New chat")):
            overlay = object.__new__(canvas.AnnotateOverlay)
            overlay.busy = False
            overlay.terminal_send_state = None
            overlay.target_label = "Old chat"
            overlay.strokes = [{"points": [(4.0, 5.0)], "width": 3.0}]
            overlay.capture_bounds = {"x": 2, "y": 3, "width": 50, "height": 40}
            overlay.crop_mode = False
            original_strokes = overlay.strokes
            original_crop = overlay.capture_bounds
            overlay.on_choose_callback = lambda: returned
            overlay._update_controls = lambda: None
            overlay.on_choose_clicked(None)
            self.assertEqual(overlay.target_label, expected)
            self.assertIs(overlay.strokes, original_strokes)
            self.assertIs(overlay.capture_bounds, original_crop)
            self.assertEqual(overlay.strokes[0]["points"], [(4.0, 5.0)])

    def test_geometry_self_test_is_capture_free(self) -> None:
        canvas.self_test()


if __name__ == "__main__":
    unittest.main()
