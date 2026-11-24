from __future__ import annotations

import base64
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.card_mcp as card_mcp  # noqa: E402


CARD_ID = "0123456789abcdef0123456789abcdef"
PNG = b"\x89PNG\r\n\x1a\nmarked-png-fixture"
NOTE = "keep this exact note: snowman ☃"


class CardMcpTests(unittest.TestCase):
    def test_one_tool_schema_names_only_an_exact_card_id(self):
        definition = card_mcp.tool_definition()
        self.assertEqual(definition["name"], "read_annotation")
        self.assertEqual(definition["inputSchema"]["required"], ["card_id"])
        self.assertTrue(definition["inputSchema"]["additionalProperties"] is False)
        self.assertEqual(set(definition["inputSchema"]["properties"]), {"card_id"})

    def test_result_is_native_literal_text_and_png_image(self):
        reader = Mock(return_value=(PNG, NOTE))
        result = card_mcp.read_annotation({"card_id": CARD_ID}, reader)
        reader.assert_called_once_with(CARD_ID)
        self.assertNotIn("isError", result)
        self.assertEqual(result["content"], [
            {"type": "text", "text": NOTE},
            {"type": "image", "data": base64.b64encode(PNG).decode("ascii"),
             "mimeType": "image/png"},
        ])

    def test_bad_ids_and_extra_fields_never_reach_card_reader(self):
        reader = Mock()
        for arguments in (
            {"card_id": "../" + "a" * 29},
            {"card_id": "A" * 32},
            {"card_id": "a" * 31},
            {"card_id": "a" * 33},
            {"card_id": "a" * 32, "path": "/tmp/elsewhere"},
            {},
            None,
        ):
            with self.subTest(arguments=arguments):
                result = card_mcp.read_annotation(arguments, reader)
                self.assertTrue(result["isError"])
                self.assertEqual(len(result["content"]), 1)
                self.assertEqual(result["content"][0]["type"], "text")
        reader.assert_not_called()

    def test_unknown_or_expired_card_returns_no_image_or_note(self):
        result = card_mcp.read_annotation({"card_id": CARD_ID}, lambda _card_id: (_ for _ in ()).throw(ValueError("expired")))
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"], [
            {"type": "text", "text": "The specified card is unavailable, expired, or unreadable."}
        ])

    def test_invalid_reader_payload_is_rejected(self):
        for payload in ((b"not png", NOTE), (PNG, "x" * (card_mcp.MAX_NOTE_BYTES + 1)),
                        (PNG + b"x" * card_mcp.MAX_PNG_BYTES, NOTE), (PNG, b"not text")):
            with self.subTest(payload_type=tuple(type(value).__name__ for value in payload)):
                result = card_mcp.read_annotation({"card_id": CARD_ID}, lambda _card_id, p=payload: p)
                self.assertTrue(result["isError"])
                self.assertEqual(len(result["content"]), 1)
                self.assertEqual(result["content"][0]["type"], "text")

    def test_handshake_lists_exactly_one_tool_and_call_returns_content(self):
        session = card_mcp.McpSession(lambda _card_id: (PNG, NOTE))
        initialize = session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "test-client", "version": "1"}},
        })
        self.assertEqual(initialize["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(initialize["result"]["capabilities"], {"tools": {}})
        self.assertIsNone(session.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        listed = session.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], ["read_annotation"])
        called = session.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "read_annotation", "arguments": {"card_id": CARD_ID}}})
        self.assertEqual(called["result"]["content"][0], {"type": "text", "text": NOTE})
        self.assertEqual(called["result"]["content"][1]["type"], "image")

    def test_unknown_protocol_version_falls_back_without_trusting_its_type(self):
        session = card_mcp.McpSession()
        response = session.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": [], "capabilities": {},
                       "clientInfo": {"name": "test-client", "version": "1"}},
        })
        self.assertIsNotNone(response)
        self.assertEqual(response["result"]["protocolVersion"], card_mcp.LATEST_PROTOCOL)

    def test_protocol_output_is_json_lines_and_eof_returns(self):
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        source = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in messages))
        sink = io.BytesIO()
        card_mcp.serve(source, sink, lambda _card_id: (PNG, NOTE))
        responses = [json.loads(line) for line in sink.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertEqual(responses[1]["result"]["tools"][0]["name"], "read_annotation")


if __name__ == "__main__":
    unittest.main()
