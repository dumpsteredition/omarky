"""Optional end-to-end check with the official MCP Python SDK client.

Install exactly mcp==2.0.0 in the test environment, then run this module. The
application server itself remains standard-library-only.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_VERSION = importlib.metadata.version("mcp")
except (ImportError, importlib.metadata.PackageNotFoundError):
    ClientSession = StdioServerParameters = stdio_client = None
    MCP_VERSION = None

ROOT = Path(__file__).resolve().parents[1]
CARD_ID = "fedcba9876543210fedcba9876543210"
PNG = b"\x89PNG\r\n\x1a\nSDK-roundtrip-png"
NOTE = "literal SDK-client note — kept intact"


@unittest.skipUnless(MCP_VERSION == "2.0.0", "optional verifier requires official mcp==2.0.0")
class OfficialMcpSdkClientTests(unittest.TestCase):
    def _server_tree(self, root: Path) -> Path:
        (root / "bin").mkdir()
        (root / "app").mkdir()
        shutil.copy2(ROOT / "bin" / "omarky-mcp", root / "bin" / "omarky-mcp")
        shutil.copy2(ROOT / "app" / "card_mcp.py", root / "app" / "card_mcp.py")
        (root / "app" / "chat_card.py").write_text(
            "CARD_ID = " + repr(CARD_ID) + "\n"
            "PNG = " + repr(PNG) + "\n"
            "NOTE = " + repr(NOTE) + "\n"
            "def load_card(card_id):\n"
            "    if card_id != CARD_ID:\n"
            "        raise ValueError('unknown or expired')\n"
            "    return PNG, NOTE\n",
            encoding="utf-8",
        )
        return root / "bin" / "omarky-mcp"

    def test_official_sdk_negotiates_lists_calls_and_receives_native_image(self):
        async def exercise(script: Path):
            params = StdioServerParameters(command=sys.executable, args=[str(script)])
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.protocol_version, "2025-11-25")
                    listed = await session.list_tools()
                    self.assertEqual([tool.name for tool in listed.tools], ["read_annotation"])
                    self.assertEqual(listed.tools[0].model_dump(by_alias=True)["inputSchema"]["required"], ["card_id"])
                    result = await session.call_tool("read_annotation", {"card_id": CARD_ID})
                    result_data = result.model_dump(by_alias=True)
                    self.assertFalse(result_data["isError"])
                    self.assertEqual(len(result_data["content"]), 2)
                    self.assertEqual(result_data["content"][0]["type"], "text")
                    self.assertEqual(result_data["content"][0]["text"], NOTE)
                    self.assertEqual(result_data["content"][1]["type"], "image")
                    self.assertEqual(result_data["content"][1]["mimeType"], "image/png")
                    self.assertEqual(base64.b64decode(result_data["content"][1]["data"]), PNG)
                    invalid = await session.call_tool("read_annotation", {"card_id": "x" * 32})
                    invalid_data = invalid.model_dump(by_alias=True)
                    self.assertTrue(invalid_data["isError"])
                    self.assertEqual([item["type"] for item in invalid_data["content"]], ["text"])

        with tempfile.TemporaryDirectory(prefix="omarky-mcp-sdk-") as directory:
            script = self._server_tree(Path(directory))
            asyncio.run(exercise(script))

    def test_stdio_server_exits_when_client_closes_stdin(self):
        with tempfile.TemporaryDirectory(prefix="omarky-mcp-eof-") as directory:
            script = self._server_tree(Path(directory))
            process = subprocess.Popen(
                [sys.executable, str(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=Path(directory),
            )
            try:
                process.stdin.close()
                return_code = process.wait(timeout=3)
                self.assertEqual(return_code, 0)
                self.assertEqual(process.stdout.read(), b"")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                process.stdout.close()
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
