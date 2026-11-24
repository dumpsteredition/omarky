from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import cairo

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import canvas  # noqa: E402
from app import omarky  # noqa: E402


CARD_ID = "a" * 32


def png_image(width: int = 64, height: int = 40) -> bytes:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    context = cairo.Context(surface)
    context.set_source_rgb(0.1, 0.2, 0.3)
    context.paint()
    stream = io.BytesIO()
    surface.write_to_png(stream)
    return stream.getvalue()


class ForChatHandoffTests(unittest.TestCase):
    def test_text_contains_exact_id_path_and_literal_note_as_one_human_request(self) -> None:
        path = os.path.abspath("marked image.png")
        note = "Keep this exact.\nSecond line stays literal."
        text = canvas.build_handoff_text({"id": CARD_ID, "image_path": path, "note": note})
        self.assertEqual(
            text,
            f"Use OMarky card {CARD_ID} in this conversation. "
            f"If the OMarky tool is available, call read_annotation with that ID. "
            f"Otherwise open this local image: {path}\nMy note: {note}",
        )
        self.assertNotIn(";", text)
        self.assertNotIn("&&", text)

    def test_explicit_handoff_saves_exact_rendered_png_then_copies_one_request(self) -> None:
        png = png_image()
        note = "My note, literally."
        image_path = os.path.abspath("marked.png")
        saved = []
        copied = []

        def save(png_bytes: bytes, note_text: str):
            saved.append((png_bytes, note_text))
            return {"id": CARD_ID, "image_path": image_path, "note": note_text}

        result = canvas.invoke_for_chat(png, note, card_saver=save, clipboard_writer=copied.append)
        self.assertEqual(saved, [(png, note)])
        self.assertEqual(len(copied), 1)
        self.assertIn(CARD_ID, copied[0])
        self.assertIn(image_path, copied[0])
        self.assertTrue(copied[0].endswith("My note: " + note))
        self.assertEqual(result["status"], "ok")
        self.assertIn("Ready to paste · nothing sent", result["message"])
        self.assertIn("30 minutes", result["message"])

    def test_path_export_failure_never_copies_and_warns_nothing_was_sent(self) -> None:
        copied = []

        def unavailable(_png: bytes, _note: str):
            raise OSError("private card directory unavailable")

        result = canvas.invoke_for_chat(png_image(), "note", card_saver=unavailable,
                                        clipboard_writer=copied.append)
        self.assertEqual(result["status"], "error")
        self.assertIn("Could not export the marked image path", result["message"])
        self.assertIn("nothing copied or sent", result["message"])
        self.assertEqual(copied, [])

    def test_clipboard_failure_names_saved_card_and_does_not_claim_ready(self) -> None:
        image_path = os.path.abspath("marked.png")
        card = {"id": CARD_ID, "image_path": image_path, "note": "note"}

        def unavailable(_text: str) -> None:
            raise RuntimeError("clipboard readback failed")

        result = canvas.invoke_for_chat(png_image(), "note", card_saver=lambda *_: card,
                                        clipboard_writer=unavailable)
        self.assertEqual(result["status"], "error")
        self.assertIn(CARD_ID, result["message"])
        self.assertIn(image_path, result["message"])
        self.assertIn("clipboard text was not verified; nothing was sent", result["message"])
        self.assertNotIn("Ready to paste", result["message"])

    def test_text_clipboard_writer_uses_one_write_and_exact_readback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="omarky-wl-clipboard-") as temporary:
            root = Path(temporary)
            bindir = root / "bin"
            bindir.mkdir()
            clipboard_path = root / "text.clipboard"
            count_path = root / "copy-count"
            copy_script = bindir / "wl-copy"
            paste_script = bindir / "wl-paste"
            copy_script.write_text(
                "#!/bin/sh\nprintf x >> \"$OMARKY_TEST_COPY_COUNT\"\n"
                "cat > \"$OMARKY_TEST_CLIPBOARD\"\n",
                encoding="utf-8",
            )
            paste_script.write_text("#!/bin/sh\ncat \"$OMARKY_TEST_CLIPBOARD\"\n", encoding="utf-8")
            copy_script.chmod(0o755)
            paste_script.chmod(0o755)
            payload = "Use OMarky card abc in this conversation.\nMy note: exact text."
            with patch.dict(os.environ, {
                "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                "OMARKY_TEST_COPY_COUNT": str(count_path),
                "OMARKY_TEST_CLIPBOARD": str(clipboard_path),
            }, clear=False):
                canvas.publish_handoff_clipboard(payload)
            self.assertEqual(clipboard_path.read_bytes(), payload.encode("utf-8"))
            self.assertEqual(count_path.read_bytes(), b"x")

    def test_handoff_action_is_available_without_target_but_send_is_not(self) -> None:
        options = {"target_label": "", "has_ink": True, "crop_mode": False,
                   "busy": False, "terminal_state": None}
        self.assertTrue(canvas.action_allowed("for_chat", **options))
        self.assertTrue(canvas.action_allowed("copy", **options))
        self.assertFalse(canvas.action_allowed("send", **options))
        self.assertFalse(canvas.action_allowed("for_chat", **{**options, "has_ink": False}))
        self.assertFalse(canvas.action_allowed("for_chat", **{**options, "crop_mode": True}))
        self.assertFalse(canvas.action_allowed("for_chat", **{**options, "busy": True}))

    def test_practice_canvas_rejects_for_chat_to_preserve_codex_tutorial(self) -> None:
        status = []
        overlay = object.__new__(canvas.AnnotateOverlay)
        overlay.practice = True
        overlay._set_status = status.append
        overlay.on_for_chat_clicked(None)
        # The public handler goes through the same routine-mode guard as clicks.
        self.assertEqual(status, ["For chat is available on routine marking only"])

    def test_for_chat_button_stores_the_actual_marked_crop_and_literal_note_not_a_send(self) -> None:
        source = png_image()
        saved = []
        copied = []
        send_calls = []
        status = []
        completions = []
        card_path = os.path.abspath("marked.png")

        class Entry:
            def get_text(self):
                return "literal note"

        class FakeThread:
            def __init__(self, *, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        overlay = object.__new__(canvas.AnnotateOverlay)
        overlay.target_label = ""
        overlay.practice = False
        overlay.strokes = [{
            "points": [(32.0, 20.0)],
            "color": canvas.INK_PALETTE["Coral"],
            "width": 8.0,
        }]
        overlay.crop_mode = False
        overlay.busy = False
        overlay.terminal_send_state = None
        overlay._image_rect = lambda: (0.0, 0.0, 64.0, 40.0)
        overlay.pixbuf = types.SimpleNamespace(get_width=lambda: 64, get_height=lambda: 40)
        overlay.capture_bounds = {"x": 16, "y": 10, "width": 32, "height": 20}
        overlay.screenshot = source
        overlay.last_marked_png = None
        overlay.note_entry = Entry()
        overlay.closed = False
        overlay.action_kind = None
        overlay._set_status = status.append
        overlay._update_controls = lambda: None
        overlay.on_send_callback = lambda *args: send_calls.append(args) or {"status": "ok"}
        overlay.clipboard_writer = lambda _png: self.fail("For chat must not copy an image")
        overlay.handoff_writer = copied.append

        def save(marked_png, note):
            saved.append((marked_png, note))
            return {"id": CARD_ID, "image_path": card_path, "note": note}

        with patch.object(canvas, "save_card", side_effect=save), \
                patch.object(canvas.threading, "Thread", FakeThread), \
                patch.object(canvas.GLib, "idle_add",
                             side_effect=lambda callback, *args: completions.append((callback, args)) or 1):
            overlay.on_for_chat_clicked(None)

        self.assertEqual(len(saved), 1)
        marked_png, note = saved[0]
        self.assertEqual(note, "literal note")
        self.assertEqual(canvas.decode_pixbuf(marked_png).get_width(), 32)
        self.assertEqual(canvas.decode_pixbuf(marked_png).get_height(), 20)
        self.assertNotEqual(marked_png, source)
        self.assertEqual(len(copied), 1)
        self.assertIn(card_path, copied[0])
        self.assertEqual(send_calls, [])
        self.assertEqual(len(completions), 1)
        completion, arguments = completions[0]
        completion(*arguments)
        self.assertTrue(any("Ready to paste · nothing sent" in message for message in status))
        self.assertIsNone(overlay.terminal_send_state)


class ForChatLauncherTests(unittest.TestCase):
    class FakeWidget:
        def __init__(self, text, callback=None):
            self.text = text
            self.callback = callback

    class FakeBox:
        def __init__(self, **_kwargs):
            self.children = []

        def pack_start(self, child, *_args):
            self.children.append(child)

    def _window(self):
        window = object.__new__(omarky.FirstUseWindow)
        window.Gtk = types.SimpleNamespace(
            Orientation=types.SimpleNamespace(HORIZONTAL=0),
            Box=self.FakeBox,
        )
        window.guidance = []
        window.scan_errors = []
        window.targets = []
        window._label = lambda text, *_args, **_kwargs: self.FakeWidget(text)
        window._button = lambda text, _style, callback: self.FakeWidget(text, callback)
        window._replace_content = lambda widgets: setattr(window, "rendered", widgets)
        window.pending = None
        window._finish = lambda action: setattr(window, "pending", action)
        window.start_scan = lambda *_args: setattr(window, "scan_started", True)
        return window

    def test_welcome_and_no_reachable_chat_expose_for_chat_before_codex_setup(self) -> None:
        window = self._window()
        window.show_welcome()
        widgets = [widget for widget, _expand, _fill in window.rendered]
        labels = [widget.text for widget in widgets if isinstance(widget, self.FakeWidget)]
        self.assertIn("Choose how to use OMarky.", labels)
        actions = next(widget for widget in widgets if isinstance(widget, self.FakeBox))
        self.assertEqual([child.text for child in actions.children], ["For chat", "Choose a chat"])
        button = next(child for child in actions.children if child.text == "For chat")
        button.callback(None)
        self.assertEqual(window.pending, {"kind": "for_chat"})

        window = self._window()
        with patch.object(omarky, "_codex_picker_argv", return_value=None):
            window.show_no_targets()
        widgets = [widget for widget, _expand, _fill in window.rendered]
        labels = [widget.text for widget in widgets if isinstance(widget, self.FakeWidget)]
        self.assertIn("For chat", labels)
        self.assertTrue(any("does not open a chat or send anything" in label for label in labels))
        button = next(widget for widget in widgets
                      if isinstance(widget, self.FakeWidget) and widget.text == "For chat")
        button.callback(None)
        self.assertEqual(window.pending, {"kind": "for_chat"})

    def test_launcher_routes_for_chat_to_targetless_capture_and_never_calls_send(self) -> None:
        calls = []
        fake_canvas = types.ModuleType("canvas")
        fake_canvas.annotate = lambda **kwargs: calls.append(kwargs)
        previous = sys.modules.get("canvas")
        sys.modules["canvas"] = fake_canvas

        class Gtk:
            @staticmethod
            def init_check():
                return True, None

        class Window:
            targets = []

            def __init__(self, *_args, **_kwargs):
                pass

            def show_and_wait(self):
                return {"kind": "for_chat"}

        try:
            with patch.object(omarky, "load_preferences", return_value={"targets": [], "active": None}), \
                    patch.object(omarky, "_gtk_modules", return_value=(None, None, Gtk)), \
                    patch.object(omarky, "FirstUseWindow", Window):
                self.assertEqual(omarky._start_desktop("launch"), 0)
        finally:
            if previous is None:
                sys.modules.pop("canvas", None)
            else:
                sys.modules["canvas"] = previous

        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["sample_png"])
        self.assertEqual(calls[0]["target_label"], "")
        result = calls[0]["on_send"](png_image(), "note", {})
        self.assertEqual(result["status"], "error")
        self.assertIn("No chat is paired", result["message"])


if __name__ == "__main__":
    unittest.main()
