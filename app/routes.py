"""Thin dispatcher for OMarky's built-in conversation adapters.

Keep imports lazy: each connector lane can be exercised independently, and
optional adapters absent from a partial build are never discovered as chats.
"""
from __future__ import annotations

import importlib
from typing import Any, Dict, List, Mapping, Optional


_CONNECTORS = ("codex",)
_MISSING_GUIDANCE = {
    "codex": "Codex: its adapter is not installed in this OMarky build.",
}


def _load_adapter(connector: str) -> Optional[Any]:
    if connector not in _CONNECTORS:
        return None
    module_name = connector + "_route"
    if __package__:
        qualified = "%s.%s" % (__package__, module_name)
        try:
            return importlib.import_module("." + module_name, __package__)
        except ModuleNotFoundError as exc:
            if exc.name == qualified:
                return None
            raise
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise


def _valid_target(target: Any, connector: str) -> Optional[Dict[str, str]]:
    if not isinstance(target, Mapping) or target.get("connector") != connector:
        return None
    endpoint = target.get("endpoint")
    thread_id = target.get("thread_id")
    label = target.get("label")
    if (not isinstance(endpoint, str) or not endpoint
            or not isinstance(thread_id, str) or not thread_id
            or not isinstance(label, str) or not label):
        return None
    return {"connector": connector, "endpoint": endpoint, "thread_id": thread_id, "label": label}


def discover() -> List[Dict[str, str]]:
    """Return only live conversations emitted by the hardcoded adapters."""
    targets: List[Dict[str, str]] = []
    seen = set()
    for connector in _CONNECTORS:
        adapter = _load_adapter(connector)
        if adapter is None:
            continue
        for raw in adapter.discover():
            target = _valid_target(raw, connector)
            if target is None:
                continue
            identity = (target["connector"], target["endpoint"], target["thread_id"])
            if identity not in seen:
                seen.add(identity)
                targets.append(target)
    return targets


def guidance() -> List[str]:
    """Return concrete pairing instructions from installed built-in adapters."""
    instructions: List[str] = []
    for connector in _CONNECTORS:
        adapter = _load_adapter(connector)
        if adapter is None:
            instructions.append(_MISSING_GUIDANCE[connector])
            continue
        values = adapter.guidance()
        if isinstance(values, list):
            for value in values:
                if isinstance(value, str) and value and value not in instructions:
                    instructions.append(value)
    return instructions


def send(target: Mapping[str, Any], png: bytes, note: str, *, request_id: str,
         wait_reply: bool) -> Dict[str, str]:
    """Dispatch an explicit target through the matching built-in adapter."""
    if not isinstance(target, Mapping):
        return {"status": "error", "message": "The selected conversation target is invalid."}
    connector = target.get("connector")
    if connector not in _CONNECTORS:
        return {"status": "error", "message": "The selected connector is not supported by OMarky."}
    adapter = _load_adapter(connector)
    if adapter is None:
        return {"status": "gone", "message": "The selected connector is not available in this OMarky build."}
    return adapter.send(target, png, note, request_id=request_id, wait_reply=wait_reply)
