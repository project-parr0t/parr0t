from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from game_l10n.cli import main
from game_l10n.errors import ConfigurationError
from game_l10n.control_plane import control_plane_state
from game_l10n.framework_session import change_framework_mode, framework_session_state
from game_l10n.user_request import USER_REQUEST_PATH, record_user_request, user_request_binding


class UserRequestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "game-l10n.json").write_text("{}", encoding="utf-8")
        self.source = self.workspace / "initial-request.txt"
        self.original = "SYNTHETIC images/fonts; keep usable locale.\r\nSYNTHETIC model fixture/reviewer@route:high; 2 total calls.\nSYNTHETIC Unicode: \uac00\u2603\n"
        self.source.write_bytes(self.original.encode("utf-8"))

    def record(self, **overrides):
        return record_user_request(self.workspace, self.source, **{
            "user_requested": True, "text_free_confirmed": True, **overrides,
        })

    def test_snapshot_survives_source_removal_without_normalizing_request(self):
        binding = self.record()
        self.assertEqual(self.record(), binding)
        self.source.unlink()
        self.assertEqual(user_request_binding(self.workspace), binding)
        self.assertEqual((self.workspace / binding["path"]).read_bytes(), self.original.encode("utf-8"))

    def test_rebinding_cannot_replace_original_with_restart_summary(self):
        binding = self.record()
        self.source.write_text("Continue the task.", encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "E_USER_REQUEST_CONFLICT"):
            self.record()
        self.assertEqual(user_request_binding(self.workspace), binding)

    def test_changed_payload_and_escaping_binding_fail_closed(self):
        binding = self.record()
        payload = self.workspace / binding["path"]
        payload.write_bytes(b"x" * binding["bytes"])
        with self.assertRaisesRegex(ConfigurationError, "E_USER_REQUEST_INVALID"):
            user_request_binding(self.workspace)
        metadata = self.workspace / USER_REQUEST_PATH
        metadata.write_text(json.dumps({**binding, "path": "../initial-request.txt"}), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "E_USER_REQUEST_INVALID"):
            user_request_binding(self.workspace)

    def test_missing_confirmation_empty_and_invalid_requests_are_not_recorded(self):
        for arguments in ({"user_requested": False}, {"text_free_confirmed": False}):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ConfigurationError, "ASSERTION_REQUIRED"):
                self.record(**arguments)
        for data in (b"", b" \r\n", b"\xff", b"text\0text"):
            self.source.write_bytes(data)
            with self.subTest(data=data), self.assertRaisesRegex(ConfigurationError, "E_USER_REQUEST_INVALID"):
                self.record()
        self.assertFalse((self.workspace / USER_REQUEST_PATH).exists())

    def test_recording_during_analysis_does_not_reactivate_or_echo_request(self):
        change_framework_mode(self.workspace, "analysis", user_requested=True)
        lifecycle = framework_session_state(self.workspace)
        control = control_plane_state(self.workspace)
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["user-request", "--workspace", str(self.workspace), "--input", str(self.source),
                         "--user-requested", "--text-free-confirmed"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "recorded")
        self.assertNotIn(self.original, output.getvalue())
        self.assertEqual(framework_session_state(self.workspace), lifecycle)
        self.assertEqual(control_plane_state(self.workspace), control)


if __name__ == "__main__":
    unittest.main()
