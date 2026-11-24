"""Small stdio MCP endpoint for reading one explicitly named OMarky card."""
from __future__ import annotations

import base64
import json
import re
import sys
from typing import BinaryIO, Callable

SERVER_NAME = "omarky"
SERVER_VERSION = "0.1.0"
TOOL_NAME = "read_annotation"
SUPPORTED_PROTOCOLS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
LATEST_PROTOCOL = "2025-11-25"
CARD_ID_PATTERN = re.compile(r"[0-9a-f]{32}", re.ASCII)
MAX_PNG_BYTES = 12 * 1024 * 1024
MAX_NOTE_BYTES = 16 * 1024


def load_card(card_id: str) -> tuple[bytes, str]:
    """Use the app's exact-ID private store; never interpret an ID as a path."""
    from app.chat_card import load_card as read_card

    return read_card(card_id)


def tool_definition() -> dict:
    return {
        "name": TOOL_NAME,
        "description": (
            "Read exactly one OMarky marked PNG and its original note by the "
            "32-character card_id supplied by the user. This tool cannot search cards."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{32}$",
                    "description": "Exact OMarky card ID copied by the user.",
                }
            },
            "required": ["card_id"],
            "additionalProperties": False,
        },
    }


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def read_annotation(arguments: object, reader: Callable[[str], tuple[bytes, str]] | None = None) -> dict:
    """Return MCP-native text and image content for one exact card only."""
    if not isinstance(arguments, dict) or set(arguments) != {"card_id"}:
        return _tool_error("Provide only the exact card_id supplied by the user.")
    card_id = arguments.get("card_id")
    if not isinstance(card_id, str) or CARD_ID_PATTERN.fullmatch(card_id) is None:
        return _tool_error("card_id must be 32 lowercase hexadecimal characters.")

    try:
        png_bytes, note = (reader or load_card)(card_id)
        if not isinstance(png_bytes, bytes) or not (8 <= len(png_bytes) <= MAX_PNG_BYTES):
            return _tool_error("The specified card is unavailable or has an invalid image.")
        if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return _tool_error("The specified card is unavailable or has an invalid image.")
        if not isinstance(note, str):
            return _tool_error("The specified card is unavailable or has an invalid note.")
        note_bytes = note.encode("utf-8", errors="strict")
        if len(note_bytes) > MAX_NOTE_BYTES:
            return _tool_error("The specified card is unavailable or has an invalid note.")
    except Exception:
        # Do not expose storage paths, implementation details, or partial card data.
        return _tool_error("The specified card is unavailable, expired, or unreadable.")

    return {
        "content": [
            {"type": "text", "text": note},
            {
                "type": "image",
                "data": base64.b64encode(png_bytes).decode("ascii"),
                "mimeType": "image/png",
            },
        ]
    }


def _rpc_error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _valid_request_id(value: object) -> bool:
    return value is None or (isinstance(value, (str, int)) and not isinstance(value, bool))


class McpSession:
    """Minimal MCP JSON-RPC session supporting initialization and one read tool."""

    def __init__(self, reader: Callable[[str], tuple[bytes, str]] | None = None):
        self.reader = reader
        self.negotiated = False
        self.ready = False

    def handle(self, message: object) -> dict | None:
        if not isinstance(message, dict):
            return _rpc_error(None, -32600, "Invalid Request")
        request_id = message.get("id")
        has_id = "id" in message
        if has_id and not _valid_request_id(request_id):
            return _rpc_error(None, -32600, "Invalid Request")
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _rpc_error(request_id if has_id else None, -32600, "Invalid Request") if has_id else None

        method = message["method"]
        if not has_id:
            if method == "notifications/initialized" and self.negotiated:
                self.ready = True
            return None

        params = message.get("params", {})
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "Invalid params")

        if method == "initialize":
            requested = params.get("protocolVersion")
            protocol_version = (
                requested
                if isinstance(requested, str) and requested in SUPPORTED_PROTOCOLS
                else LATEST_PROTOCOL
            )
            self.negotiated = True
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": "Use read_annotation only for the exact card_id explicitly supplied by the user.",
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if not self.ready:
            return _rpc_error(request_id, -32002, "Server not initialized")
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [tool_definition()]}}
        if method == "tools/call":
            name = params.get("name")
            if name != TOOL_NAME:
                return _rpc_error(request_id, -32602, "Unknown tool")
            result = read_annotation(params.get("arguments", {}), self.reader)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        return _rpc_error(request_id, -32601, "Method not found")


def serve(input_stream: BinaryIO | None = None, output_stream: BinaryIO | None = None,
          reader: Callable[[str], tuple[bytes, str]] | None = None) -> None:
    """Serve newline-delimited JSON-RPC until stdin closes; stdout is protocol-only."""
    source = input_stream if input_stream is not None else sys.stdin.buffer
    sink = output_stream if output_stream is not None else sys.stdout.buffer
    session = McpSession(reader)
    for raw_line in source:
        try:
            message = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            response = _rpc_error(None, -32700, "Parse error")
        else:
            response = session.handle(message)
        if response is not None:
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            sink.write(encoded + b"\n")
            sink.flush()
