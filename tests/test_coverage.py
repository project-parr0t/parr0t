from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_l10n.coverage import (
    completion_attestation,
    record_runtime_coverage,
    require_inventory_attestation,
    runtime_coverage_state,
    save_inventory_attestation,
    verify_inventory_pair,
)
from game_l10n.cli import _finalize_translation_results
from game_l10n.errors import ConfigurationError
from game_l10n.failure_queue import load_failure_queue, update_failure_queue
from game_l10n.models import AttemptRecord, Segment, TranslationResult
from game_l10n.store import stable_hash


DEFAULT_LANGUAGE_BINDING = {
    "source_language": "en",
    "target_language": "ko",
    "policy_revision": 0,
    "policy_digest": stable_hash(
        {
            "target_language": "ko",
            "revision": 0,
            "authorization": "default_korean",
        }
    ),
}


def write_inventory(
    path: Path,
    method: str,
    segments: list[dict],
    *,
    declared_complete: bool = True,
    exclusions: list[dict] | None = None,
    static_scope: dict | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "parr0t.source-inventory.v1",
                "method": method,
                "implementation_fingerprint": f"{method}-implementation",
                "coverage": {
                    "declared_complete": declared_complete,
                    "exclusions": exclusions or [],
                    **({"static_scope": static_scope} if static_scope is not None else {}),
                },
                "segments": segments,
            }
        ),
        encoding="utf-8",
    )


