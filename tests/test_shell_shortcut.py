from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess

ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "bin" / "omarky-shortcut"
FIXTURE_PATH = ROOT / "tests" / "shell-fixtures" / "hyprctl-binds.json"

loader = importlib.machinery.SourceFileLoader("omarky_shortcut", str(HELPER_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
shortcut = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = shortcut
loader.exec_module(shortcut)


class ShellShortcutTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_show_reads_live_binds_and_prints_one_non_overwriting_line(self):
        calls: list[list[str]] = []
        output = io.StringIO()

        def runner(command, **kwargs):
            calls.append(command)
            self.assertEqual(command, ["hyprctl", "binds", "-j"])
            return CompletedProcess(command, 0, json.dumps(self.fixture), "")

        with contextlib.redirect_stdout(output):
            result = shortcut.main([], runner=runner)

        self.assertEqual(result, 0)
        self.assertEqual(calls, [["hyprctl", "binds", "-j"]])
        self.assertEqual(output.getvalue().count("o.bind("), 1)
        self.assertIn('"SUPER + CTRL + ALT + M"', output.getvalue())
        self.assertIn("app/omarky.py", output.getvalue())
        self.assertIn(" launch", output.getvalue())
        self.assertIn("No config was edited", output.getvalue())

    def test_copy_calls_wl_copy_only_after_live_conflict_check(self):
        calls: list[list[str]] = []
        clipboard: list[str] = []
        output = io.StringIO()

        def runner(command, **kwargs):
            calls.append(command)
            if command == ["hyprctl", "binds", "-j"]:
                return CompletedProcess(command, 0, json.dumps(self.fixture), "")
            if command == ["wl-copy"]:
                clipboard.append(kwargs["input"])
                return CompletedProcess(command, 0, "", "")
            raise AssertionError(f"unexpected external command: {command!r}")

        with contextlib.redirect_stdout(output):
            result = shortcut.main(["--copy"], runner=runner)

        self.assertEqual(result, 0)
        self.assertEqual(calls, [["hyprctl", "binds", "-j"], ["wl-copy"]])
        self.assertEqual(len(clipboard), 1)
        self.assertEqual(clipboard[0].count("o.bind("), 1)
        self.assertIn("Super+Ctrl+Alt+M", output.getvalue())

    def test_ambiguous_physical_key_binding_is_skipped_conservatively(self):
        bindings = [{"key": "", "keycode": 43, "modmask": 76, "mouse": False}]
        candidate = shortcut.choose_candidate(bindings)
        self.assertIsNotNone(candidate)
        self.assertNotEqual(candidate.modmask, 76)
        self.assertEqual(candidate.lua_chord, "SUPER + SHIFT + CTRL + ALT + O")

    def test_all_supported_chords_occupied_returns_no_suggestion(self):
        bindings = [
            {"key": candidate.key, "keycode": 0, "modmask": candidate.modmask, "mouse": False}
            for candidate in shortcut.CANDIDATES
        ]
        self.assertIsNone(shortcut.choose_candidate(bindings))

    def test_failed_hyprctl_fails_closed_without_clipboard_write(self):
        calls: list[list[str]] = []
        output = io.StringIO()
        errors = io.StringIO()

        def runner(command, **kwargs):
            calls.append(command)
            return CompletedProcess(command, 1, "", "not connected")

        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = shortcut.main(["--copy"], runner=runner)

        self.assertEqual(result, 2)
        self.assertEqual(calls, [["hyprctl", "binds", "-j"]])
        self.assertIn("no shortcut was suggested", errors.getvalue())

    def test_malformed_bind_data_fails_closed_without_clipboard_write(self):
        calls: list[list[str]] = []
        errors = io.StringIO()

        def runner(command, **kwargs):
            calls.append(command)
            return CompletedProcess(command, 0, '[{"key":"O"}]', "")

        with contextlib.redirect_stderr(errors):
            result = shortcut.main(["--copy"], runner=runner)

        self.assertEqual(result, 2)
        self.assertEqual(calls, [["hyprctl", "binds", "-j"]])
        self.assertIn("malformed binding data", errors.getvalue())

    def test_widget_is_user_triggered_accessible_and_uses_expected_entrypoints(self):
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        catalog = json.loads((ROOT / "i18n" / "en.json").read_text(encoding="utf-8"))
        widget = (ROOT / "OMarkyWidget.qml").read_text(encoding="utf-8")

        self.assertEqual(manifest["id"], "io.github.dumpsteredition.omarky")
        self.assertEqual(manifest["name"], "OMarky")
        self.assertEqual(manifest["version"], "0.2.0")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(manifest["repository"], "https://github.com/dumpsteredition/omarky")
        self.assertEqual(manifest["entryPoints"]["barWidget"], "OMarkyWidget.qml")
        self.assertTrue(
            {
                "bar.launch.name",
                "bar.launch.description",
                "bar.launch.tooltip",
                "bar.setup.name",
                "bar.setup.description",
                "bar.setup.tooltip",
                "bar.shortcut.copied",
                "bar.shortcut.failed",
            }.issubset(catalog)
        )
        self.assertIn('moduleName: "io.github.dumpsteredition.omarky"', widget)
        self.assertIn("import qs.Commons", widget)
        self.assertIn("import qs.Ui", widget)
        self.assertIn("BarIconButton", widget)
        self.assertIn("iconComponent: Component", widget)
        self.assertIn('Qt.resolvedUrl("app/omarky.py")', widget)
        self.assertIn('root.startApp("launch")', widget)
        self.assertIn('root.startApp("setup")', widget)
        self.assertIn("button === Qt.RightButton", widget)
        self.assertIn("root.copyShortcut()", widget)
        self.assertIn('command = ["/usr/bin/python3", root.appPath, action]', widget)
        self.assertIn("Accessible.role: Accessible.Button", widget)
        self.assertIn("Accessible.name:", widget)
        self.assertIn("Commons.Translations", widget)
        self.assertIn('Qt.resolvedUrl("i18n")', widget)
        self.assertNotIn("Timer {", widget)
        self.assertNotIn("Component.onCompleted", widget)
        self.assertNotIn("grabToImage", widget)
        self.assertNotIn("screenshot", widget.lower())
        self.assertNotIn("http://", widget)
        self.assertNotIn("https://", widget)


if __name__ == "__main__":
    unittest.main()
