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
from unittest.mock import patch

from app import omarky
from app.practice import new_practice_card
from app.prefs_firstuse import (
    CONNECTORS,
    active_target,
    load_preferences,
    normalize_target,
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
    def test_only_implemented_codex_targets_are_supported(self):
        self.assertEqual(CONNECTORS, frozenset(("codex",)))
        self.assertIsNotNone(normalize_target(TARGET_A))
        unsupported = {**TARGET_A, "connector": "pi"}
        self.assertIsNone(normalize_target(unsupported))
        self.assertEqual(omarky._display_label(unsupported, []), "Unknown conversation")

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

    def test_pre_send_validation_failures_are_recoverable_and_never_reach_connector(self):
        calls = []
        handler = omarky._make_send_handler(
            TARGET_A,
            practice_card=self.card,
            send_function=lambda *args, **kwargs: calls.append(args) or {"status": "ok", "answer": self.key.label},
        )
        no_mark = {"width": self.card.width, "height": self.card.height, "strokes": []}
        wrong_size = {**self.meta, "width": self.card.width - 1}
        label_in_note = f"Read {self.key.label} and confirm it."
        results = [
            handler(self.marked_png, self.card.note, None),
            handler(self.marked_png, self.card.note, no_mark),
            handler(self.marked_png, self.card.note, wrong_size),
            handler(self.marked_png, label_in_note, self.meta),
        ]

        for result in results:
            self.assertEqual(result["status"], "wrong")
            self.assertIn("nothing was sent", result["message"].casefold())
            self.assertIn("circle just one key", result["message"].casefold())
        self.assertIn("remove key labels", results[-1]["message"].casefold())
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


class FirstUsePickerTests(unittest.TestCase):
    def _window(self, guidance=None):
        class FakeWidget:
            def __init__(self, text, callback=None):
                self.text = text
                self.callback = callback

        class FakeBox:
            def __init__(self, **_kwargs):
                self.children = []

            def pack_start(self, child, *_args):
                self.children.append(child)

        window = object.__new__(omarky.FirstUseWindow)
        window.Gtk = types.SimpleNamespace(
            Orientation=types.SimpleNamespace(HORIZONTAL=0),
            Box=FakeBox,
        )
        window.guidance = list(guidance or [])
        setattr(window, "rendered", [])
        window._label = lambda text, *_args, **_kwargs: FakeWidget(text)
        window._button = lambda text, style, callback: FakeWidget(text, callback)
        window._replace_content = lambda widgets: setattr(window, "rendered", widgets)
        return window, FakeWidget, FakeBox

    def test_picker_argv_uses_omarchy_app_id_and_plugin_relative_launcher(self):
        with tempfile.TemporaryDirectory(prefix="omarky-picker-") as temporary:
            plugin_root = Path(temporary) / "plugin"
            (plugin_root / "app").mkdir(parents=True)
            picker = plugin_root / "bin" / "omarky-codex"
            picker.parent.mkdir()
            picker.write_text("mock launcher", encoding="utf-8")
            picker.chmod(0o755)
            launcher = "/usr/share/omarchy/bin/omarchy-launch-tui"

            with (
                patch.object(omarky, "__file__", str(plugin_root / "app" / "omarky.py")),
                patch.object(omarky, "_direct_codex_binary", return_value="/usr/bin/codex"),
                patch.object(omarky, "_omarchy_launch_tui", return_value=launcher),
            ):
                self.assertEqual(
                    omarky._codex_picker_argv(),
                    [launcher, "--app-id=org.omarchy.agent", str(picker)],
                )

        with patch.object(omarky, "_direct_codex_binary", return_value=None):
            self.assertIsNone(omarky._codex_picker_argv())

    def test_no_targets_exposes_one_picker_action_and_keeps_fallbacks_without_launching(self):
        window, FakeWidget, FakeBox = self._window(
            ["To pair explicitly, run bin/omarky-codex in a terminal."]
        )
        command = [
            "/usr/share/omarchy/bin/omarchy-launch-tui",
            "--app-id=org.omarchy.agent",
            "/plugin/bin/omarky-codex",
        ]
        with patch.object(omarky, "_codex_picker_argv", return_value=command), patch.object(
            omarky.subprocess, "Popen"
        ) as launch:
            window.show_no_targets()
            launch.assert_not_called()

        widgets = [widget for widget, _expand, _fill in getattr(window, "rendered")]
        labels = [widget.text for widget in widgets if isinstance(widget, FakeWidget)]
        self.assertEqual(labels.count("Open Codex picker…"), 1)
        self.assertTrue(any("bin/omarky-codex in a terminal" in text for text in labels))
        actions = next(widget for widget in widgets if isinstance(widget, FakeBox))
        self.assertEqual([item.text for item in actions.children], ["Rescan", "Copy only", "Leave"])

    def test_picker_click_uses_mocked_launcher_and_selected_account_environment(self):
        window = object.__new__(omarky.FirstUseWindow)
        command = [
            "/usr/share/omarchy/bin/omarchy-launch-tui",
            "--app-id=org.omarchy.agent",
            "/plugin/bin/omarky-codex",
        ]
        env = {"CODEX_HOME": "/accounts/current"}
        with (
            patch.object(omarky, "_codex_picker_argv", return_value=command),
            patch.object(omarky, "_codex_launch_environment", return_value=env),
            patch.object(omarky.subprocess, "Popen") as launch,
        ):
            window._open_codex_picker()
        launch.assert_called_once_with(command, env=env, close_fds=True)

    def test_omarchy_account_selection_replaces_parent_pin_but_preserves_explicit_home(self):
        helper = "/usr/sbin/omarchy-agent-account-home"
        parent = {
            "PATH": "/usr/bin",
            "CODEX_HOME": "/accounts/parent",
            "OMARCHY_AGENT_CODEX_HOME": "/accounts/parent",
        }
        completed = subprocess.CompletedProcess([helper, "codex"], 0, stdout="/accounts/current\n")
        with patch.object(omarky.shutil, "which", return_value=helper), patch.object(
            omarky.subprocess, "run", return_value=completed
        ) as lookup:
            selected = omarky._codex_launch_environment(parent)
        self.assertEqual(selected["CODEX_HOME"], "/accounts/current")
        self.assertEqual(selected["OMARCHY_AGENT_CODEX_HOME"], "/accounts/current")
        lookup.assert_called_once_with(
            [helper, "codex"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=2,
        )

        explicit = {"PATH": "/usr/bin", "CODEX_HOME": "/accounts/explicit", "OMARCHY_AGENT_CODEX_HOME": "/old-pin"}
        with patch.object(omarky.shutil, "which", return_value=helper), patch.object(
            omarky.subprocess, "run"
        ) as lookup:
            preserved = omarky._codex_launch_environment(explicit)
        self.assertEqual(preserved["CODEX_HOME"], "/accounts/explicit")
        self.assertNotIn("OMARCHY_AGENT_CODEX_HOME", preserved)
        lookup.assert_not_called()

    def test_main_account_home_is_pinned_without_explicit_codex_home(self):
        helper = "/usr/sbin/omarchy-agent-account-home"
        completed = subprocess.CompletedProcess([helper, "codex"], 0, stdout="")
        with patch.object(omarky.shutil, "which", return_value=helper), patch.object(
            omarky.subprocess, "run", return_value=completed
        ):
            selected = omarky._codex_launch_environment({"PATH": "/usr/bin"})
        self.assertEqual(selected["OMARCHY_AGENT_CODEX_HOME"], "")
        self.assertNotIn("CODEX_HOME", selected)


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
