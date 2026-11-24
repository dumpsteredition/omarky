import math
import os
from pathlib import Path
import random
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest

from app import omarky
from app.practice import new_practice_card
from app.prefs_firstuse import (
    active_target,
    load_preferences,
    preferences_path,
    save_verified_target,
    set_active_verified,
    target_identity,
)


TARGET_A = {
    "connector": "codex",
    "endpoint": "/run/user/1000/omarky/project-a.sock",
    "thread_id": "thread-1",
    "label": "Project A — Review",
}
TARGET_B = {
    "connector": "codex",
    "endpoint": "/run/user/1000/omarky/project-b.sock",
    "thread_id": "thread-1",
    "label": "Project A — Review",
}


def circle_stroke(key, points=96):
    center_x, center_y = key.center
    radius_x, radius_y = key.width / 2 + 28, key.height / 2 + 22
    return [
        [center_x + radius_x * math.cos(math.tau * index / points),
         center_y + radius_y * math.sin(math.tau * index / points)]
        for index in range(points + 1)
    ]


class FirstUseStateTests(unittest.TestCase):
    def test_route_identity_keeps_same_thread_in_different_projects_separate(self):
        self.assertNotEqual(target_identity(TARGET_A), target_identity(TARGET_B))
        self.assertEqual(TARGET_A["thread_id"], TARGET_B["thread_id"])

    def test_only_verified_targets_are_persisted_privately_and_extras_are_dropped(self):
        with tempfile.TemporaryDirectory(prefix="omarky-state-") as temporary:
            config_home = Path(temporary) / "config"
            target_with_extra = {**TARGET_A, "access_token": "must-not-persist"}
            self.assertTrue(save_verified_target(target_with_extra, config_home=config_home))
            state = load_preferences(config_home)
            self.assertEqual(state["targets"], [TARGET_A])
            self.assertEqual(active_target(state), TARGET_A)
            self.assertFalse(set_active_verified(TARGET_B, config_home=config_home))
            path = preferences_path(config_home)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn("must-not-persist", path.read_text(encoding="utf-8"))

    def test_two_projects_pin_as_distinct_targets(self):
        with tempfile.TemporaryDirectory(prefix="omarky-projects-") as temporary:
            self.assertTrue(save_verified_target(TARGET_A, config_home=temporary))
            self.assertTrue(save_verified_target(TARGET_B, config_home=temporary))
            state = load_preferences(temporary)
            self.assertEqual(len(state["targets"]), 2)
            self.assertEqual(
                state["active"],
                [TARGET_B["connector"], TARGET_B["endpoint"], TARGET_B["thread_id"]],
            )

    def test_last_explicit_pin_wins_over_discovery_order_and_missing_pin_never_falls_back(self):
        state = {
            "version": 1,
            "targets": [TARGET_A],
            "active": [TARGET_A["connector"], TARGET_A["endpoint"], TARGET_A["thread_id"]],
        }
        self.assertEqual(omarky._active_reachable_target(state, [TARGET_B, TARGET_A]), TARGET_A)
        self.assertIsNone(omarky._active_reachable_target(state, [TARGET_B]))
        self.assertIsNone(omarky._active_reachable_target({"targets": [], "active": None}, [TARGET_B, TARGET_A]))

    def test_same_display_label_is_disambiguated_by_real_route_identity(self):
        text = omarky._display_label(TARGET_A, [TARGET_A, TARGET_B])
        self.assertIn(TARGET_A["endpoint"], text)
        self.assertIn(TARGET_A["thread_id"], text)