class CoverageTests(unittest.TestCase):
    @staticmethod
    def _static_scope(*, unused: int = 1) -> dict:
        return {
            "schema": "parr0t.static-scope.v1",
            "claim": "static_reachability_only",
            "engine_capability": "synthetic-fixed-path-graph-v1",
            "candidate_count": 2,
            "included_count": 2 - unused,
            "proven_unused_count": unused,
            "conservative_retained_count": 0,
            "unresolved_dynamic_reference_count": 0,
            "methods": [
                {"name": "synthetic-bfs", "implementation_fingerprint": "bfs-v1"},
                {"name": "synthetic-fixed-point", "implementation_fingerprint": "fixed-point-v1"},
            ],
            "methods_agree": True,
            "evidence_fingerprint": "fixture-scope-evidence",
        }

    def test_independent_static_scope_can_account_for_proven_unused_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [{"id": "synthetic-active", "source": "SOURCE_ACTIVE"}]
            scope = self._static_scope()
            exclusions = [{
                "reason_code": "SYNTHETIC_STATIC_UNREACHABLE",
                "affects_user_visible_text": False,
                "scope": "static_reachability",
                "target_count": 1,
                "evidence_fingerprint": "fixture-exclusion-evidence",
            }]
            write_inventory(primary, "synthetic-bfs", rows, exclusions=exclusions, static_scope=scope)
            write_inventory(independent, "synthetic-fixed-point", rows, exclusions=exclusions, static_scope=scope)

            state = verify_inventory_pair(primary, independent)

        self.assertTrue(state["static_complete"])
        self.assertEqual(state["static_scope"]["proven_unused_count"], 1)
        self.assertFalse(state["static_scope_tracking_mismatch"])

    def test_static_scope_disagreement_cannot_silently_filter_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [{"id": "synthetic-active", "source": "SOURCE_ACTIVE"}]
            exclusions = [{
                "reason_code": "SYNTHETIC_STATIC_UNREACHABLE",
                "affects_user_visible_text": False,
                "scope": "static_reachability",
                "target_count": 1,
                "evidence_fingerprint": "fixture-exclusion-evidence",
            }]
            first = self._static_scope()
            second = {**first, "evidence_fingerprint": "different-evidence"}
            write_inventory(primary, "synthetic-bfs", rows, exclusions=exclusions, static_scope=first)
            write_inventory(independent, "synthetic-fixed-point", rows, exclusions=exclusions, static_scope=second)

            state = verify_inventory_pair(primary, independent)

        self.assertFalse(state["static_complete"])
        self.assertTrue(state["static_scope_tracking_mismatch"])
        self.assertIn("STATIC_SCOPE_TRACKING_MISMATCH", state["issue_codes"])

    def test_verified_inventory_uses_unchanged_file_binding_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [{"id": "fixture-1", "source": "SOURCE"}]
            write_inventory(primary, "primary", rows)
            write_inventory(independent, "independent", rows)
            saved = save_inventory_attestation(root, primary, independent)

            with patch(
                "game_l10n.coverage._inventory_document",
                side_effect=AssertionError("unchanged inventory was reparsed"),
            ):
                loaded = require_inventory_attestation(root, primary)

        self.assertEqual(loaded["primary_fingerprint"], saved["primary_fingerprint"])

    def test_retry_approval_requires_current_generation_and_approved_status(self) -> None:
        segment = Segment("synthetic-retry", "SOURCE")
        for stage in ("review-grace-retry", "review-refusal-retry"):
            for generation, verdict, expected in (
                (2, "approved", True),
                (1, "approved", False),
                (2, "error", False),
            ):
                with self.subTest(stage=stage, generation=generation, verdict=verdict):
                    result = TranslationResult(
                        segment_id=segment.id,
                        source=segment.source,
                        text="TARGET",
                        status="reviewed",
                        acceptance_authority="reviewer",
                        candidate_generation=2,
                        candidate_digest=stable_hash("TARGET"),
                        attempts=[
                            AttemptRecord(
                                stage,
                                "synthetic-reviewer",
                                verdict,
                                candidate_generation=generation,
                            )
                        ],
                    )
                    state = completion_attestation(
                        [segment],
                        [result],
                        inventory_segments=[segment],
                        inventory={
                            "status": "static_verified",
                            "static_complete": True,
                            "primary_segments": 1,
                            "primary_fingerprint": "synthetic",
                        },
                        runtime={"runtime_complete": False},
                        review_required=True,
                    )
                    self.assertEqual(state["semantic_review"]["complete"], expected)
                    self.assertEqual(state["static_completion"], expected)

    def test_selected_strong_target_cannot_complete_with_triage_verdict_only(self) -> None:
        segment = Segment("synthetic-selected", "SOURCE")
        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            text="TARGET",
            status="reviewed",
            acceptance_authority="reviewer",
            candidate_generation=1,
            candidate_digest=stable_hash("TARGET"),
            metadata={"review_tier": "strong_pending"},
            attempts=[
                AttemptRecord(
                    "review-triage",
                    "synthetic-triager",
                    "approved",
                    candidate_generation=1,
                )
            ],
        )

        state = completion_attestation(
            [segment],
            [result],
            inventory_segments=[segment],
            inventory={
                "status": "static_verified",
                "static_complete": True,
                "primary_segments": 1,
                "primary_fingerprint": "synthetic",
            },
            runtime={"runtime_complete": False},
            review_required=True,
        )

        self.assertFalse(state["semantic_review"]["complete"])
        self.assertFalse(state["static_completion"])

    def test_independent_inventory_gate_is_bound_to_primary_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [{"id": "synthetic-1", "source": "SOURCE_ONE"}]
            write_inventory(primary, "synthetic-ast", rows)
            write_inventory(independent, "synthetic-runtime-table", rows)
            state = save_inventory_attestation(root, primary, independent)

            self.assertTrue(state["static_complete"])
            self.assertEqual(
                require_inventory_attestation(root, primary)["status"], "static_verified"
            )

            write_inventory(
                primary,
                "synthetic-ast",
                [*rows, {"id": "synthetic-2", "source": "SOURCE_TWO"}],
            )
            with self.assertRaisesRegex(ConfigurationError, "changed"):
                require_inventory_attestation(root, primary)

    def test_user_visible_exclusion_prevents_static_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [{"id": "synthetic-1", "source": "SOURCE_ONE"}]
            write_inventory(primary, "synthetic-ast", rows)
            write_inventory(
                independent,
                "synthetic-secondary",
                rows,
                exclusions=[
                    {
                        "reason_code": "SYNTHETIC_DYNAMIC_SCOPE",
                        "affects_user_visible_text": True,
                    }
                ],
            )

            state = verify_inventory_pair(primary, independent)

            self.assertFalse(state["static_complete"])
            self.assertIn("USER_VISIBLE_SCOPE_EXCLUDED", state["issue_codes"])

    def test_independent_inventory_must_confirm_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [
                {"id": "synthetic-1", "source": "SOURCE_ONE"},
                {"id": "synthetic-2", "source": "SOURCE_TWO"},
            ]
            write_inventory(primary, "synthetic-ast", rows)
            write_inventory(independent, "synthetic-secondary", list(reversed(rows)))

            state = verify_inventory_pair(primary, independent)

        self.assertFalse(state["static_complete"])
        self.assertTrue(state["order_mismatch"])
        self.assertIn("INVENTORY_ORDER_MISMATCH", state["issue_codes"])

    def test_method_specific_ids_can_share_a_canonical_comparison_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            primary_rows = [
                {
                    "id": "primary-label-id",
                    "source": "SOURCE_LABEL",
                    "metadata": {
                        "inventory_comparison_key": "physical-label-occurrence",
                        "ui_slot_role": "label",
                    },
                },
                {
                    "id": "primary-resource-id",
                    "source": "SOURCE_ONE",
                    "metadata": {
                        "inventory_comparison_key": "physical-occurrence-1",
                        "ui_slot_role": "value",
                        "ui_composed_with_ids": ["primary-label-id"],
                    },
                }
            ]
            independent_rows = [
                {
                    "id": "independent-label-id",
                    "source": "SOURCE_LABEL",
                    "metadata": {
                        "inventory_comparison_key": "physical-label-occurrence",
                        "ui_slot_role": "label",
                    },
                },
                {
                    "id": "independent-resource-id",
                    "source": "SOURCE_ONE",
                    "metadata": {
                        "inventory_comparison_key": "physical-occurrence-1",
                        "ui_slot_role": "value",
                        "ui_composed_with_ids": ["independent-label-id"],
                    },
                }
            ]
            write_inventory(primary, "synthetic-ast", primary_rows)
            write_inventory(independent, "synthetic-secondary", independent_rows)

            state = verify_inventory_pair(primary, independent)

        self.assertTrue(state["static_complete"])
        self.assertEqual(state["missing_from_primary_count"], 0)
        self.assertEqual(state["missing_from_independent_count"], 0)
        self.assertFalse(state["order_mismatch"])

    def test_disjoint_id_namespaces_are_not_counted_as_whole_content_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            primary_rows = [
                {"id": f"primary-{index}", "source": f"SOURCE_{index}"}
                for index in range(10)
            ]
            independent_rows = [
                {"id": f"secondary-{index}", "source": f"SOURCE_{index}"}
                for index in range(10)
            ]
            write_inventory(primary, "synthetic-ast", primary_rows)
            write_inventory(independent, "synthetic-table", independent_rows)

            state = verify_inventory_pair(primary, independent)

        self.assertFalse(state["static_complete"])
        self.assertIn(
            "INVENTORY_IDENTITY_NAMESPACE_MISMATCH", state["issue_codes"]
        )
        self.assertEqual(state["identity_gate"]["status"], "namespace_mismatch")
        self.assertEqual(state["identity_gate"]["raw_id_overlap_count"], 0)
        self.assertTrue(
            state["identity_gate"]["source_multiset_overlap_measured"]
        )
        self.assertEqual(
            state["identity_gate"]["source_multiset_overlap_ratio"], 1.0
        )
        self.assertEqual(state["gap_score"], 2)

    def test_saved_primary_identity_is_append_only_across_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [
                {"id": "stable-1", "source": "SOURCE_ONE"},
                {"id": "stable-2", "source": "SOURCE_TWO"},
            ]
            write_inventory(primary, "synthetic-ast", rows)
            write_inventory(independent, "synthetic-table", rows)
            first = save_inventory_attestation(root, primary, independent)

            extended = [*rows, {"id": "stable-3", "source": "SOURCE_THREE"}]
            write_inventory(primary, "synthetic-ast", extended)
            write_inventory(independent, "synthetic-table", extended)
            second = save_inventory_attestation(root, primary, independent)

            rewritten = [
                {"id": "replacement-1", "source": "SOURCE_ONE"},
                {"id": "replacement-2", "source": "SOURCE_TWO"},
                {"id": "replacement-3", "source": "SOURCE_THREE"},
            ]
            write_inventory(primary, "synthetic-ast", rewritten)
            write_inventory(independent, "synthetic-table", rewritten)
            with self.assertRaisesRegex(
                ConfigurationError, "E_INVENTORY_PRIMARY_IDENTITY_DRIFT"
            ):
                save_inventory_attestation(root, primary, independent)

        self.assertEqual(
            first["primary_identity_binding"]["policy"],
            "append_only_preserve_existing_id_order",
        )
        self.assertEqual(second["primary_identity_binding"]["records"], 3)

    def test_inventory_comparison_keys_must_be_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [
                {
                    "id": "first",
                    "source": "SOURCE_ONE",
                    "metadata": {"inventory_comparison_key": "same-occurrence"},
                },
                {
                    "id": "second",
                    "source": "SOURCE_TWO",
                    "metadata": {"inventory_comparison_key": "same-occurrence"},
                },
            ]
            write_inventory(primary, "synthetic-ast", rows)
            write_inventory(independent, "synthetic-secondary", rows)

            with self.assertRaisesRegex(ConfigurationError, "duplicate comparison key"):
                verify_inventory_pair(primary, independent)

    def test_saved_inventory_attestation_records_measured_gap_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            rows = [
                {"id": "first", "source": "SOURCE_ONE"},
                {"id": "second", "source": "SOURCE_TWO"},
            ]
            write_inventory(primary, "synthetic-ast", rows)
            write_inventory(independent, "synthetic-secondary", rows[:1])
            first = save_inventory_attestation(root, primary, independent)

            write_inventory(independent, "synthetic-secondary", rows)
            second = save_inventory_attestation(root, primary, independent)

        self.assertEqual(first["reconciliation"]["progress_state"], "initial")
        self.assertTrue(first["reconciliation"]["continuation_required"])
        self.assertEqual(second["reconciliation"]["progress_state"], "complete")
        self.assertFalse(second["reconciliation"]["continuation_required"])
        self.assertFalse(second["reconciliation"]["terminal"])

    def test_independent_inventory_must_confirm_ui_composition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            primary_rows = [
                {
                    "id": "synthetic-state",
                    "source": "SOURCE_STATE",
                    "metadata": {
                        "ui_slot_role": "state_value",
                        "ui_state_semantics": "enabled",
                        "ui_composed_with_ids": ["synthetic-label"],
                    },
                }
            ]
            independent_rows = [
                {
                    "id": "synthetic-state",
                    "source": "SOURCE_STATE",
                    "metadata": {"ui_slot_role": "standalone"},
                }
            ]
            write_inventory(primary, "synthetic-ast", primary_rows)
            write_inventory(independent, "synthetic-secondary", independent_rows)

            state = verify_inventory_pair(primary, independent)

        self.assertFalse(state["static_complete"])
        self.assertEqual(state["ui_composition_mismatch_count"], 1)
        self.assertIn("INVENTORY_UI_COMPOSITION_MISMATCH", state["issue_codes"])

    def test_completion_separates_manifest_review_and_runtime_claims(self) -> None:
        segment = Segment("synthetic-1", "SOURCE_ONE")
        text = "TARGET_ONE"
        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            text=text,
            status="reviewed",
            reviewer="synthetic-reviewer",
            acceptance_authority="reviewer",
            candidate_digest=stable_hash(text),
            candidate_generation=1,
            attempts=[
                AttemptRecord(
                    stage="review",
                    provider="synthetic-reviewer",
                    status="approved",
                    candidate_generation=1,
                )
            ],
        )
        inventory = {
            "status": "static_verified",
            "static_complete": True,
            "primary_segments": 1,
            "primary_fingerprint": "synthetic-inventory-fingerprint",
        }
        runtime = {"status": "not_run", "runtime_complete": False, "issue_codes": ["REAL_RUNTIME_NOT_EXECUTED"]}

        state = completion_attestation(
            [segment],
            [result],
            inventory_segments=[segment],
            inventory=inventory,
            runtime=runtime,
            review_required=True,
        )

        self.assertTrue(state["result_alignment"]["complete"])
        self.assertTrue(state["semantic_review"]["complete"])
        self.assertTrue(state["static_completion"])
        self.assertEqual(
            state["source_inventory"]["fingerprint"],
            "synthetic-inventory-fingerprint",
        )
        self.assertFalse(state["runtime_completion"])
        self.assertIn("REAL_RUNTIME_NOT_EXECUTED", state["issue_codes"])

    def test_operational_blocking_failure_is_queued_and_patch_must_hold(self) -> None:
        segment = Segment(
            "synthetic-critical",
            "SOURCE_CRITICAL",
            metadata={"blocking_on_failure": True},
        )
        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            text=segment.source,
            status="source_preserved",
            attempts=[
                AttemptRecord(
                    stage="translate",
                    provider="synthetic-provider",
                    status="error",
                    reason="rate_limited",
                    issue_code="rate_limited",
                )
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = update_failure_queue(
                root,
                [segment],
                [result],
                inventory_fingerprint="synthetic-fingerprint",
            )
            loaded = load_failure_queue(root, "synthetic-fingerprint")

        self.assertEqual(state["operational_count"], 1)
        self.assertEqual(state["blocking_count"], 1)
        self.assertEqual(state["pending_count"], 0)
        self.assertEqual(state["terminal_count"], 1)
        self.assertFalse(
            loaded["terminal_entries"][0]["retryable_on_user_resume"]
        )

    def test_completion_rejects_reviewed_output_in_the_wrong_language(self) -> None:
        segment = Segment("dialogue-1", "Original source dialogue")
        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            text="This remains untranslated English prose.",
            status="reviewed",
            acceptance_authority="reviewer",
            candidate_generation=1,
            candidate_digest=stable_hash("This remains untranslated English prose."),
            attempts=[
                AttemptRecord(
                    stage="review",
                    provider="synthetic-reviewer",
                    status="approved",
                    candidate_generation=1,
                )
            ],
        )
        state = completion_attestation(
            [segment],
            [result],
            inventory_segments=[segment],
            inventory={
                "status": "static_verified",
                "static_complete": True,
                "primary_segments": 1,
                "primary_fingerprint": "fixture",
            },
            runtime={"runtime_complete": False, "issue_codes": []},
            review_required=True,
            target_language="ko",
            language_binding=DEFAULT_LANGUAGE_BINDING,
        )

        self.assertFalse(state["target_language"]["complete"])
        self.assertEqual(state["target_language"]["mismatch_count"], 1)
        self.assertFalse(state["target_completion"])
        self.assertIn("TARGET_LANGUAGE_MISMATCH", state["issue_codes"])

    def test_blocking_failure_emits_checkpoint_but_no_patch(self) -> None:
        segment = Segment(
            "synthetic-critical",
            "SOURCE_CRITICAL",
            metadata={"blocking_on_failure": True},
        )
        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            text=segment.source,
            status="source_preserved",
            attempts=[
                AttemptRecord(
                    stage="translate",
                    provider="synthetic-provider",
                    status="error",
                    reason="timeout",
                    issue_code="timeout",
                )
            ],
        )
        inventory = {
            "status": "static_verified",
            "static_complete": True,
            "primary_segments": 1,
            "primary_fingerprint": "synthetic-fingerprint",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "patch.json"
            summary, exit_code = _finalize_translation_results(
                work_dir=root,
                target_segments=[segment],
                inventory_segments=[segment],
                results=[result],
                destination=destination,
                inventory_state=inventory,
                review_required=True,
                language_binding_state=DEFAULT_LANGUAGE_BINDING,
            )

            self.assertFalse(destination.exists())
            self.assertTrue(Path(summary["held_checkpoint"]).exists())
        self.assertEqual(exit_code, 3)
        self.assertEqual(summary["status"], "held")
        self.assertFalse(summary["artifact_emitted"])

    def test_nonblocking_source_preservation_is_terminal_and_packable(self) -> None:
        segment = Segment("synthetic-optional", "SOURCE_OPTIONAL")
        result = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            text=segment.source,
            status="source_preserved",
            attempts=[
                AttemptRecord(
                    stage="translate",
                    provider="synthetic-provider",
                    status="error",
                    reason="quota_exhausted",
                    issue_code="quota_exhausted",
                )
            ],
        )
        inventory = {
            "status": "static_verified",
            "static_complete": True,
            "primary_segments": 1,
            "primary_fingerprint": "synthetic-fingerprint",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "patch.json"
            summary, exit_code = _finalize_translation_results(
                work_dir=root,
                target_segments=[segment],
                inventory_segments=[segment],
                results=[result],
                destination=destination,
                inventory_state=inventory,
                review_required=True,
                language_binding_state=DEFAULT_LANGUAGE_BINDING,
            )
            queue = json.loads(
                (root / ".game-l10n" / "failure-queue.json").read_text(
                    encoding="utf-8"
                )
            )
            completion = json.loads(
                (root / ".game-l10n" / "completion-attestation.json").read_text(
                    encoding="utf-8"
                )
            )
            destination_exists = destination.exists()

        self.assertEqual(exit_code, 0)
        self.assertTrue(destination_exists)
        self.assertTrue(summary["artifact_emitted"])
        self.assertEqual(queue["pending_count"], 0)
        self.assertEqual(queue["terminal_count"], 1)
        self.assertTrue(completion["static_completion"])
        self.assertFalse(completion["semantic_completion"])

    def test_runtime_defaults_to_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = runtime_coverage_state(Path(directory), "synthetic-fingerprint")
        self.assertEqual(state["status"], "not_run")
        self.assertFalse(state["runtime_complete"])

    def test_runtime_completion_requires_observing_every_inventory_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.json"
            independent = root / "independent.json"
            report = root / "runtime.json"
            rows = [
                {"id": "synthetic-1", "source": "SOURCE_ONE"},
                {"id": "synthetic-2", "source": "SOURCE_TWO"},
            ]
            write_inventory(primary, "synthetic-ast", rows)
            write_inventory(independent, "synthetic-secondary", rows)
            inventory = save_inventory_attestation(root, primary, independent)
            report.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.runtime-coverage.v1",
                        "inventory_fingerprint": inventory["primary_fingerprint"],
                        "observation_method": "synthetic-observer",
                        "observed_target_ids": ["synthetic-1"],
                        "unmatched_visible_count": 0,
                        "untranslated_visible_count": 0,
                        "unvisited_state_count": 0,
                        "known_unobservable_categories": [],
                    }
                ),
                encoding="utf-8",
            )

            state = record_runtime_coverage(root, report)

        self.assertFalse(state["runtime_complete"])
        self.assertEqual(state["unobserved_target_count"], 1)


if __name__ == "__main__":
    unittest.main()
