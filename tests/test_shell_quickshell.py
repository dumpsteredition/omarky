from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_SHELL = Path("/usr/share/omarchy/shell")
FIXTURE = ROOT / "tests" / "shell-fixtures" / "load_widget.qml"
AVAILABLE = bool(
    shutil.which("quickshell")
    and (SYSTEM_SHELL / "Commons" / "qmldir").is_file()
    and (SYSTEM_SHELL / "Ui" / "qmldir").is_file()
)


@unittest.skipUnless(AVAILABLE, "installed Quickshell and Omarchy QML modules are required")
class ShellQmlSmokeTests(unittest.TestCase):
    def test_widget_loads_offscreen_in_private_quickshell_config(self):
        with tempfile.TemporaryDirectory(prefix="omarky-qml-smoke-") as scratch:
            private = Path(scratch)
            config = private / "config"
            state = private / "state"
            cache = private / "cache"
            data = private / "data"
            runtime = private / "runtime"
            for directory in (config, state, cache, data):
                directory.mkdir()
            runtime.mkdir(mode=0o700)
            runtime.chmod(0o700)
            for module in ("Commons", "Ui"):
                (config / module).symlink_to(SYSTEM_SHELL / module, target_is_directory=True)

            widget_url = (ROOT / "OMarkyWidget.qml").as_uri()
            shell_qml = FIXTURE.read_text(encoding="utf-8").replace(
                "__OMARKY_WIDGET_URL__", widget_url
            )
            shell_file = config / "shell.qml"
            shell_file.write_text(shell_qml, encoding="utf-8")

            env = os.environ.copy()
            env.update(
                {
                    "XDG_CONFIG_HOME": str(config),
                    "XDG_STATE_HOME": str(state),
                    "XDG_CACHE_HOME": str(cache),
                    "XDG_DATA_HOME": str(data),
                    "XDG_RUNTIME_DIR": str(runtime),
                    "QT_QPA_PLATFORM": "offscreen",
                    "QT_FORCE_STDERR_LOGGING": "1",
                    "OMARCHY_PATH": "/usr/share/omarchy",
                }
            )
            for key in ("WAYLAND_DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "HYPRLAND_CMD"):
                env.pop(key, None)

            process = subprocess.Popen(
                ["quickshell", "--path", str(shell_file), "--no-color"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    output, _ = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate()

            self.assertIn("OMARKY_WIDGET_LOADED", output)
            self.assertNotIn("OMARKY_WIDGET_LOAD_ERROR", output)
            self.assertNotIn("Failed to load configuration", output)
            self.assertNotIn("QML Error", output)


if __name__ == "__main__":
    unittest.main()
