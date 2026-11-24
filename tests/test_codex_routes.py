import unittest
from types import SimpleNamespace
from unittest import mock

from app import routes


class RoutesDispatcherTests(unittest.TestCase):
    def test_dispatcher_only_returns_verified_codex_targets_and_deduplicates(self):
        first = {"connector": "codex", "endpoint": "unix:///tmp/owner.sock", "thread_id": "t-1", "label": "Repo — title", "extra": "discard"}
        codex = SimpleNamespace(discover=lambda: [first, dict(first), {"connector": "codex", "endpoint": "registration", "thread_id": "", "label": "capability"}], guidance=lambda: [])
        with mock.patch.object(routes, "_load_adapter", return_value=codex) as load:
            result = routes.discover()
        self.assertEqual(result, [{"connector": "codex", "endpoint": "unix:///tmp/owner.sock", "thread_id": "t-1", "label": "Repo — title"}])
        self.assertEqual([call.args[0] for call in load.call_args_list], ["codex"])
        self.assertNotIn("extra", result[0])

    def test_missing_codex_adapter_is_not_imported_as_a_chat(self):
        with mock.patch.object(routes, "_load_adapter", return_value=None):
            self.assertEqual(routes.discover(), [])
            guidance = routes.guidance()
        self.assertTrue(any("Codex" in item and "not installed" in item for item in guidance))

    def test_send_dispatches_exact_signature_to_builtin_adapter(self):
        expected = {"status": "ok", "message": "accepted"}
        adapter = SimpleNamespace(send=mock.Mock(return_value=expected))
        target = {"connector": "codex", "endpoint": "socket", "thread_id": "target", "label": "label"}
        with mock.patch.object(routes, "_load_adapter", return_value=adapter):
            actual = routes.send(target, b"image", "note", request_id="req-1", wait_reply=True)
        self.assertIs(actual, expected)
        adapter.send.assert_called_once_with(target, b"image", "note", request_id="req-1", wait_reply=True)

    def test_unknown_connector_is_rejected_without_import_or_execution(self):
        with mock.patch.object(routes, "_load_adapter") as load:
            result = routes.send({"connector": "arbitrary", "command": "not run"}, b"image", "note", request_id="r", wait_reply=False)
        self.assertEqual(result["status"], "error")
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