class FirstUseSendTests(unittest.TestCase):
    def setUp(self):
        self.card = new_practice_card(random.Random(9))
        self.key = self.card.keycaps[1]
        self.meta = {"width": self.card.width, "height": self.card.height, "strokes": [circle_stroke(self.key)]}
        self.marked_png = b"marked-image"

    def test_practice_is_one_explicit_waiting_send_and_pins_only_matching_same_turn_answer(self):
        calls = []
        saved = []

        def send(target, png, note, *, request_id, wait_reply):
            calls.append((target, png, note, request_id, wait_reply))
            return {"status": "ok", "answer": f"The circled key is ({self.key.label})!"}

        def save(target, *, config_home=None):
            saved.append(target)
            return True

        handler = omarky._make_send_handler(
            {**TARGET_A, "auth": "not forwarded"},
            practice_card=self.card,
            send_function=send,
            save_function=save,
        )
        note = "Read the mark in the image, then answer plainly."
        result = handler(self.marked_png, note, self.meta)

        self.assertEqual(result["status"], "ok")
        self.assertIn("pinned", result["message"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], TARGET_A)
        self.assertEqual(calls[0][1:3], (self.marked_png, note))
        self.assertTrue(calls[0][3])
        self.assertTrue(calls[0][4])
        self.assertEqual(saved, [TARGET_A])
        self.assertTrue(set(calls[0][3]).issubset(set("0123456789abcdef")))

    def test_correct_same_turn_answer_is_the_only_path_that_writes_a_pin(self):
        with tempfile.TemporaryDirectory(prefix="omarky-verified-turn-") as temporary:
            handler = omarky._make_send_handler(
                TARGET_A,
                practice_card=self.card,
                config_home=temporary,
                send_function=lambda *args, **kwargs: {"status": "ok", "answer": self.key.label},
            )
            result = handler(self.marked_png, self.card.note, self.meta)
            state = load_preferences(temporary)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(active_target(state), TARGET_A)

    def test_missing_final_answer_is_uncertain_not_verified(self):
        with tempfile.TemporaryDirectory(prefix="omarky-no-answer-") as temporary:
            handler = omarky._make_send_handler(
                TARGET_A,
                practice_card=self.card,
                config_home=temporary,
                send_function=lambda *args, **kwargs: {"status": "ok", "message": "Turn accepted"},
            )
            result = handler(self.marked_png, self.card.note, self.meta)
            self.assertEqual(result["status"], "uncertain")
            self.assertEqual(load_preferences(temporary)["targets"], [])

    def test_wrong_or_multiple_key_answer_does_not_pin_and_is_not_retried(self):
        calls = []
        saved = []

        def send(target, png, note, *, request_id, wait_reply):
            calls.append(request_id)
            other = next(label for label in self.card.labels if label != self.key.label)
            return {"status": "ok", "answer": f"{self.key.label} or {other}"}

        handler = omarky._make_send_handler(
            TARGET_A,
            practice_card=self.card,
            send_function=send,
            save_function=lambda target, **kwargs: saved.append(target) or True,
        )
        result = handler(self.marked_png, self.card.note, self.meta)

        self.assertEqual(result["status"], "wrong")
        self.assertEqual(len(calls), 1)
        self.assertEqual(saved, [])

    def test_no_mark_changed_dimensions_or_key_in_editable_note_never_reach_connector(self):
        calls = []
        handler = omarky._make_send_handler(
            TARGET_A,
            practice_card=self.card,
            send_function=lambda *args, **kwargs: calls.append(args) or {"status": "ok", "answer": self.key.label},
        )
        no_mark = {"width": self.card.width, "height": self.card.height, "strokes": []}
        wrong_size = {**self.meta, "width": self.card.width - 1}
        label_in_note = f"Read {self.key.label} and confirm it."

        self.assertEqual(handler(self.marked_png, self.card.note, no_mark)["status"], "uncertain")
        self.assertEqual(handler(self.marked_png, self.card.note, wrong_size)["status"], "uncertain")
        self.assertEqual(handler(self.marked_png, label_in_note, self.meta)["status"], "uncertain")
        self.assertEqual(calls, [])

    def test_single_send_gate_blocks_a_concurrent_second_request(self):
        gate = threading.Lock()
        gate.acquire()
        calls = []
        handler = omarky._make_send_handler(
            TARGET_A,
            practice_card=self.card,
            send_function=lambda *args, **kwargs: calls.append(args) or {"status": "ok", "answer": self.key.label},
            gate=gate,
        )
        self.assertEqual(handler(self.marked_png, self.card.note, self.meta)["status"], "busy")
        self.assertEqual(calls, [])
        gate.release()

    def test_ordinary_capture_sends_once_without_waiting_for_an_answer(self):
        calls = []

        def send(target, png, note, *, request_id, wait_reply):
            calls.append((target, png, note, wait_reply))
            return {"status": "ok", "message": "Accepted"}

        handler = omarky._make_send_handler(TARGET_B, send_function=send)
        result = handler(self.marked_png, "Editable ordinary note", {"width": 5, "height": 6, "strokes": []})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [(TARGET_B, self.marked_png, "Editable ordinary note", False)])

    def test_failed_target_is_reported_without_pinning_or_retry(self):
        calls = []
        saved = []
        handler = omarky._make_send_handler(
            TARGET_A,
            practice_card=self.card,
            send_function=lambda *args, **kwargs: calls.append(args) or {"status": "gone", "message": "Conversation closed"},
            save_function=lambda target, **kwargs: saved.append(target) or True,
        )
        result = handler(self.marked_png, self.card.note, self.meta)
        self.assertEqual(result["status"], "gone")
        self.assertEqual(len(calls), 1)
        self.assertEqual(saved, [])


