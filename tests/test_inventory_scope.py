from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from game_l10n.coverage import (
    record_runtime_coverage, require_inventory_attestation, save_inventory_attestation,
    verify_inventory_pair,
)
from game_l10n.errors import ConfigurationError
from game_l10n.formats import read_segments
from game_l10n.inventory_scope import SCOPE_CORRECTION_SCHEMA, inventory_scope


def _row(target_id: str, key: str, source: str) -> dict:
    return {"id": target_id, "source": source, "metadata": {
        "inventory_comparison_key": key,
        "display_binding_id": "render-" + key,
        "display_scope_evidence": {
            "kind": "positive_display_binding", "evidence_fingerprint": "c" * 64,
            "source_sha256": sha256(source.encode()).hexdigest(),
        },
    }}


class InventoryScopeCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.primary_path = self.root / "primary.json"
        self.independent_path = self.root / "independent.json"
        self.basis_path = self.root / "canonical-basis.json"
        self.control = _row("canonical-control", "control", "SYNTHETIC_CONTROL_TOKEN")
        self.control["metadata"]["storage_role"] = "lookup_key"
        self.label = _row("canonical-label", "label", "SYNTHETIC LABEL")
        self.tail = _row("canonical-tail", "tail", "SYNTHETIC TAIL")
        self.primary = {
            "schema": "parr0t.source-inventory.v1", "method": "synthetic-structural",
            "implementation_fingerprint": "synthetic-structural-v1",
            "coverage": {"declared_complete": True, "exclusions": []},
            "segments": [self.control, self.label, self.tail],
        }
        self.independent = {
            "schema": "parr0t.source-inventory.v1", "method": "synthetic-dataflow",
            "implementation_fingerprint": "synthetic-dataflow-v1",
            "coverage": {"declared_complete": True, "exclusions": []},
            "segments": [deepcopy(self.label), deepcopy(self.tail)],
        }
        for row in self.independent["segments"]:
            row["id"] = row["id"].replace("canonical", "independent")
        self._write()
        self.initial = save_inventory_attestation(self.root, self.primary_path, self.independent_path)
        self.basis_path.write_bytes(self.primary_path.read_bytes())
        self.correction = {
            "schema": SCOPE_CORRECTION_SCHEMA,
            "basis": {"file": self.basis_path.name, "sha256": sha256(self.basis_path.read_bytes()).hexdigest()},
            "records": [{
                "canonical_id": self.control["id"], "comparison_key": "control",
                "source_sha256": sha256(self.control["source"].encode()).hexdigest(),
                "classification": "proven_non_display", "projection_ids": [],
                "evidence": [
                    {"method": self.primary["method"],
                     "implementation_fingerprint": self.primary["implementation_fingerprint"],
                     "evidence_sha256": "a" * 64},
                    {"method": self.independent["method"],
                     "implementation_fingerprint": self.independent["implementation_fingerprint"],
                     "evidence_sha256": "b" * 64},
                ],
            }],
        }

    def _write(self) -> None:
        self.primary_path.write_text(json.dumps(self.primary), encoding="utf-8")
        self.independent_path.write_text(json.dumps(self.independent), encoding="utf-8")

    def _correct(self) -> None:
        for payload in (self.primary, self.independent):
            payload["coverage"]["scope_correction"] = deepcopy(self.correction)
            payload["coverage"]["display_scope"] = {
                "schema": "parr0t.display-scope.v1", "raw_occurrence_count": 3,
                "display_candidate_count": 2, "control_only_count": 1,
                "unclassified_count": 0, "evidence_fingerprint": "d" * 64,
            }
        self._write()

    def test_corrected_scope_preserves_history_but_removes_control_from_translation_and_context(self) -> None:
        self.label["previous"] = self.control["source"]
        self.tail["next"] = self.control["source"]
        self._correct()
        state = save_inventory_attestation(self.root, self.primary_path, self.independent_path)
        self.assertTrue(state["static_complete"])
        self.assertEqual(state["primary_occurrences"], 3)
        self.assertEqual(state["primary_segments"], 2)
        identity = json.loads((self.root / ".game-l10n/inventory-primary-identity.json").read_text())
        self.assertEqual(identity["ids"], ["canonical-control", "canonical-label", "canonical-tail"])
        self.assertEqual(json.loads(self.primary_path.read_text())["segments"][0]["source"],
                         "SYNTHETIC_CONTROL_TOKEN")
        segments = read_segments(self.primary_path)
        self.assertEqual([row.id for row in segments], ["canonical-label", "canonical-tail"])
        self.assertEqual(segments[0].next, "SYNTHETIC TAIL")
        self.assertEqual(segments[1].previous, "SYNTHETIC LABEL")
        self.assertEqual(segments[0].previous, "")
        self.assertEqual(segments[-1].next, "")
        self.assertEqual(require_inventory_attestation(self.root, self.primary_path)["primary_fingerprint"],
                         state["primary_fingerprint"])
        report = self.root / "synthetic-observation.json"
        report.write_text(json.dumps({
            "schema": "parr0t.runtime-coverage.v1", "inventory_fingerprint": state["primary_fingerprint"],
            "observation_method": "synthetic-fixture", "observed_target_ids": [row.id for row in segments],
        }))
        self.assertTrue(record_runtime_coverage(self.root, report)["runtime_complete"])

    def test_one_sided_proposal_is_incomplete_not_silent_scope_reduction(self) -> None:
        self._correct()
        del self.primary["coverage"]["scope_correction"]
        del self.primary["coverage"]["display_scope"]
        self._write()
        state = save_inventory_attestation(self.root, self.primary_path, self.independent_path)
        self.assertFalse(state["static_complete"])
        self.assertIn("INVENTORY_SCOPE_CORRECTION_MISMATCH", state["issue_codes"])
        with self.assertRaises(ConfigurationError):
            require_inventory_attestation(self.root, self.primary_path)

    def test_single_method_replayed_proof_and_wrong_occurrence_cannot_retire_text(self) -> None:
        for mutation in ("one-proof", "same-method", "same-digest", "source", "occurrence"):
            with self.subTest(mutation=mutation):
                self._correct()
                record = self.primary["coverage"]["scope_correction"]["records"][0]
                if mutation == "one-proof":
                    record["evidence"].pop()
                elif mutation == "same-method":
                    record["evidence"][1]["method"] = record["evidence"][0]["method"]
                elif mutation == "same-digest":
                    record["evidence"][1]["evidence_sha256"] = record["evidence"][0]["evidence_sha256"]
                elif mutation == "source":
                    record["source_sha256"] = "e" * 64
                else:
                    record["comparison_key"] = "another-physical-occurrence"
                self._write()
                with self.assertRaises(ConfigurationError):
                    verify_inventory_pair(self.primary_path, self.independent_path)

    def test_migration_cannot_remove_reorder_or_change_historical_source(self) -> None:
        identity_path = self.root / ".game-l10n/inventory-primary-identity.json"
        original_identity = identity_path.read_bytes()
        for mutation in ("remove", "reorder", "source"):
            with self.subTest(mutation=mutation):
                self.primary["segments"] = deepcopy([self.control, self.label, self.tail])
                self._correct()
                if mutation == "remove":
                    self.primary["segments"].pop(0)
                elif mutation == "reorder":
                    self.primary["segments"].reverse()
                else:
                    self.primary["segments"][0]["source"] = "CHANGED SYNTHETIC CONTROL"
                self._write()
                with self.assertRaises(ConfigurationError):
                    save_inventory_attestation(self.root, self.primary_path, self.independent_path)
                self.assertEqual(identity_path.read_bytes(), original_identity)

    def test_changed_basis_and_changed_independent_proof_invalidate_cached_readiness(self) -> None:
        self._correct()
        save_inventory_attestation(self.root, self.primary_path, self.independent_path)
        raw = self.basis_path.read_bytes()
        self.basis_path.write_bytes(raw + b" ")
        with self.assertRaises(ConfigurationError):
            require_inventory_attestation(self.root, self.primary_path)
        with self.assertRaises(ConfigurationError):
            read_segments(self.primary_path)
        self.basis_path.write_bytes(raw)
        self.independent["coverage"]["scope_correction"]["records"][0]["evidence"][1]["evidence_sha256"] = "e" * 64
        self._write()
        with self.assertRaises(ConfigurationError):
            require_inventory_attestation(self.root, self.primary_path)

    def test_basis_must_be_the_bound_canonical_history_not_a_replacement_snapshot(self) -> None:
        forged = deepcopy(self.primary)
        forged["coverage"]["declared_complete"] = False
        self.basis_path.write_text(json.dumps(forged))
        self.correction["basis"]["sha256"] = sha256(self.basis_path.read_bytes()).hexdigest()
        self._correct()
        with self.assertRaisesRegex(ConfigurationError, "BASIS_STALE"):
            save_inventory_attestation(self.root, self.primary_path, self.independent_path)

    def test_control_retirement_needs_a_separate_positive_display_projection(self) -> None:
        record = self.correction["records"][0]
        record["classification"] = "control_storage_with_projection"
        self._correct()
        incomplete = verify_inventory_pair(self.primary_path, self.independent_path)
        self.assertFalse(incomplete["static_complete"])
        self.assertIn("INVENTORY_SCOPE_PROJECTION_REQUIRED", incomplete["issue_codes"])
        with self.assertRaisesRegex(ConfigurationError, "PROJECTION_REQUIRED"):
            read_segments(self.primary_path)
        record["projection_ids"] = ["render-label"]
        for payload in (self.primary, self.independent):
            for row in payload["segments"]:
                if row["metadata"]["inventory_comparison_key"] == "label":
                    row["metadata"]["display_projection_of"] = "control"
        self._correct()
        state = save_inventory_attestation(self.root, self.primary_path, self.independent_path)
        self.assertTrue(state["static_complete"])
        self.assertNotIn("canonical-control", [row.id for row in read_segments(self.primary_path)])

    def test_invalid_basis_cannot_masquerade_as_a_valid_projection_constraint(self) -> None:
        self.correction["records"][0]["classification"] = "control_storage_with_projection"
        self.correction["basis"]["sha256"] = "0" * 64
        self._correct()
        with self.assertRaisesRegex(ConfigurationError, "CORRECTION_INVALID"):
            inventory_scope(self.primary, self.primary_path)

    def test_corrected_scope_still_requires_display_proof_and_cannot_discard_its_history(self) -> None:
        self._correct()
        del self.primary["coverage"]["display_scope"]
        self._write()
        with self.assertRaises(ConfigurationError):
            verify_inventory_pair(self.primary_path, self.independent_path)
        self._correct()
        save_inventory_attestation(self.root, self.primary_path, self.independent_path)
        del self.primary["coverage"]["scope_correction"]
        del self.independent["coverage"]["scope_correction"]
        self._write()
        with self.assertRaises(ConfigurationError):
            save_inventory_attestation(self.root, self.primary_path, self.independent_path)

    def test_post_basis_additions_keep_their_attested_source_and_physical_occurrence(self) -> None:
        self._correct()
        save_inventory_attestation(self.root, self.primary_path, self.independent_path)
        added = _row("canonical-added", "added", "SYNTHETIC ADDED DISPLAY")
        self.primary["segments"].append(added)
        independent_added = deepcopy(added)
        independent_added["id"] = "independent-added"
        self.independent["segments"].append(independent_added)
        for payload in (self.primary, self.independent):
            payload["coverage"]["display_scope"].update(raw_occurrence_count=4, display_candidate_count=3)
        self._write()
        save_inventory_attestation(self.root, self.primary_path, self.independent_path)
        primary_before, independent_before = deepcopy(self.primary), deepcopy(self.independent)
        identity_path = self.root / ".game-l10n/inventory-primary-identity.json"
        identity_before = identity_path.read_bytes()
        for field in ("source", "comparison_key"):
            with self.subTest(field=field):
                self.primary, self.independent = deepcopy(primary_before), deepcopy(independent_before)
                for payload in (self.primary, self.independent):
                    row = payload["segments"][-1]
                    if field == "source":
                        row["source"] = "CHANGED SYNTHETIC DISPLAY"
                        row["metadata"]["display_scope_evidence"]["source_sha256"] = sha256(row["source"].encode()).hexdigest()
                    else:
                        row["metadata"]["inventory_comparison_key"] = "different-physical-occurrence"
                self._write()
                with self.assertRaisesRegex(ConfigurationError, "PRIMARY_OCCURRENCE_DRIFT"):
                    save_inventory_attestation(self.root, self.primary_path, self.independent_path)
                self.assertEqual(identity_path.read_bytes(), identity_before)

    def test_pair_verification_does_not_reuse_a_basis_from_an_earlier_call(self) -> None:
        self._correct()
        self.assertTrue(verify_inventory_pair(self.primary_path, self.independent_path)["static_complete"])
        raw = self.basis_path.read_bytes()
        self.basis_path.write_bytes(raw + b" ")
        with self.assertRaisesRegex(ConfigurationError, "CORRECTION_INVALID"):
            verify_inventory_pair(self.primary_path, self.independent_path)
        self.basis_path.write_bytes(raw)
        self.assertTrue(verify_inventory_pair(self.primary_path, self.independent_path)["static_complete"])

    def test_shared_basis_still_checks_each_methods_expected_digest(self) -> None:
        self._correct()
        self.independent["coverage"]["scope_correction"]["basis"]["sha256"] = "0" * 64
        self._write()
        with self.assertRaisesRegex(ConfigurationError, "CORRECTION_INVALID"):
            verify_inventory_pair(self.primary_path, self.independent_path)



if __name__ == "__main__":
    unittest.main()
