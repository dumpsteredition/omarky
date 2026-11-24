"""Private, atomic preferences for targets verified by a real practice turn."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Mapping

CONNECTORS = frozenset(("codex", "pi"))
TARGET_FIELDS = ("connector", "endpoint", "thread_id", "label")


def normalize_target(target: Mapping) -> dict[str, str] | None:
    """Keep only the public route identity and its human-readable label."""
    if not isinstance(target, Mapping):
        return None
    values = {field: target.get(field) for field in TARGET_FIELDS}
    if not isinstance(values["connector"], str) or values["connector"] not in CONNECTORS:
        return None
    safe = {}
    for field, value in values.items():
        if not isinstance(value, str) or not value.strip():
            return None
        safe[field] = value
    if any(len(safe[field]) > 8192 for field in TARGET_FIELDS):
        return None
    return safe


def target_identity(target: Mapping) -> tuple[str, str, str] | None:
    safe = normalize_target(target)
    if safe is None:
        return None
    return safe["connector"], safe["endpoint"], safe["thread_id"]


def config_directory(config_home: str | os.PathLike | None = None) -> Path:
    if config_home is not None and os.fspath(config_home):
        base = Path(config_home)
    else:
        xdg_home = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg_home) if xdg_home else Path.home() / ".config"
    return base / "omarky"


def preferences_path(config_home: str | os.PathLike | None = None) -> Path:
    return config_directory(config_home) / "verified-targets.json"


def _empty_state() -> dict:
    return {"version": 1, "targets": [], "active": None}


def load_preferences(config_home: str | os.PathLike | None = None) -> dict:
    path = preferences_path(config_home)
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = json.load(stream)
    except (OSError, ValueError, TypeError):
        return _empty_state()
    if not isinstance(raw, dict) or raw.get("version") != 1 or not isinstance(raw.get("targets"), list):
        return _empty_state()

    targets = []
    seen = set()
    for candidate in raw["targets"]:
        safe = normalize_target(candidate)
        if safe is None:
            continue
        identity = target_identity(safe)
        if identity not in seen:
            targets.append(safe)
            seen.add(identity)
    active = raw.get("active")
    if not isinstance(active, list) or len(active) != 3 or tuple(active) not in seen:
        active = None
    return {"version": 1, "targets": targets, "active": list(active) if active else None}


@contextmanager
def _preference_lock(config_home: str | os.PathLike | None = None):
    directory = config_directory(config_home)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / "verified-targets.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write(state: dict, config_home: str | os.PathLike | None = None) -> bool:
    directory = config_directory(config_home)
    temp_path = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".verified-targets-", dir=directory)
        temp_path = Path(temp_name)
        os.fchmod(fd, 0o600)
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, preferences_path(config_home))
        temp_path = None
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    except OSError:
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def save_verified_target(target: Mapping, config_home: str | os.PathLike | None = None) -> bool:
    """Pin only an already-verified target; callers invoke after correct reply."""
    safe = normalize_target(target)
    if safe is None:
        return False
    try:
        with _preference_lock(config_home):
            state = load_preferences(config_home)
            identity = target_identity(safe)
            if identity is None:
                return False
            replaced = False
            for index, current in enumerate(state["targets"]):
                if target_identity(current) == identity:
                    state["targets"][index] = safe
                    replaced = True
                    break
            if not replaced:
                state["targets"].append(safe)
            state["active"] = list(identity)
            return _atomic_write(state, config_home)
    except OSError:
        return False


def set_active_verified(target: Mapping, config_home: str | os.PathLike | None = None) -> bool:
    """Move the active pointer only to a target already pinned by verification."""
    identity = target_identity(target)
    if identity is None:
        return False
    try:
        with _preference_lock(config_home):
            state = load_preferences(config_home)
            if not any(target_identity(current) == identity for current in state["targets"]):
                return False
            state["active"] = list(identity)
            return _atomic_write(state, config_home)
    except OSError:
        return False


def active_target(state: Mapping) -> dict[str, str] | None:
    if not isinstance(state, Mapping):
        return None
    identity = state.get("active")
    if not isinstance(identity, (list, tuple)) or len(identity) != 3:
        return None
    return next(
        (target for target in state.get("targets", []) if target_identity(target) == tuple(identity)),
        None,
    )