class FirstUseCanvasHandoffTests(unittest.TestCase):
    def test_practice_handoff_uses_sample_canvas_and_never_starts_capture(self):
        calls = []
        fake_canvas = types.ModuleType("canvas")
        setattr(fake_canvas, "annotate", lambda **kwargs: calls.append(kwargs))
        previous = sys.modules.get("canvas")
        sys.modules["canvas"] = fake_canvas
        try:
            result = omarky._run_annotation(
                {"kind": "practice", "target": TARGET_A},
                {"version": 1, "targets": [], "active": None},
                [TARGET_A],
            )
        finally:
            if previous is None:
                sys.modules.pop("canvas", None)
            else:
                sys.modules["canvas"] = previous
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["sample_png"].startswith(bytes((137, 80, 78, 71, 13, 10, 26, 10))))
        self.assertEqual(calls[0]["note"], "Read the key I circled. Reply with only its label.")
        self.assertEqual(calls[0]["target_label"], TARGET_A["label"])
        self.assertIsNone(calls[0]["on_choose"])
        self.assertTrue(callable(calls[0]["on_send"]))

    def test_ordinary_handoff_uses_real_capture_branch_and_verified_target(self):
        calls = []
        fake_canvas = types.ModuleType("canvas")
        setattr(fake_canvas, "annotate", lambda **kwargs: calls.append(kwargs))
        previous = sys.modules.get("canvas")
        sys.modules["canvas"] = fake_canvas
        try:
            result = omarky._run_annotation(
                {"kind": "capture", "target": TARGET_A},
                {"version": 1, "targets": [TARGET_A], "active": [TARGET_A["connector"], TARGET_A["endpoint"], TARGET_A["thread_id"]]},
                [TARGET_A],
            )
        finally:
            if previous is None:
                sys.modules.pop("canvas", None)
            else:
                sys.modules["canvas"] = previous
        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0]["sample_png"])
        self.assertEqual(calls[0]["target_label"], TARGET_A["label"])
        self.assertTrue(callable(calls[0]["on_choose"]))
        self.assertTrue(callable(calls[0]["on_send"]))


class FirstUseCliTests(unittest.TestCase):
    def test_help_does_not_open_gtk_or_connectors(self):
        script = Path(__file__).resolve().parents[1] / "app" / "omarky.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "DISPLAY": "", "WAYLAND_DISPLAY": ""},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("launch", completed.stdout)
        self.assertIn("setup", completed.stdout)
        self.assertIn("capture", completed.stdout)
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
