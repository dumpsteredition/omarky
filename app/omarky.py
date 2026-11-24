"""Native OMarky first-use, destination chooser, and capture entrypoint.

Run as ``python3 app/omarky.py [launch|setup|capture]``. The route and canvas
modules are loaded only when their real integration points are needed, keeping
``--help`` independent from GTK, connectors, and the desktop session.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Callable, Iterable, Mapping

try:  # Direct script execution puts app/ on sys.path; tests import app.*.
    from practice import PracticeCard, answer_matches, marked_key, new_practice_card
    from prefs_firstuse import (
        active_target,
        load_preferences,
        normalize_target,
        save_verified_target,
        set_active_verified,
        target_identity,
    )
except ModuleNotFoundError:
    from .practice import PracticeCard, answer_matches, marked_key, new_practice_card
    from .prefs_firstuse import (
        active_target,
        load_preferences,
        normalize_target,
        save_verified_target,
        set_active_verified,
        target_identity,
    )


_STATUS_TEXT = {
    "busy": "That conversation is busy. Your mark is still here; choose when to send again.",
    "gone": "That conversation is no longer reachable. Your mark is still here; rescan or Copy.",
    "uncertain": "OMarky could not confirm a final answer from that turn. Your mark is still here; no retry was started.",
    "wrong": "The reply did not name only the circled key. Your mark is still here; try another deliberate Send or Copy.",
    "error": "OMarky could not send to that conversation. Your mark is still here; use Copy or try again.",
}


def _response_message(result: Mapping, fallback: str) -> str:
    value = result.get("message")
    if not isinstance(value, str) or not value.strip():
        return fallback
    # Connector messages are plain display text, never GTK/Pango markup.
    clean = "".join(char for char in value if char in "\n\t" or ord(char) >= 32)
    return clean[:500] or fallback


def _note_names_key(note: str, labels: Iterable[str]) -> bool:
    text = note if isinstance(note, str) else ""
    upper = text.upper()
    return any(
        re.search(rf"(?<![A-Z0-9]){re.escape(label.upper())}(?![A-Z0-9])", upper)
        for label in labels
    )


def _direct_codex_binary() -> str | None:
    """Use the connector's direct-binary check; never invoke a PATH shim."""
    module_names = ["codex_route"]
    if __package__:
        module_names.insert(0, f"{__package__}.codex_route")
    for module_name in module_names:
        try:
            route = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        except Exception:
            return None
        finder = getattr(route, "find_codex_binary", None)
        if callable(finder):
            try:
                binary = finder()
            except Exception:
                return None
            return binary if isinstance(binary, str) and binary else None
    return None


def _omarchy_launch_tui() -> str | None:
    packaged = Path("/usr/share/omarchy/bin/omarchy-launch-tui")
    if packaged.is_file() and os.access(packaged, os.X_OK):
        return str(packaged)
    return shutil.which("omarchy-launch-tui")


def _codex_picker_argv() -> list[str] | None:
    """Return the native launcher argv only when its real pieces are available."""
    if not _direct_codex_binary():
        return None
    plugin_root = Path(__file__).resolve().parent.parent
    picker = plugin_root / "bin" / "omarky-codex"
    if not picker.is_file() or not os.access(picker, os.X_OK):
        return None
    launcher = _omarchy_launch_tui()
    if not launcher:
        return None
    return [launcher, "--app-id=org.omarchy.agent", str(picker)]


