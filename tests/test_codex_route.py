import base64
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import codex_route


PNG = b"\x89PNG\r\n\x1a\nsynthetic-marked-image"
TARGET = {
    "connector": "codex",
    "endpoint": "unix:///run/user/1000/omarky-codex/codex-12-abcd.sock",
    "thread_id": "thread-visible",
    "label": "synthetic — target",
}


class ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.initialized = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def initialize(self):
        self.initialized = True

    def request(self, method, params=None, **_kwargs):
        self.calls.append((method, params))
        if not self.replies:
            raise AssertionError("unexpected RPC: " + method)
        expected, result = self.replies.pop(0)
        if expected != method:
            raise AssertionError("expected %s, received %s" % (expected, method))
        if isinstance(result, Exception):
            raise result
        return result


class CodexRouteTests(unittest.TestCase):
    def make_client(self, replies):
        client = ScriptedClient(replies)
        self.entered = client
        return client

    def patch_route(self, replies):
        client = self.make_client(replies)
        stack = (
            mock.patch.object(codex_route, "_lease_for_endpoint", return_value={"socket_path": "/private/server.sock"}),
            mock.patch.object(codex_route, "find_codex_binary", return_value="/direct/codex"),
            mock.patch.object(codex_route, "_AppServerClient", return_value=client),
        )
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        return client

    def test_send_targets_exact_loaded_thread_with_note_and_png(self):
        client = self.patch_route([
            ("thread/read", {"thread": {"id": "thread-visible", "status": {"type": "idle"}}}),
            ("turn/start", {"turn": {"id": "turn-correlated", "status": "inProgress"}}),
        ])
        result = codex_route.send(TARGET, PNG, "Read the circled key.", request_id="request-7", wait_reply=False)
        self.assertEqual(result["status"], "ok")
        self.assertIn("target", result["message"])
        self.assertTrue(client.initialized)
        self.assertEqual([call[0] for call in client.calls], ["thread/read", "turn/start"])
        self.assertEqual(client.calls[0][1], {"threadId": "thread-visible", "includeTurns": False})
        params = client.calls[1][1]
        self.assertEqual(params["threadId"], "thread-visible")
        self.assertEqual(params["clientUserMessageId"], "request-7")
        self.assertEqual(params["input"][0], {"type": "text", "text": "Read the circled key."})
        self.assertEqual(params["input"][1]["type"], "image")
        self.assertEqual(params["input"][1]["url"], "data:image/png;base64," + base64.b64encode(PNG).decode("ascii"))

    def test_active_thread_is_busy_and_does_not_start_turn(self):
        client = self.patch_route([
            ("thread/read", {"thread": {"id": "thread-visible", "status": {"type": "active", "activeFlags": []}}}),
        ])
        result = codex_route.send(TARGET, PNG, "note", request_id="r", wait_reply=False)
        self.assertEqual(result["status"], "busy")
        self.assertEqual([call[0] for call in client.calls], ["thread/read"])

    def test_unloaded_thread_is_gone_and_never_resumed(self):
        client = self.patch_route([
            ("thread/read", {"thread": {"id": "thread-visible", "status": {"type": "notLoaded"}}}),
        ])
        result = codex_route.send(TARGET, PNG, "note", request_id="r", wait_reply=False)
        self.assertEqual(result["status"], "gone")
        self.assertEqual([call[0] for call in client.calls], ["thread/read"])

    def test_gone_rpc_error_is_reported_without_a_turn(self):
        client = self.patch_route([
            ("thread/read", codex_route._RPCFailure(-32000, "thread not found")),
        ])
        result = codex_route.send(TARGET, PNG, "note", request_id="r", wait_reply=False)
        self.assertEqual(result["status"], "gone")
        self.assertEqual(len(client.calls), 1)

    def test_wait_reply_uses_completed_turn_id_not_other_chat_output(self):
        client = self.patch_route([
            ("thread/read", {"thread": {"id": "thread-visible", "status": {"type": "idle"}}}),
            ("turn/start", {"turn": {"id": "turn-target", "status": "inProgress"}}),
            ("thread/turns/list", {"data": [
                {"id": "other-turn", "status": "completed", "items": [{"type": "agentMessage", "text": "wrong other answer"}]},
                {"id": "turn-target", "status": "inProgress", "items": []},
            ]}),
            ("thread/turns/list", {"data": [
                {"id": "other-turn", "status": "completed", "items": [{"type": "agentMessage", "text": "wrong other answer"}]},
                {"id": "turn-target", "status": "completed", "items": [
                    {"type": "reasoning", "text": "not a final answer"},
                    {"type": "agentMessage", "text": "exact targeted reply"},
                ]},
            ]}),
        ])
        with mock.patch.object(codex_route, "_WAIT_REPLY_TIMEOUT", 1.0), mock.patch.object(codex_route, "_POLL_INTERVAL", 0):
            result = codex_route.send(TARGET, PNG, "note", request_id="reply-1", wait_reply=True)
        self.assertEqual(result, {"status": "ok", "message": "Codex replied in synthetic — target.", "answer": "exact targeted reply"})
        self.assertEqual([call[0] for call in client.calls].count("turn/start"), 1)
        poll_params = client.calls[2][1]
        self.assertEqual(poll_params["threadId"], "thread-visible")
        self.assertEqual(poll_params["itemsView"], "full")

    def test_uncertain_wait_does_not_retry_accepted_turn(self):
        client = self.patch_route([
            ("thread/read", {"thread": {"id": "thread-visible", "status": {"type": "idle"}}}),
            ("turn/start", {"turn": {"id": "turn-target", "status": "inProgress"}}),
            ("thread/turns/list", {"data": []}),
            ("thread/turns/list", {"data": []}),
            ("thread/turns/list", {"data": []}),
            ("thread/turns/list", {"data": []}),
        ])
        with mock.patch.object(codex_route, "_WAIT_REPLY_TIMEOUT", 0.015), mock.patch.object(codex_route, "_POLL_INTERVAL", 0.005):
            result = codex_route.send(TARGET, PNG, "note", request_id="reply-2", wait_reply=True)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual([call[0] for call in client.calls].count("turn/start"), 1)

    def test_unowned_endpoint_and_bad_png_fail_before_transport(self):
        with mock.patch.object(codex_route, "_lease_for_endpoint", return_value=None), mock.patch.object(codex_route, "_AppServerClient") as factory:
            result = codex_route.send(TARGET, PNG, "note", request_id="r", wait_reply=False)
        self.assertEqual(result["status"], "gone")
        factory.assert_not_called()
        bad = codex_route.send(TARGET, b"not a png", "note", request_id="r", wait_reply=False)
        self.assertEqual(bad["status"], "error")

    def test_thread_status_error_is_uncertain_not_success(self):
        client = self.patch_route([
            ("thread/read", {"thread": {"id": "thread-visible", "status": {"type": "systemError"}}}),
        ])
        result = codex_route.send(TARGET, PNG, "note", request_id="r", wait_reply=False)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(len(client.calls), 1)

    def test_websocket_client_frames_are_masked_and_handle_extended_lengths(self):
        for size in (5, 126, 8192):
            payload = bytes((index % 251 for index in range(size)))
            frame = codex_route._websocket_frame(payload)
            self.assertEqual(frame[0], 0x81)
            self.assertTrue(frame[1] & 0x80)
            length_code = frame[1] & 0x7F
            offset = 2
            if length_code == 126:
                actual = int.from_bytes(frame[offset:offset + 2], "big")
                offset += 2
            elif length_code == 127:
                actual = int.from_bytes(frame[offset:offset + 8], "big")
                offset += 8
            else:
                actual = length_code
            mask = frame[offset:offset + 4]
            offset += 4
            decoded = bytes(value ^ mask[index & 3] for index, value in enumerate(frame[offset:]))
            self.assertEqual(actual, size)
            self.assertEqual(decoded, payload)

    def test_private_lease_file_is_owner_only_and_rejects_stale_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            runtime.mkdir(mode=0o700)
            os.chmod(runtime, 0o700)
            env = {"XDG_RUNTIME_DIR": str(runtime)}
            with mock.patch.dict(os.environ, env, clear=False):
                root = codex_route.runtime_root(create=True)
                self.assertIsNotNone(root)
                assert root is not None
                sock_path = root / ("codex-%d-unit.sock" % os.getpid())
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    sock.bind(str(sock_path))
                    os.chmod(sock_path, 0o600)
                    _root, lease_path = codex_route.create_lease(os.getpid(), sock_path)
                    info = lease_path.stat()
                    self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
                    with mock.patch.object(codex_route, "_is_launcher_process", return_value=False):
                        self.assertIsNone(codex_route._safe_lease(lease_path, root))
                    with mock.patch.object(codex_route, "_is_launcher_process", return_value=True):
                        verified = codex_route._safe_lease(lease_path, root)
                        self.assertIsNotNone(verified)
                        assert verified is not None
                        self.assertEqual(verified["socket_path"], str(sock_path))
                finally:
                    sock.close()
                    for path in (root / (sock_path.stem + ".json"), sock_path):
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass


if __name__ == "__main__":
    unittest.main()
