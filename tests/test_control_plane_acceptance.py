from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from game_l10n import control_plane
from game_l10n.cli import main
from game_l10n.errors import ConfigurationError
from game_l10n.models import Segment
from game_l10n.native_pass import prepare_native_tasks


class ControlPlaneExposureAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.incident = control_plane.record_control_plane_leak(
            self.root, "source_or_candidate_received"
        )
        self.handoff = self.root / control_plane.CONTROL_HANDOFF_PATH

    def accept(self) -> dict:
        return control_plane.accept_control_plane_exposure(
            self.root, self.handoff, user_confirmed=True, contracts_read=True
        )

    def test_explicit_consent_and_contract_assertions_are_both_required(self) -> None:
        for consent, contracts in ((False, False), (False, True), (True, False), (1, True)):
            with self.subTest(consent=consent, contracts=contracts):
                with self.assertRaisesRegex(ConfigurationError, "ACCEPTANCE_REQUIRED"):
                    control_plane.accept_control_plane_exposure(
                        self.root, self.handoff,
                        user_confirmed=consent, contracts_read=contracts,
                    )
                with self.assertRaisesRegex(ConfigurationError, "ROTATION_REQUIRED"):
                    control_plane.require_control_plane_ready(self.root)
        self.assertFalse((self.root / control_plane.CONTROL_ACCEPTANCE_DIRECTORY).exists())

    def test_cli_admits_exposed_session_without_claiming_freshness(self) -> None:
        arguments = ["accept-control-plane-exposure", "--workspace", str(self.root)]
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(main(arguments), 2)
            self.assertEqual(main(arguments + ["--contracts-read"]), 2)
            self.assertEqual(main(arguments + [
                "--contracts-read", "--user-confirmed-exposure-acceptance",
            ]), 0)
        public = json.loads(output.getvalue())
        self.assertEqual(public["control_plane"]["boundary_mode"], "user_accepted_exposure")
        self.assertEqual(public["control_plane"]["generation"], 1)
        self.assertEqual(public["entry_gate"]["status"], "ready")
        self.assertNotIn(self.incident["incident_id"], output.getvalue())
        audit_path = self.root / public["control_plane"]["acceptance_receipt"]
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(audit["incident_id"], self.incident["incident_id"])
        self.assertEqual(audit["handoff_digest"], json.loads(
            self.handoff.read_text(encoding="utf-8")
        )["digest"])
        self.assertNotIn("resumed_from_incident", control_plane.control_plane_state(self.root))

    def test_acceptance_unblocks_native_preparation_without_modifying_source(self) -> None:
        source = self.root / "source.jsonl"
        source.write_text('{}\n', encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "ROTATION_REQUIRED"):
            prepare_native_tasks(
                self.root, source, [Segment("fixture-1", "SOURCE_FIXTURE")],
                self.root / "batches",
            )
        self.accept()
        result = prepare_native_tasks(
            self.root, source, [Segment("fixture-1", "SOURCE_FIXTURE")],
            self.root / "batches",
        )
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(source.read_text(encoding="utf-8"), '{}\n')

    def test_new_incident_blocks_again_and_rejects_prior_handoff(self) -> None:
        old_handoff = self.handoff.read_bytes()
        first = self.accept()
        next_incident = control_plane.record_control_plane_leak(
            self.root, "parent_opened_data_plane"
        )
        new_handoff = self.handoff.read_bytes()
        self.assertNotEqual(self.incident["incident_id"], next_incident["incident_id"])
        with self.assertRaisesRegex(ConfigurationError, "ROTATION_REQUIRED"):
            control_plane.require_control_plane_ready(self.root)
        self.assertEqual(control_plane.control_plane_entry_state(self.root)["status"], "pending")
        self.handoff.write_bytes(old_handoff)
        with self.assertRaisesRegex(ConfigurationError, "HANDOFF_INVALID"):
            self.accept()
        self.handoff.write_bytes(new_handoff)
        second = self.accept()
        self.assertNotEqual(first["exposure_acceptance"], second["exposure_acceptance"])
        for state in (first, second):
            audit = (self.root / control_plane.CONTROL_ACCEPTANCE_DIRECTORY
                     / f"{state['exposure_acceptance']}.json")
            self.assertTrue(audit.is_file())

    def test_missing_or_modified_audit_blocks_ready_state(self) -> None:
        accepted = self.accept()
        audit = (self.root / control_plane.CONTROL_ACCEPTANCE_DIRECTORY
                 / f"{accepted['exposure_acceptance']}.json")
        original = audit.read_bytes()
        for replacement in (None, b'{}', b'{"source":"PRIVATE_FIXTURE"}'):
            with self.subTest(replacement=replacement):
                if replacement is None:
                    audit.unlink()
                else:
                    audit.write_bytes(replacement)
                with self.assertRaisesRegex(ConfigurationError, "ACCEPTANCE_INVALID") as error:
                    control_plane.require_control_plane_ready(self.root)
                self.assertNotIn("PRIVATE_FIXTURE", str(error.exception))
                audit.write_bytes(original)

    def test_acceptance_cannot_be_replayed_into_another_workspace(self) -> None:
        self.accept()
        with tempfile.TemporaryDirectory() as other_directory:
            other = Path(other_directory)
            shutil.copytree(self.root / ".game-l10n", other / ".game-l10n")
            with self.assertRaisesRegex(ConfigurationError, "ACCEPTANCE_INVALID"):
                control_plane.require_control_plane_ready(other)

    def test_arbitrary_handoff_and_tampered_canonical_handoff_are_rejected(self) -> None:
        other = self.root / "other.json"
        other.write_bytes(self.handoff.read_bytes())
        with self.assertRaisesRegex(ConfigurationError, "HANDOFF_REQUIRED"):
            control_plane.accept_control_plane_exposure(
                self.root, other, user_confirmed=True, contracts_read=True,
            )
        handoff = json.loads(self.handoff.read_text(encoding="utf-8"))
        handoff["from_generation"] += 1
        self.handoff.write_text(json.dumps(handoff), encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "HANDOFF_INVALID"):
            self.accept()
        self.assertEqual(control_plane.control_plane_state(self.root)["status"], "rotation_required")

    def test_interrupted_acceptance_is_fail_closed_and_retryable(self) -> None:
        original_write = control_plane._atomic_json

        def interrupted(path: Path, payload: dict) -> None:
            if "exposure_acceptance" in payload:
                raise OSError("synthetic interruption")
            original_write(path, payload)

        with patch.object(control_plane, "_atomic_json", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "synthetic interruption"):
                self.accept()
        with self.assertRaisesRegex(ConfigurationError, "ROTATION_REQUIRED"):
            control_plane.require_control_plane_ready(self.root)
        self.accept()
        control_plane.require_control_plane_ready(self.root)
        control_plane.require_control_plane_entry(self.root)
        audits = list((self.root / control_plane.CONTROL_ACCEPTANCE_DIRECTORY).glob("*.json"))
        self.assertEqual(len(audits), 1)

    def test_acceptance_preserves_native_observations_usage_and_completed_results(self) -> None:
        native = self.root / ".game-l10n" / "native" / "control" / "state.json"
        native.parent.mkdir(parents=True)
        native.write_text(json.dumps({
            "schema": "parr0t.native-control-state.v3",
            "control_steps_in_wave": 9, "last_progress_marker": "fixture-progress",
            "unchanged_observations": 3, "last_action": "wait_for_active_wave",
            "usage": {"calls": 7},
        }), encoding="utf-8")
        result = self.root / "completed.json"
        result.write_text('{"fixture":"locked-success"}', encoding="utf-8")
        before = {path: path.read_bytes() for path in (native, result, self.handoff)}
        accepted = self.accept()
        self.assertEqual(accepted["generation"], self.incident["generation"])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with self.assertRaisesRegex(ConfigurationError, "no exposure incident"):
            self.accept()

    def test_regular_rotation_after_new_leak_removes_accepted_mode(self) -> None:
        self.accept()
        control_plane.record_control_plane_leak(self.root, "unvalidated_worker_response")
        rotated = control_plane.acknowledge_control_plane_rotation(self.root, self.handoff)
        control_plane.establish_control_plane_entry(self.root)
        self.assertEqual(rotated["generation"], 2)
        self.assertNotIn("boundary_mode", control_plane.public_control_plane_state(rotated))

    def test_acceptance_does_not_admit_private_envelope_fields(self) -> None:
        self.accept()
        envelope = self.root / "envelope.json"
        envelope.write_text(json.dumps({"source": "PRIVATE_FIXTURE"}), encoding="utf-8")
        with self.assertRaises(ConfigurationError) as error:
            control_plane.validate_control_envelope(self.root, envelope)
        self.assertNotIn("PRIVATE_FIXTURE", str(error.exception))


if __name__ == "__main__":
    unittest.main()