def _codex_launch_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Carry the currently selected Omarchy Codex account into the new TUI."""
    env = dict(os.environ if environ is None else environ)
    home_key = "CODEX_HOME"
    pin_key = "OMARCHY_AGENT_CODEX_HOME"
    if pin_key in env and env.get(home_key, "") == env[pin_key]:
        env.pop(home_key, None)
    env.pop(pin_key, None)

    if not env.get(home_key):
        env.pop(home_key, None)
        helper = shutil.which("omarchy-agent-account-home")
        if helper:
            try:
                selected_home = subprocess.run(
                    [helper, "codex"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    check=True,
                    timeout=2,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                selected_home = None
            if selected_home is not None:
                env[pin_key] = selected_home
                if selected_home:
                    env[home_key] = selected_home
    return env


def _make_send_handler(
    target: Mapping | None,
    *,
    practice_card: PracticeCard | None = None,
    config_home=None,
    send_function: Callable | None = None,
    save_function: Callable | None = None,
    gate: threading.Lock | None = None,
) -> Callable:
    """Create the one-callback-per-explicit-Send route boundary.

    ``send_function`` and ``save_function`` are narrow test seams. Product
    callers omit them and resolve the real routes.send implementation.
    """
    safe_target = normalize_target(target) if target is not None else None
    send_gate = gate if gate is not None else threading.Lock()

    def on_send(marked_png: bytes, note: str, meta: dict) -> dict:
        if safe_target is None:
            return {
                "status": "error",
                "message": "No chat is paired. Use Copy to keep this image on your device.",
            }
        if not send_gate.acquire(blocking=False):
            return {"status": "busy", "message": _STATUS_TEXT["busy"]}

        try:
            expected = None
            if practice_card is not None:
                if not isinstance(meta, Mapping):
                    return {
                        "status": "wrong",
                        "message": "Nothing was sent. Circle just one key on the practice image, then try again.",
                    }
                if meta.get("width") != practice_card.width or meta.get("height") != practice_card.height:
                    return {
                        "status": "wrong",
                        "message": "Nothing was sent. Use the current practice image and circle just one key.",
                    }
                expected = marked_key(meta, practice_card)
                if expected is None:
                    return {
                        "status": "wrong",
                        "message": "Nothing was sent. Circle just one key on the practice image, then try again.",
                    }
                if _note_names_key(note, practice_card.labels):
                    return {
                        "status": "wrong",
                        "message": "Nothing was sent. Remove key labels from the note, then circle just one key on the practice image.",
                    }

            try:
                route_send = send_function
                if route_send is None:
                    route_send = importlib.import_module("routes").send
                result = route_send(
                    safe_target,
                    marked_png,
                    note,
                    request_id=uuid.uuid4().hex,
                    wait_reply=practice_card is not None,
                )
            except Exception:
                return {"status": "error", "message": _STATUS_TEXT["error"]}
            if not isinstance(result, Mapping):
                return {"status": "error", "message": _STATUS_TEXT["error"]}

            status = result.get("status")
            if status != "ok":
                if status not in _STATUS_TEXT:
                    status = "error"
                return {"status": status, "message": _response_message(result, _STATUS_TEXT[status])}

            if practice_card is not None:
                answer = result.get("answer")
                if not isinstance(answer, str) or not answer.strip():
                    return {"status": "uncertain", "message": _STATUS_TEXT["uncertain"]}
                if expected is None or not answer_matches(answer, expected, practice_card.labels):
                    return {"status": "wrong", "message": _STATUS_TEXT["wrong"]}
                saver = save_function if save_function is not None else save_verified_target
                try:
                    saved = saver(safe_target, config_home=config_home)
                except Exception:
                    saved = False
                if saved:
                    return {
                        "status": "ok",
                        "message": f"Verified this conversation: {safe_target['label']}. It is pinned for next time.",
                    }
                return {
                    "status": "ok",
                    "message": f"Verified this conversation: {safe_target['label']}. OMarky could not pin it for next time.",
                }

            return {
                "status": "ok",
                "message": _response_message(
                    result,
                    f"Sent to {safe_target['label']}.",
                ),
            }
        finally:
            send_gate.release()

    return on_send


def _display_label(target: Mapping, peers: Iterable[Mapping]) -> str:
    safe = normalize_target(target)
    if safe is None:
        return "Unknown conversation"
    duplicates = [
        candidate
        for candidate in peers
        if normalize_target(candidate) is not None
        and normalize_target(candidate)["label"].casefold() == safe["label"].casefold()
    ]
    if len(duplicates) < 2:
        return safe["label"]
    return f"{safe['label']} — {safe['connector']} · {safe['endpoint']} · {safe['thread_id']}"


def _identity_line(target: Mapping) -> str:
    safe = normalize_target(target)
    if safe is None:
        return "Unknown conversation"
    return f"{safe['connector'].upper()} · endpoint {safe['endpoint']} · thread {safe['thread_id']}"


def _active_reachable_target(state: Mapping, reachable: Iterable[Mapping]) -> Mapping | None:
    """Reuse only the previously explicit active pin; never pick discovery order."""
    active = active_target(state)
    identity = target_identity(active) if active is not None else None
    if identity is None:
        return None
    return next(
        (target for target in reachable if target_identity(target) == identity),
        None,
    )


def _gtk_modules():
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    return Gdk, GLib, Gtk


class FirstUseWindow:
    """One small GTK window for first-use, recovery, and destination choice."""

    def __init__(self, mode: str, state: dict, config_home=None):
        self.Gdk, self.GLib, self.Gtk = _gtk_modules()
        self.mode = mode
        self.state = state
        self.config_home = config_home
        self.pinned = {
            target_identity(target): target
            for target in state.get("targets", [])
            if target_identity(target) is not None
        }
        self.targets: list[dict[str, str]] = []
        self.guidance: list[str] = []
        self.scan_errors: list[str] = []
        self.scan_id = 0
        self.scan_state = None
        self.pending = {"kind": "leave"}

        self.window = self.Gtk.Window(title="OMarky")
        self.window.set_default_size(560, 570)
        self.window.set_resizable(False)
        self.window.set_position(self.Gtk.WindowPosition.CENTER)
        self.window.connect("delete-event", self._on_delete)
        self.window.connect("destroy", self._on_destroy)
        self._install_css()

        self.outer = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=0)
        self.outer.set_border_width(22)
        self.window.add(self.outer)
        self._build_header()
        self.content = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=12)
        self.content.set_margin_top(18)
        self.content.set_margin_bottom(4)
        self.outer.pack_start(self.content, True, True, 0)

    def _install_css(self):
        provider = self.Gtk.CssProvider()
        provider.load_from_data(b"""
            window.omarky-window { background-color: #111923; color: #f3f0e8; }
            label.omarky-title { color: #f3f0e8; font-size: 24px; font-weight: 700; }
            label.omarky-eyebrow { color: #67c7b2; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
            label.omarky-body { color: #c2cad2; font-size: 14px; }
            label.omarky-heading { color: #f5f0e4; font-size: 20px; font-weight: 700; }
            label.omarky-target { color: #f4f0e6; font-size: 15px; font-weight: 700; }
            label.omarky-detail { color: #aeb9c5; font-size: 11px; }
            button.omarky-primary { background: #f1c85b; color: #141923; border-radius: 9px; font-weight: 700; padding: 10px 16px; }
            button.omarky-secondary { background: #263342; color: #f3f0e8; border-radius: 9px; padding: 9px 14px; }
            button.omarky-target-button { background: #202d3b; color: #f3f0e8; border: 1px solid #354657; border-radius: 10px; padding: 10px; }
            button.omarky-target-button:hover { background: #2a3948; border-color: #67c7b2; }
            label.omarky-action { color: #67c7b2; font-size: 11px; font-weight: 700; }
        """)
        screen = self.Gdk.Screen.get_default()
        if screen is not None:
            self.Gtk.StyleContext.add_provider_for_screen(
                screen, provider, self.Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        self.window.get_style_context().add_class("omarky-window")

    def _build_header(self):
        row = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=13)
        mark = self.Gtk.DrawingArea()
        mark.set_size_request(44, 44)
        mark.connect("draw", self._draw_mark)
        row.pack_start(mark, False, False, 0)
        text = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=1)
        title = self._label("OMarky", "omarky-title", wrap=False)
        eyebrow = self._label("CIRCLE TO SEND", "omarky-eyebrow", wrap=False)
        text.pack_start(title, False, False, 0)
        text.pack_start(eyebrow, False, False, 0)
        row.pack_start(text, True, True, 0)
        self.outer.pack_start(row, False, False, 0)
        divider = self.Gtk.Separator(orientation=self.Gtk.Orientation.HORIZONTAL)
        divider.set_margin_top(17)
        self.outer.pack_start(divider, False, False, 0)

    def _draw_mark(self, widget, ctx):
        width, height = widget.get_allocated_width(), widget.get_allocated_height()
        ctx.set_source_rgb(0.12, 0.42, 0.38)
        ctx.arc(width / 2, height / 2, 19, 0, 6.283185307)
        ctx.fill()
        ctx.set_line_width(3)
        ctx.set_source_rgb(0.96, 0.82, 0.39)
        ctx.arc(width / 2, height / 2, 11, 0.25, 5.85)
        ctx.stroke()
        ctx.set_source_rgb(0.96, 0.94, 0.88)
        ctx.arc(width / 2 + 8, height / 2 - 8, 2.3, 0, 6.283185307)
        ctx.fill()
        return False

    def _label(self, text: str, style: str | None = None, *, wrap=True):
        label = self.Gtk.Label()
        label.set_text(text)
        label.set_xalign(0.0)
        label.set_yalign(0.5)
        label.set_line_wrap(wrap)
        label.set_selectable(False)
        if style:
            label.get_style_context().add_class(style)
        return label

    def _button(self, text, style, callback):
        button = self.Gtk.Button(label=text)
        button.get_style_context().add_class(style)
        button.set_can_focus(True)
        button.connect("clicked", callback)
        return button

    def _replace_content(self, widgets):
        for child in self.content.get_children():
            self.content.remove(child)
        for widget, expand, fill in widgets:
            self.content.pack_start(widget, expand, fill, 0)
        self.window.show_all()

    def show_welcome(self):
        heading = self._label("Choose the chat doing the work.", "omarky-heading")
        body = self._label(
            "OMarky will send one practice image only after you press Send. "
            "It pins a conversation only when that same chat replies with the key you circled.",
            "omarky-body",
        )
        boundary = self._label(
            "No chat is selected for you. Nothing is sent while you choose.",
            "omarky-body",
        )
        actions = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=9)
        choose = self._button("Choose a chat", "omarky-primary", lambda *_: self.start_scan())
        copy_only = self._button("Copy only", "omarky-secondary", lambda *_: self._finish({"kind": "copy"}))
        actions.pack_start(choose, True, True, 0)
        actions.pack_start(copy_only, False, False, 0)
        leave = self._button("Leave", "omarky-secondary", lambda *_: self._finish({"kind": "leave"}))
        self._replace_content([
            (heading, False, False),
            (body, False, False),
            (boundary, False, False),
            (actions, False, False),
            (leave, False, False),
        ])

    def start_scan(self, *_args):
        self.scan_id += 1
        scan_id = self.scan_id
        self.scan_state = {"discover_done": False, "guidance_done": False}
        self.scan_errors = []
        self.show_scanning()

        def coordinator():
            try:
                routes = importlib.import_module("routes")
            except Exception:
                self.GLib.idle_add(self._scan_import_failed, scan_id)
                return

            def run_part(kind, callback):
                try:
                    value = callback()
                    error = None
                except Exception:
                    value = None
                    error = "This connector could not be checked. Refresh to try again."
                self.GLib.idle_add(self._scan_part, scan_id, kind, value, error)

            threading.Thread(target=run_part, args=("discover", routes.discover), daemon=True).start()
            threading.Thread(target=run_part, args=("guidance", routes.guidance), daemon=True).start()

        threading.Thread(target=coordinator, daemon=True).start()

    def show_scanning(self):
        heading = self._label("Looking for reachable conversations…", "omarky-heading")
        body = self._label(
            "This check runs in the background. You can leave or open a copy-only canvas now.",
            "omarky-body",
        )
        actions = self._utility_actions(include_refresh=False)
        self._replace_content([(heading, False, False), (body, False, False), (actions, False, False)])

    def _scan_import_failed(self, scan_id):
        if scan_id != self.scan_id:
            return False
        self.scan_errors = ["OMarky could not load its chat connectors. Copy-only still works."]
        self.targets = []
        self.guidance = []
        self.scan_state = {"discover_done": True, "guidance_done": True}
        self.show_no_targets()
        return False

    def _scan_part(self, scan_id, kind, value, error):
        if scan_id != self.scan_id or self.scan_state is None:
            return False
        if error:
            self.scan_errors.append(error)
        if kind == "discover":
            cleaned = []
            seen = set()
            for item in value if isinstance(value, (list, tuple)) else ():
                target = normalize_target(item)
                identity = target_identity(target) if target is not None else None
                if target is not None and identity not in seen:
                    cleaned.append(target)
                    seen.add(identity)
            self.targets = cleaned
            self.scan_state["discover_done"] = True
        elif kind == "guidance":
            self.guidance = [item for item in value if isinstance(item, str) and item.strip()] if isinstance(value, (list, tuple)) else []
            self.scan_state["guidance_done"] = True

        if self.scan_state["discover_done"] and self.scan_state["guidance_done"]:
            self._finish_scan()
        return False

    def _finish_scan(self):
        if self.mode in ("launch", "capture"):
            match = _active_reachable_target(self.state, self.targets)
            if match is not None:
                self._finish({"kind": "capture", "target": match})
                return
        if not self.targets:
            self.show_no_targets()
        else:
            self.show_targets()

    def _utility_actions(self, *, include_refresh=True):
        actions = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=9)
        if include_refresh:
            actions.pack_start(self._button("Rescan", "omarky-secondary", self.start_scan), False, False, 0)
        actions.pack_start(
            self._button("Copy only", "omarky-secondary", lambda *_: self._finish({"kind": "copy"})),
            False,
            False,
            0,
        )
        actions.pack_start(
            self._button("Leave", "omarky-secondary", lambda *_: self._finish({"kind": "leave"})),
            False,
            False,
            0,
        )
        return actions

    def show_no_targets(self):
        heading = self._label("No reachable chats yet.", "omarky-heading")
        body = self._label(
            "When a Codex conversation is available, OMarky will show its project and chat identity here. "
            "Your capture can still be copied without pairing.",
            "omarky-body",
        )
        widgets = [(heading, False, False), (body, False, False)]
        if _codex_picker_argv() is not None:
            widgets.append((
                self._button("Open Codex picker…", "omarky-primary", self._open_codex_picker),
                False,
                False,
            ))
        guidance = self.guidance or [
            "Refresh after starting a supported connector. New conversations must pass the practice image before they can receive a capture."
        ]
        for item in guidance:
            label = self._label("•  " + item, "omarky-detail")
            widgets.append((label, False, False))
        widgets.append((self._utility_actions(), False, False))
        self._replace_content(widgets)

    def _open_codex_picker(self, *_args):
        command = _codex_picker_argv()
        if command is None:
            self.guidance = [
                "The Codex picker is no longer available. Check the terminal instructions below."
            ] + self.guidance
            self.show_no_targets()
            return False
        try:
            subprocess.Popen(command, env=_codex_launch_environment(), close_fds=True)
        except (OSError, subprocess.SubprocessError):
            self.guidance = [
                "OMarky could not open the Codex picker. Try the terminal instruction below."
            ] + self.guidance
            self.show_no_targets()
        return False

    def show_targets(self):
        if self.mode == "setup":
            heading_text = "Choose a conversation to pair."
            body_text = "New chats get one practice image. Already-pinned chats can open directly."
        elif self.state.get("targets"):
            heading_text = "Your last chat is away."
            body_text = "Choose another reachable chat. A new one must pass practice first; no capture is sent during the check."
        else:
            heading_text = "Choose a reachable conversation."
            body_text = "A new chat must answer the practice image before it can receive a capture."
        widgets = [
            (self._label(heading_text, "omarky-heading"), False, False),
            (self._label(body_text, "omarky-body"), False, False),
        ]
        if self.scan_errors:
            widgets.append((self._label(self.scan_errors[0], "omarky-detail"), False, False))

        scroller = self.Gtk.ScrolledWindow()
        scroller.set_policy(self.Gtk.PolicyType.NEVER, self.Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(260)
        rows = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=8)
        for target in self.targets:
            identity = target_identity(target)
            paired = identity in self.pinned
            button = self.Gtk.Button()
            button.get_style_context().add_class("omarky-target-button")
            button.set_hexpand(True)
            button.set_size_request(-1, 86)
            inside = self.Gtk.Box(orientation=self.Gtk.Orientation.VERTICAL, spacing=4)
            inside.set_halign(self.Gtk.Align.FILL)
            top = self.Gtk.Box(orientation=self.Gtk.Orientation.HORIZONTAL, spacing=8)
            name = self._label(_display_label(target, self.targets), "omarky-target")
            action = self._label("USE SAVED CHAT" if paired else "TEST THIS CHAT", "omarky-action", wrap=False)
            top.pack_start(name, True, True, 0)
            top.pack_end(action, False, False, 0)
            detail = self._label(_identity_line(target), "omarky-detail")
            inside.pack_start(top, False, False, 0)
            inside.pack_start(detail, False, False, 0)
            if paired:
                inside.pack_start(self._label("Already passed the same-turn visual check", "omarky-detail"), False, False, 0)
            button.add(inside)
            button.connect("clicked", lambda _button, chosen=target: self.choose_target(chosen))
            rows.pack_start(button, False, False, 0)
        scroller.add(rows)
        widgets.append((scroller, True, True))
        widgets.append((self._utility_actions(), False, False))
        self._replace_content(widgets)

    def choose_target(self, target):
        identity = target_identity(target)
        if identity in self.pinned:
            set_active_verified(target, config_home=self.config_home)
            self._finish({"kind": "capture", "target": target})
        else:
            self._finish({"kind": "practice", "target": target})

    def _finish(self, action):
        self.pending = action
        self.window.destroy()

    def _on_delete(self, *_args):
        self.pending = {"kind": "leave"}
        return False

    def _on_destroy(self, *_args):
        if self.Gtk.main_level() > 0:
            self.Gtk.main_quit()

    def show_and_wait(self):
        self.window.show_all()
        if self.mode == "launch" and not self.state.get("targets"):
            self.show_welcome()
        else:
            self.start_scan()
        self.Gtk.main()
        return self.pending


def _choose_verified_target(current: Mapping, verified: list[Mapping]) -> Mapping | None:
    """Open a fast native picker; never include unverified destinations."""
    Gdk, GLib, Gtk = _gtk_modules()
    current_identity = target_identity(current)
    alternatives = [target for target in verified if target_identity(target) != current_identity]
    if not alternatives:
        return None

    dialog = Gtk.Dialog(title="Choose a verified chat", flags=Gtk.DialogFlags.MODAL)
    dialog.set_default_size(520, 390)
    dialog.add_button("Keep current", Gtk.ResponseType.CANCEL)
    content = dialog.get_content_area()
    content.set_border_width(18)
    content.set_spacing(10)
    heading = Gtk.Label(label="Only chats that passed practice appear here.")
    heading.set_xalign(0)
    heading.set_line_wrap(True)
    content.pack_start(heading, False, False, 0)
    hint = Gtk.Label(label="New chats must pass a practice turn first. Close capture and open OMarky Setup to pair one.")
    hint.set_xalign(0)
    hint.set_line_wrap(True)
    content.pack_start(hint, False, False, 0)
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
    selected = {"target": None}
    for target in alternatives:
        button = Gtk.Button()
        button.set_size_request(-1, 76)
        button.set_label(_display_label(target, verified) + "\n" + _identity_line(target))
        button.connect(
            "clicked",
            lambda _button, chosen=target: (selected.__setitem__("target", chosen), dialog.response(Gtk.ResponseType.OK)),
        )
        rows.pack_start(button, False, False, 0)
    scroller.add(rows)
    content.pack_start(scroller, True, True, 0)
    dialog.show_all()
    dialog.run()
    dialog.destroy()
    return selected["target"]


def _run_annotation(action: Mapping, state: dict, reachable: list[Mapping], config_home=None):
    try:
        canvas = importlib.import_module("canvas")
        annotate = canvas.annotate
    except Exception as exc:
        print(f"OMarky canvas is unavailable: {exc}", file=sys.stderr)
        return 1

    kind = action.get("kind")
    target = normalize_target(action.get("target")) if action.get("target") is not None else None
    if kind == "practice":
        if target is None:
            print("OMarky refused an invalid practice destination.", file=sys.stderr)
            return 1
        try:
            card = new_practice_card()
        except Exception as exc:
            print(f"OMarky could not render its practice card: {exc}", file=sys.stderr)
            return 1
        handler = _make_send_handler(target, practice_card=card, config_home=config_home)
        peers = [normalize_target(item) for item in reachable]
        peers = [item for item in peers if item is not None]
        target_label = _display_label(target, peers)
        try:
            annotate(
                sample_png=card.png,
                note=card.note,
                target_label=target_label,
                on_send=handler,
                on_choose=None,
            )
        except Exception as exc:
            print(f"OMarky practice canvas failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if target is None:
        handler = _make_send_handler(None)
        target_label = ""
        verified = []
    else:
        handler = _make_send_handler(target, config_home=config_home)
        pinned_ids = {
            target_identity(item)
            for item in state.get("targets", [])
            if target_identity(item) is not None
        }
        verified = [
            item for item in reachable
            if target_identity(item) in pinned_ids
        ]
        if target_identity(target) not in pinned_ids:
            print("OMarky refused a capture destination without a successful practice turn.", file=sys.stderr)
            return 1
        peers = [normalize_target(item) for item in reachable]
        target_label = _display_label(target, [item for item in peers if item is not None])

    def choose():
        current = handler_state["target"]
        chosen = _choose_verified_target(current, verified) if current is not None else None
        if chosen is None:
            return None
        set_active_verified(chosen, config_home=config_home)
        handler_state["target"] = normalize_target(chosen)
        # Keep this callback's route target synchronized without changing the
        # open canvas image, strokes, note, or crop state.
        handler_state["handler"] = _make_send_handler(chosen, config_home=config_home)
        return _display_label(chosen, verified)

    # The canvas retains on_send and its image after failures. Swap only the
    # route callback's destination when its synchronous toolbar picker returns.
    handler_state = {"target": target, "handler": handler}

    def dispatch(marked_png, note, meta):
        return handler_state["handler"](marked_png, note, meta)

    try:
        annotate(
            sample_png=None,
            note="",
            target_label=target_label,
            on_send=dispatch,
            on_choose=choose if target is not None else None,
        )
    except Exception as exc:
        print(f"OMarky capture canvas failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _start_desktop(mode: str) -> int:
    config_home = None
    state = load_preferences(config_home)
    try:
        _, _, Gtk = _gtk_modules()
        initialized = Gtk.init_check()
        available = initialized[0] if isinstance(initialized, tuple) else bool(initialized)
        if not available:
            print("OMarky needs a GTK desktop session. Use --help for commands.", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"OMarky needs GTK 3 and Cairo: {exc}", file=sys.stderr)
        return 1

    window = FirstUseWindow(mode, state, config_home=config_home)
    action = window.show_and_wait()
    if action.get("kind") == "leave":
        return 0

    if action.get("kind") == "copy":
        reachable = window.targets
        action = {"kind": "capture", "target": None}
    else:
        reachable = window.targets
    return _run_annotation(action, state, reachable, config_home=config_home)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omarky",
        description="Circle a detail, then send it to one explicitly chosen conversation.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("launch", "setup", "capture"),
        default="launch",
        help="launch the first-use flow, reopen Setup, or start capture (default: launch)",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return _start_desktop(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
