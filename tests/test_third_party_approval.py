from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import ThirdPartyApprovalError
from game_l10n.third_party_approval import create_third_party_approval, require_third_party_approval
from game_l10n.workspace import initialize_workspace
from game_l10n.third_party_approval import consume_offline_processing, validated_offline_processing
from game_l10n.third_party_approval import _approval_lock
from game_l10n.store import stable_hash


class ThirdPartyApprovalTests(unittest.TestCase):
    def _workspace(self, root: Path) -> Path:
        sdk = root / "sdk"
        game = root / "fixture-game"
        work = root / "work"
        sdk.mkdir()
        game.mkdir()
        initialize_workspace(work, sdk, git_choice="disabled", game_roots=[game])
        return work

    def _approval(self, work: Path, **overrides: object) -> tuple[Path, dict[str, object]]:
        values: dict[str, object] = {
            "action": "clone",
            "name": "Fixture Tool",
            "publisher": "Fixture Publisher",
            "source": "https://example.invalid/fixture/tool",
            "version": "1.2.3",
            "digest": "git:" + "a" * 40,
            "license_name": "MIT",
            "purpose": "Exercise the synthetic approval contract.",
            "data_scope": "No project or user data is transmitted.",
            "risk_disclosed": True,
            "user_confirmed": True,
        }
        values.update(overrides)
        return create_third_party_approval(work, **values)  # type: ignore[arg-type]

    def test_exact_explicit_approval_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            path, _ = self._approval(work)
            identity = {
                "action": "clone",
                "name": "Fixture Tool",
                "publisher": "Fixture Publisher",
                "source": "https://example.invalid/fixture/tool",
                "version": "1.2.3",
                "digest": "git:" + "a" * 40,
            }
            require_third_party_approval(work, path, **identity, consume=True)
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_APPROVAL_CONSUMED"):
                require_third_party_approval(work, path, **identity, consume=True)

    def test_missing_consent_or_risk_disclosure_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_USER_CONFIRMATION_REQUIRED"):
                self._approval(work, user_confirmed=False)
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_RISK_DISCLOSURE_REQUIRED"):
                self._approval(work, risk_disclosed=False)

    def test_mutable_version_and_unpinned_code_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_VERSION_NOT_EXACT"):
                self._approval(work, version="latest")
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_DIGEST_NOT_IMMUTABLE"):
                self._approval(work, digest="not-applicable")

    def test_identity_or_action_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            path, _ = self._approval(work)
            with self.assertRaisesRegex(ThirdPartyApprovalError, "E_THIRD_PARTY_APPROVAL_NAME_MISMATCH"):
                require_third_party_approval(
                    work,
                    path,
                    action="clone",
                    name="Different Tool",
                    publisher="Fixture Publisher",
                    source="https://example.invalid/fixture/tool",
                    version="1.2.3",
                    digest="git:" + "a" * 40,
                )



    def test_explicit_workflow_reuse_keeps_exact_scope_and_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = self._workspace(Path(directory))
            path, payload = self._approval(work, workflow_reuse=True, ttl_minutes=7 * 24 * 60)
            identity = {key: payload[key] for key in (
                "action", "name", "publisher", "source", "version", "digest", "purpose", "data_scope"
            )}
            for _ in range(2):
                receipt = require_third_party_approval(work, path, **identity, consume=True)
                self.assertIsNone(receipt["consumed_at_utc"])
                self.assertEqual(receipt["expires_at_utc"], payload["expires_at_utc"])
            identity["data_scope"] = "Additional data"
            with self.assertRaisesRegex(ThirdPartyApprovalError, "WORKFLOW_SCOPE_MISMATCH"):
                require_third_party_approval(work, path, **identity)

    def test_offline_processing_requires_exact_script_and_argv_consent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = self._workspace(root)
            script = root / "fixture.py"
            script.write_text("pass\n", encoding="utf-8")
            argv = [str(script.resolve()), "--offline"]
            sha = hashlib.sha256(script.read_bytes()).hexdigest()
            entry = {"argv": argv, "sha256": sha}
            with self.assertRaisesRegex(ThirdPartyApprovalError, "APPROVAL_REQUIRED"):
                validated_offline_processing(work, [entry])
            approval, _ = self._approval(
                work, action="execute", digest="sha256:" + sha,
                purpose="Bound offline image processing",
                data_scope="offline_processing:" + stable_hash(argv),
                workflow_reuse=True,
            )
            bound = validated_offline_processing(work, [{**entry, "approval_path": str(approval)}])
            self.assertEqual(bound[0]["argv"], argv)
            self.assertEqual(bound[0]["sha256"], sha)
            self.assertIs(bound[0]["reusable"], True)
            self.assertIn("expires_at_utc", bound[0])
            script.write_text("pass # changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ThirdPartyApprovalError, "SCRIPT_MISMATCH"):
                validated_offline_processing(work, [{**entry, "approval_path": str(approval)}])


    def test_offline_validation_is_pure_and_consumption_requires_exact_durable_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work = self._workspace(root)
            script = root / "fixture.py"
            script.write_text("pass\n", encoding="utf-8")
            argv = [str(script.resolve()), "--offline"]
            sha = hashlib.sha256(script.read_bytes()).hexdigest()
            approval, _ = self._approval(
                work, action="execute", digest="sha256:" + sha,
                purpose="Bound offline image processing",
                data_scope="offline_processing:" + stable_hash(argv),
            )
            entries = [{"argv": argv, "sha256": sha, "approval_path": str(approval)}]
            bound = validated_offline_processing(work, entries)
            self.assertIsNone(json.loads(approval.read_text(encoding="utf-8"))["consumed_at_utc"])
            task_path = work / "task.json"
            task_path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ThirdPartyApprovalError, "BINDING_MISMATCH"):
                consume_offline_processing(work, entries, binding_path=task_path)
            self.assertIsNone(json.loads(approval.read_text(encoding="utf-8"))["consumed_at_utc"])
            task = {
                "schema": "parr0t.data-plane-task.v1", "worker_role": "image_ui_inventory",
                "offline_processing": bound,
            }
            task["task_id"] = stable_hash(task)
            task_path.write_text(json.dumps(task), encoding="utf-8")
            with _approval_lock(approval):
                with self.assertRaisesRegex(ThirdPartyApprovalError, "BUSY"):
                    consume_offline_processing(work, entries, binding_path=task_path)
            self.assertIsNone(json.loads(approval.read_text(encoding="utf-8"))["consumed_at_utc"])
            self.assertEqual(consume_offline_processing(work, entries, binding_path=task_path), bound)
            receipt = json.loads(approval.read_text(encoding="utf-8"))
            self.assertIsNotNone(receipt["consumed_at_utc"])
            self.assertEqual(consume_offline_processing(work, entries, binding_path=task_path), bound)
            self.assertEqual(validated_offline_processing(work, entries, binding_path=task_path), bound)
            other_task = work / "other-task.json"
            other_task.write_bytes(task_path.read_bytes())
            with self.assertRaisesRegex(ThirdPartyApprovalError, "CONSUMED"):
                consume_offline_processing(work, entries, binding_path=other_task)
            task["additional_scope"] = "changed"
            task["task_id"] = stable_hash({key: value for key, value in task.items() if key != "task_id"})
            task_path.write_text(json.dumps(task), encoding="utf-8")
            with self.assertRaisesRegex(ThirdPartyApprovalError, "CONSUMED"):
                consume_offline_processing(work, entries, binding_path=task_path)


if __name__ == "__main__":
    unittest.main()
