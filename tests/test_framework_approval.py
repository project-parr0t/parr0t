"""Synthetic consumers exercise the actual approval verifier and CLI boundary."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from game_l10n import control_plane, framework_approval, framework_session
from game_l10n.cli import main
from game_l10n.errors import ConfigurationError
from game_l10n.workspace import initialize_workspace
from tests.framework_approval_fixture import issue_approval


class FrameworkApprovalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / "synthetic workspace"
        sdk = root / "sdk"
        sdk.mkdir()
        initialize_workspace(self.workspace, sdk, git_choice="disabled")
        clock = patch.object(framework_approval.time, "time", return_value=2_000_000_000)
        clock.start()
        self.addCleanup(clock.stop)
        self.log = root / "synthetic-host.log"
        self.log.write_bytes(b"SYNTHETIC diagnostic event\n")
        self.completed = self.workspace / "locked-result.json"
        self.completed.write_bytes(b'{"fixture":"completed"}')
        self.usage = self.workspace / ".game-l10n/native/control/usage.json"
        self.usage.parent.mkdir(parents=True, exist_ok=True)
        self.usage.write_bytes(b'{"fixture_calls":9}')

    def change(self, mode="analysis", *, approval=None, **kwargs):
        return framework_session.change_framework_mode(
            self.workspace, mode, user_requested=True, approval=approval, **kwargs,
        )

    def protected_bytes(self):
        paths = [self.workspace / name for name in (
            framework_session.FRAMEWORK_SESSION_PATH, control_plane.CONTROL_STATE_PATH,
            control_plane.CONTROL_ENTRY_PATH, control_plane.CONTROL_HANDOFF_PATH,
        )]
        paths.extend((self.completed, self.usage))
        directory = self.workspace / framework_session.DIAGNOSTIC_LOG_DIRECTORY
        if directory.exists():
            paths.extend(directory.rglob("*"))
        return {str(path.relative_to(self.workspace)): path.read_bytes() if path.is_file() else None
                for path in paths}

    def record_path(self, proof):
        return self.workspace / framework_approval.FRAMEWORK_APPROVAL_DIRECTORY / f"{proof['id']}.json"

    def cli(self, raw, *flags):
        output, errors = io.StringIO(), io.StringIO()
        stream = io.BytesIO(raw) if isinstance(raw, bytes) else io.StringIO(raw)
        with patch("sys.stdin", stream), redirect_stdout(output), redirect_stderr(errors):
            code = main([
                "framework-mode", "--workspace", str(self.workspace), "--mode", "analysis",
                "--user-requested", *flags,
            ])
        return code, output.getvalue(), errors.getvalue()

    def test_flag_only_denies_all_modes_and_log_grant_changes_without_mutation(self):
        proof = issue_approval(self.workspace, "analysis", logs=[self.log])
        self.change(approval=proof, logs=[self.log])
        before = self.protected_bytes()
        for mode in ("analysis", "shutdown"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ConfigurationError, "APPROVAL_REQUIRED"):
                self.change(mode)
            self.assertEqual(self.protected_bytes(), before)
        code, output, errors = self.cli("")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("APPROVAL_REQUIRED", errors)
        self.assertEqual(self.protected_bytes(), before)

    def test_every_record_binding_rejects_without_changing_lifecycle_control_or_grants(self):
        self.change(approval=issue_approval(self.workspace, "analysis", logs=[self.log]), logs=[self.log])
        before = self.protected_bytes()
        invalid_records = (
            {"schema": "invalid"}, {"workspace": str(self.workspace.parent.resolve())},
            {"workspace": str(self.workspace.resolve()) + "/."},
            {"revision": 0}, {"revision": True}, {"mode": "shutdown"},
            {"logs": []}, {"logs": ["synthetic-host.log"]},
            {"contracts_read": True}, {"text_free_confirmed": True},
            {"expires_at": 2_000_000_000}, {"expires_at": 1_999_999_999},
            {"expires_at": 2_000_000_121}, {"expires_at": True},
            {"agent_kind": "worker"}, {"agent_depth": 1}, {"agent_depth": False},
            {"session_id": ""}, {"session_id": None}, {"agent_id": " "},
            {"token_sha256": "0" * 64}, {"token_sha256": "A" * 64},
            {"extra": "SYNTHETIC_PRIVATE_FIELD"},
        )
        for overrides in invalid_records:
            with self.subTest(overrides=overrides):
                proof = issue_approval(
                    self.workspace, "analysis", logs=[self.log], record_overrides=overrides,
                )
                original_record = self.record_path(proof).read_bytes()
                with self.assertRaisesRegex(ConfigurationError, "APPROVAL_INVALID") as failure:
                    self.change(approval=proof, logs=[self.log])
                self.assertNotIn(proof["token"], str(failure.exception))
                self.assertNotIn("SYNTHETIC_PRIVATE_FIELD", str(failure.exception))
                self.assertEqual(self.record_path(proof).read_bytes(), original_record)
                self.assertFalse(self.record_path(proof).with_suffix(".consumed.json").exists())
                self.assertEqual(self.protected_bytes(), before)

    def test_absent_and_malformed_proof_never_creates_a_lock_or_other_artifact(self):
        lock = self.workspace / ".game-l10n/framework-session.lock"
        lock.unlink(missing_ok=True)

        def workspace_bytes():
            return {str(path.relative_to(self.workspace)): path.read_bytes() if path.is_file() else None
                    for path in self.workspace.rglob("*")}

        before = workspace_bytes()
        for proof in (None, {}, {"id": "../outside", "token": "0" * 64}):
            with self.subTest(proof=proof), self.assertRaisesRegex(ConfigurationError, "APPROVAL_"):
                self.change(approval=proof)
            self.assertEqual(workspace_bytes(), before)
            self.assertFalse(lock.exists())

    def test_selected_log_order_is_bound_to_invocation(self):
        second = self.workspace / "second.log"
        second.write_bytes(b"SECOND SYNTHETIC EVENT")
        before = self.protected_bytes()
        proof = issue_approval(self.workspace, "analysis", logs=[self.log, second])
        with self.assertRaisesRegex(ConfigurationError, "APPROVAL_INVALID"):
            self.change(approval=proof, logs=[second, self.log])
        self.assertEqual(self.protected_bytes(), before)
        result = self.change(approval=proof, logs=[self.log, second])
        self.assertEqual(len(result["framework_session"]["logs"]), 2)

    def test_wrong_token_and_malformed_proofs_leave_records_and_state_unchanged(self):
        proof = issue_approval(self.workspace, "analysis")
        before = self.protected_bytes()
        for invalid in (
            {}, {**proof, "token": "f" * 64}, {**proof, "token": "A" * 64},
            {**proof, "id": "../outside"}, {**proof, "id": True},
            {**proof, "token": None}, {**proof, "extra": True},
        ):
            with self.subTest(proof=invalid), self.assertRaisesRegex(ConfigurationError, "APPROVAL_INVALID"):
                self.change(approval=invalid)
            self.assertTrue(self.record_path(proof).exists())
            self.assertEqual(self.protected_bytes(), before)

    def test_cli_bounded_utf8_object_proof_rejects_without_echo_or_mutation(self):
        before = self.protected_bytes()
        for raw in (
            b"\xff", "", "[]", "null", '{"id":"SYNTHETIC_SECRET"}',
            "SYNTHETIC_SECRET" * 100, '{"id":1,"id":2,"token":3}',
            json.dumps({"id": "0" * 32, "token": "0" * 64, "extra": True}),
        ):
            with self.subTest(raw=type(raw).__name__):
                code, output, errors = self.cli(raw, "--approval-stdin")
                self.assertEqual((code, output), (2, ""))
                self.assertIn("APPROVAL_INVALID", errors)
                self.assertNotIn("SYNTHETIC_SECRET", errors)
                self.assertEqual(self.protected_bytes(), before)

    def test_stale_revision_and_cross_workspace_proof_cannot_be_reused(self):
        stale = issue_approval(self.workspace, "shutdown")
        self.change(approval=issue_approval(self.workspace, "analysis"))
        before = self.protected_bytes()
        with self.assertRaisesRegex(ConfigurationError, "APPROVAL_INVALID"):
            self.change("shutdown", approval=stale)
        other = self.workspace.parent / "other workspace"
        other.mkdir()
        (other / "game-l10n.json").write_text("{}", encoding="utf-8")
        proof = issue_approval(self.workspace, "analysis")
        destination = other / framework_approval.FRAMEWORK_APPROVAL_DIRECTORY / self.record_path(proof).name
        destination.parent.mkdir(parents=True)
        destination.write_bytes(self.record_path(proof).read_bytes())
        with self.assertRaisesRegex(ConfigurationError, "APPROVAL_INVALID"):
            framework_session.change_framework_mode(other, "analysis", user_requested=True, approval=proof)
        self.assertEqual(self.protected_bytes(), before)
        self.assertFalse((other / framework_session.FRAMEWORK_SESSION_PATH).exists())

    def test_nonregular_or_redirected_approval_paths_deny_before_any_mutation(self):
        before = self.protected_bytes()
        proof = issue_approval(self.workspace, "analysis")
        path = self.record_path(proof)
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(ConfigurationError, "APPROVAL_INVALID"):
            self.change(approval=proof)
        path.rmdir()
        proof = issue_approval(self.workspace, "analysis")
        original = Path.is_symlink
        for redirected in (self.workspace / ".game-l10n", self.record_path(proof).parent, self.record_path(proof)):
            with self.subTest(path=redirected.name):
                with patch.object(Path, "is_symlink", lambda item: item == redirected or original(item)):
                    with self.assertRaisesRegex(ConfigurationError, "APPROVAL_INVALID"):
                        self.change(approval=proof)
                self.assertTrue(self.record_path(proof).exists())
                self.assertEqual(self.protected_bytes(), before)

    def test_authorized_cli_transition_consumes_once_and_preserves_completed_work(self):
        proof = issue_approval(self.workspace, "analysis")
        completed = {path: path.read_bytes() for path in (self.completed, self.usage)}
        code, output, errors = self.cli(json.dumps(proof), "--approval-stdin")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(json.loads(output)["framework_session"]["mode"], "analysis")
        self.assertNotIn(proof["token"], output)
        self.assertNotIn(proof["id"], output)
        self.assertFalse(self.record_path(proof).exists())
        self.assertTrue(self.record_path(proof).with_suffix(".consumed.json").is_file())
        before = self.protected_bytes()
        code, output, errors = self.cli(json.dumps(proof), "--approval-stdin")
        self.assertEqual((code, output), (2, ""))
        self.assertIn("APPROVAL_INVALID", errors)
        self.assertEqual(self.protected_bytes(), before)
        # Recreating the original record still cannot revive a consumed proof.
        consumed = self.record_path(proof).with_suffix(".consumed.json")
        self.record_path(proof).write_bytes(consumed.read_bytes())
        with self.assertRaisesRegex(ConfigurationError, "APPROVAL_INVALID"):
            self.change(approval=proof)
        self.assertEqual(self.protected_bytes(), before)
        self.assertEqual({path: path.read_bytes() for path in completed}, completed)

    def test_consumption_is_inside_existing_transition_lock(self):
        proof = issue_approval(self.workspace, "analysis")
        before = self.protected_bytes()
        with framework_session._framework_session_lock(self.workspace):
            with self.assertRaisesRegex(ValueError, "TRANSITION_BUSY"):
                self.change(approval=proof)
        self.assertTrue(self.record_path(proof).exists())
        self.assertEqual(self.protected_bytes(), before)
        self.change(approval=proof)


if __name__ == "__main__":
    unittest.main()
