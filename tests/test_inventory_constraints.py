from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from game_l10n.cli import main
from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.coverage import save_inventory_attestation
from game_l10n.data_plane_task import (
    current_inventory_constraint,
    prepare_data_plane_task,
    seal_data_plane_task,
)
from game_l10n.errors import ConfigurationError
from game_l10n.inventory_constraints import observe_inventory_constraints
from game_l10n.workspace import initialize_workspace, load_config


class InventoryConstraintTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        sdk, game = root / "sdk", root / "game"
        sdk.mkdir()
        game.mkdir()
        self.work = root / "work"
        initialize_workspace(self.work, sdk, game_roots=[game], git_choice="disabled")
        establish_control_plane_entry(self.work)
        self.config = load_config(self.work)
        self.config["sdk_selection"].update(
            status="selected", inventory_fingerprint="synthetic-sdk"
        )
        self._save_config()
        self.activation = {"schema": "synthetic-sdk-binding", "revision": 1}
        self.addCleanup(patch.stopall)
        patch("game_l10n.workspace.require_sdk_activation", return_value=self.activation).start()
        patch("game_l10n.cli.require_sdk_activation", return_value=self.activation).start()
        self.primary = self.work / "inputs/primary-inventory.json"
        self.independent = self.work / "inputs/independent-inventory.json"
        self.output = self.work / ".game-l10n/data-plane/inventory_reconciler"
        self.row = {"id": "synthetic-occurrence", "source": "{player}",
                    "metadata": {"ui_slot_role": "standalone", "ui_max_chars": 1}}
        self._inventory(self.primary, "synthetic-structural", [self.row])
        self._inventory(self.independent, "synthetic-independent", [self.row])
        self._verify()

    def _save_config(self) -> None:
        (self.work / "game-l10n.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.config = load_config(self.work)

    def _inventory(self, path: Path, method: str, rows: list[dict]) -> dict:
        payload = {
            "schema": "parr0t.source-inventory.v1", "method": method,
            "implementation_fingerprint": method + "-implementation",
            "coverage": {"declared_complete": False, "exclusions": []},
            "segments": deepcopy(rows),
        }
        self._write(path, payload)
        return payload

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _verify(self) -> dict:
        return save_inventory_attestation(self.work, self.primary, self.independent)

    def _prepare(self) -> dict:
        return prepare_data_plane_task(
            self.work, self.output, worker_role="inventory_reconciler",
            config=self.config, sdk_activation=self.activation,
        )

    def _seal(self, *, codes: list[str] | None = None, verify: bool = True) -> dict:
        if verify:
            self._verify()
        task = json.loads((self.output / "task.json").read_text(encoding="utf-8"))
        self._write(self.work / task["output_envelope"], {
            "schema": "parr0t.control-envelope.v2", "worker_role": "inventory_reconciler",
            "stage": "inventory", "status": "partial", "target_ids": [],
            "counts": {"total": 1, "success": 0, "pending": 1},
            "issue_codes": codes if codes is not None else ["constraint_unsatisfiable"],
            "artifact_paths": [".game-l10n/inventory-attestation.json"],
            "queue_transitions": [], "route_evidence_path": "",
        })
        return seal_data_plane_task(self.work, self.output / "relay.json", worker_ended=True)

    def _pending(self) -> dict | None:
        return current_inventory_constraint(
            self.work, config=self.config, sdk_activation=self.activation
        )

    def _block(self) -> dict:
        self.assertEqual(self._prepare()["status"], "ready")
        self.assertEqual(self._seal()["status"], "partial")
        pending = self._pending()
        self.assertIsNotNone(pending)
        return pending

    def test_unchanged_concrete_constraint_holds_without_new_dispatch(self) -> None:
        pending = self._block()
        task_bytes = (self.output / "task.json").read_bytes()
        self.assertEqual(pending["next_action"], "await_inventory_constraint_repair")
        self.assertFalse(pending["job_terminal"])
        self.assertFalse(pending["dispatch_allowed"])
        self.assertEqual(pending["issue_codes"], ["inventory_constraint_repair_required"])
        self.assertEqual(pending["counts"], {"blocking_constraints": 2})
        for _ in range(2):
            self.assertEqual(self._prepare()["status"], "pending")
        self.assertEqual((self.output / "task.json").read_bytes(), task_bytes)
        with patch("game_l10n.inventory_constraints.verify_inventory_pair",
                   side_effect=AssertionError("status must not parse inventory")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["status", "--workspace", str(self.work)]), 0)
            public = json.loads(output.getvalue())
        self.assertEqual(public["next_actions"], ["await_inventory_constraint_repair"])
        self.assertIsNone(public.get("data_plane_task"))
        rendered = json.dumps(pending)
        self.assertNotIn(self.row["id"], rendered)
        self.assertNotIn(self.row["source"], rendered)
        self.assertNotIn(str(self.work), rendered)

    def test_inventory_diagnostic_codes_do_not_hide_a_proven_deterministic_constraint(self) -> None:
        self._prepare()
        self._seal(codes=["constraint_unsatisfiable", "inventory_order_mismatch",
                          "inventory_not_declared_complete",
                          "independent_inventory_missed_primary_targets"])
        pending = self._pending()
        self.assertIsNotNone(pending)
        self.assertFalse(pending["dispatch_allowed"])
        self.assertFalse(pending["job_terminal"])

    def test_alternate_output_cannot_bypass_workspace_hold(self) -> None:
        self._block()
        alternate = self.work / ".game-l10n/synthetic-alternate-dispatch"
        result = prepare_data_plane_task(
            self.work, alternate, worker_role="inventory_reconciler",
            config=self.config, sdk_activation=self.activation,
        )
        self.assertEqual(result["status"], "pending")
        self.assertFalse(alternate.exists())

    def test_control_generation_and_envelope_bytes_invalidate_hold(self) -> None:
        self._block()
        with patch("game_l10n.data_plane_task.control_plane_state",
                   return_value={"generation": 999}):
            self.assertIsNone(self._pending())
        self.assertIsNotNone(self._pending())
        task = json.loads((self.output / "task.json").read_text())
        envelope = self.work / task["output_envelope"]
        envelope.write_bytes(envelope.read_bytes() + b" ")
        self.assertIsNone(self._pending())

    def test_meaningful_partial_progress_remains_executable(self) -> None:
        extra = {"id": "synthetic-extra", "source": "EXTRA"}
        self._inventory(self.independent, "synthetic-independent", [self.row, extra])
        initial = self._verify()
        self._prepare()
        self._inventory(self.independent, "synthetic-independent", [self.row])
        sealed = self._seal()
        self.assertEqual(sealed["status"], "partial")
        self.assertLess(self._verify()["gap_score"], initial["gap_score"])
        self.assertIsNone(self._pending())
        self.assertEqual(self._prepare()["status"], "ready")
        self._seal()
        self.assertIsNotNone(self._pending())

    def test_input_evidence_and_capability_changes_resume_without_erasing_history(self) -> None:
        self._block()
        historical = next(self.output.glob("inventory-constraint.*.json"))
        history_bytes = historical.read_bytes()
        original = self.independent.read_bytes()
        payload = json.loads(original)
        payload["segments"][0]["metadata"]["synthetic_evidence_revision"] = 2
        self._write(self.independent, payload)
        self.assertIsNone(self._pending())
        self.independent.write_bytes(original)
        self.assertIsNotNone(self._pending())
        self.activation["revision"] = 2
        self.assertIsNone(self._pending())
        resumed = self._prepare()
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(historical.read_bytes(), history_bytes)
        self.assertEqual(json.loads((self.output / "task.json").read_text())["attempt"], 2)

    def test_fingerprint_and_config_changes_invalidate_bound_hold(self) -> None:
        self._block()
        state_path = self.work / ".game-l10n/inventory-attestation.json"
        original = state_path.read_bytes()
        state = json.loads(original)
        state["primary_fingerprint"] = "synthetic-corrected-eligibility"
        self._write(state_path, state)
        self.assertIsNone(self._pending())
        state_path.write_bytes(original)
        self.config["routing"]["extra_protected_patterns"] = ["SYNTHETIC"]
        self._save_config()
        self.assertIsNone(self._pending())
        self.assertEqual(self._prepare()["status"], "ready")

    def test_tampered_or_foreign_seal_never_certifies_success(self) -> None:
        self._block()
        state_path = next(self.output.glob("inventory-constraint.*.json"))
        original = state_path.read_bytes()
        for key, value in (("task_id", "f" * 64), ("control_generation", 999),
                           ("constraint_count", 0), ("state_fingerprint", "0" * 64)):
            with self.subTest(field=key):
                state = json.loads(original)
                state[key] = value
                self._write(state_path, state)
                self.assertIsNone(self._pending())
        state_path.write_bytes(original)
        receipt_path = self.output / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["task_id"] = "e" * 64
        self._write(receipt_path, receipt)
        self.assertIsNone(self._pending())
        self.assertEqual(self._prepare()["status"], "ready")

    def test_assertions_and_provider_failures_are_not_constraint_evidence(self) -> None:
        for codes in (["provider_error"], ["task_incomplete"], ["malformed_response"],
                      ["constraint_unsatisfiable", "provider_error"]):
            with self.subTest(codes=codes):
                self.assertEqual(self._prepare()["status"], "ready")
                self._seal(codes=codes)
                self.assertIsNone(self._pending())
        self._inventory(self.primary, "synthetic-structural", [
            {"id": self.row["id"], "source": "{player}", "metadata": {"ui_slot_role": "standalone", "ui_max_chars": 100}}
        ])
        self._inventory(self.independent, "synthetic-independent", [
            {"id": self.row["id"], "source": "{player}", "metadata": {"ui_slot_role": "standalone", "ui_max_chars": 100}}
        ])
        self._verify()
        self._prepare()
        self._seal()
        self.assertIsNone(self._pending())

    def test_legacy_receipt_and_missing_output_keep_normal_resume(self) -> None:
        self._prepare()
        result = seal_data_plane_task(
            self.work, self.output / "relay.json", worker_ended=True
        )
        self.assertEqual(result["issue_codes"], ["task_incomplete"])
        self.assertIsNone(self._pending())
        receipt_path = self.output / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["schema"] = "parr0t.data-plane-receipt.v1"
        receipt.pop("execution")
        self._write(receipt_path, receipt)
        self.assertEqual(self._prepare()["status"], "ready")

    def test_operational_error_keeps_configured_native_route_progression(self) -> None:
        self.config["routing"]["data_plane_routes"] = {
            "inventory_reconciler": ["native-sol", "native-luna"]
        }
        self._save_config()
        self._prepare()
        self._seal(codes=["provider_error"])
        receipt_path = self.output / "receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["issue_codes"] = ["operational_error", "execution_guard_denied"]
        receipt["status"] = "error"
        self._write(receipt_path, receipt)
        self.assertIsNone(self._pending())
        self.assertEqual(self._prepare()["status"], "ready")
        task = json.loads((self.output / "task.json").read_text())
        self.assertEqual(task["worker_route"], "native-luna")
        self.assertEqual(task["data_plane_route_index"], 1)

    def _scope_correction(self, *, projection: bool = False) -> tuple[dict, Path]:
        plain = {"id": self.row["id"], "source": "SYNTHETIC_STORAGE"}
        retained = {"id": "synthetic-display", "source": "SYNTHETIC_DISPLAY", "metadata": {
            "display_binding_id": "synthetic-render-sink", "display_scope_evidence": {
                "kind": "positive_display_binding", "evidence_fingerprint": "c" * 64,
                "source_sha256": sha256(b"SYNTHETIC_DISPLAY").hexdigest(),
            },
        }}
        primary = self._inventory(self.primary, "synthetic-structural", [plain, retained])
        self._inventory(self.independent, "synthetic-independent", [plain, retained])
        self._verify()
        basis = self.primary.with_name("synthetic-canonical-basis.json")
        basis.write_bytes(self.primary.read_bytes())
        primary["coverage"]["scope_correction"] = {
            "schema": "parr0t.inventory-scope-correction.v1",
            "basis": {"file": basis.name, "sha256": sha256(basis.read_bytes()).hexdigest()},
            "records": [{
                "canonical_id": plain["id"], "comparison_key": plain["id"],
                "source_sha256": sha256(plain["source"].encode()).hexdigest(),
                "classification": "control_storage_with_projection" if projection else "proven_non_display",
                "projection_ids": ["synthetic-unbound-projection"] if projection else [],
                "evidence": [
                    {"method": method, "implementation_fingerprint": method + "-implementation",
                     "evidence_sha256": digest * 64}
                    for method, digest in (("synthetic-structural", "a"),
                                           ("synthetic-independent", "b"))
                ],
            }],
        }
        primary["coverage"]["display_scope"] = {
            "schema": "parr0t.display-scope.v1", "raw_occurrence_count": 2,
            "display_candidate_count": 1, "control_only_count": 1,
            "unclassified_count": 0, "evidence_fingerprint": "d" * 64,
        }
        self._write(self.primary, primary)
        return primary, basis

    def test_core_validated_scope_disagreement_is_a_concrete_constraint(self) -> None:
        _, basis = self._scope_correction()
        state = self._verify()
        self.assertIn("INVENTORY_SCOPE_CORRECTION_MISMATCH", state["issue_codes"])
        self._block()
        original = basis.read_bytes()
        basis.write_bytes(original + b" ")
        self.assertIsNone(self._pending())
        self.assertEqual(self._prepare()["status"], "ready")
        self.assertIsNone(self._pending())

    def test_agreed_scope_repair_resumes_with_canonical_history_intact(self) -> None:
        primary, _ = self._scope_correction()
        self._verify()
        self._block()
        history = next(self.output.glob("inventory-constraint.*.json"))
        canonical_ids = [row["id"] for row in primary["segments"]]
        independent = json.loads(self.independent.read_text())
        independent["coverage"]["scope_correction"] = deepcopy(
            primary["coverage"]["scope_correction"]
        )
        independent["coverage"]["display_scope"] = deepcopy(primary["coverage"]["display_scope"])
        self._write(self.independent, independent)
        corrected = self._verify()
        self.assertNotIn("INVENTORY_SCOPE_CORRECTION_MISMATCH", corrected["issue_codes"])
        self.assertIsNone(self._pending())
        self.assertEqual(self._prepare()["status"], "ready")
        self.assertTrue(history.is_file())
        self.assertEqual([row["id"] for row in json.loads(self.primary.read_text())["segments"]],
                         canonical_ids)

    def test_projection_constraint_requires_current_complete_pair_attestation(self) -> None:
        self._scope_correction(projection=True)
        self._verify()
        self._prepare()
        sealed = self._seal()
        self.assertEqual(sealed["status"], "partial")
        pending = self._pending()
        self.assertIsNotNone(pending)
        self.assertFalse(pending["job_terminal"])
        self.assertEqual(self._prepare()["status"], "pending")

    def test_tampered_basis_never_becomes_constraint_evidence(self) -> None:
        _, basis = self._scope_correction(projection=True)
        basis.write_bytes(basis.read_bytes() + b" ")
        state = json.loads((self.work / ".game-l10n/inventory-attestation.json").read_text())
        with self.assertRaises(ConfigurationError):
            observe_inventory_constraints(self.work, state, self.config, self.activation, {})
        self._prepare()
        self._seal(verify=False)
        self.assertIsNone(self._pending())

    def test_projection_cannot_hide_malformed_independent_inventory(self) -> None:
        self._scope_correction(projection=True)
        original = json.loads(self.independent.read_text())
        attestation = json.loads((self.work / ".game-l10n/inventory-attestation.json").read_text())
        for field, value in (("schema", "unsupported-schema"), ("method", None), ("segments", [None])):
            with self.subTest(field=field):
                malformed = deepcopy(original)
                malformed[field] = value
                self._write(self.independent, malformed)
                with self.assertRaises(ConfigurationError):
                    observe_inventory_constraints(self.work, attestation, self.config, self.activation, {})
                self._prepare()
                self._seal(verify=False)
                self.assertIsNone(self._pending())

    def test_projection_cannot_certify_a_basis_older_than_accepted_history(self) -> None:
        proposed, _ = self._scope_correction(projection=True)
        correction = deepcopy(proposed["coverage"]["scope_correction"])
        tracking = deepcopy(proposed["coverage"]["display_scope"])
        del proposed["coverage"]["scope_correction"]
        del proposed["coverage"]["display_scope"]
        proposed["segments"][-1]["metadata"]["synthetic_evidence_revision"] = 2
        self._write(self.primary, proposed)
        accepted = self._verify()
        original_primary = deepcopy(proposed)
        original_independent = json.loads(self.independent.read_text())
        for side in ("primary", "independent"):
            with self.subTest(side=side):
                self._write(self.primary, original_primary)
                self._write(self.independent, original_independent)
                target = self.primary if side == "primary" else self.independent
                payload = json.loads(target.read_text())
                payload["coverage"].update(scope_correction=correction, display_scope=tracking)
                self._write(target, payload)
                with self.assertRaisesRegex(ConfigurationError, "BASIS_STALE"):
                    self._verify()
                with self.assertRaisesRegex(ConfigurationError, "BASIS_STALE"):
                    observe_inventory_constraints(self.work, accepted, self.config, self.activation, {})
                self._prepare()
                self._seal(verify=False)
                self.assertIsNone(self._pending())


if __name__ == "__main__":
    unittest.main()
