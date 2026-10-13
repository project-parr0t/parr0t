from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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


def write_inventory(
    path: Path,
    method: str,
    segments: list[dict],
    *,
    declared_complete: bool = True,
    exclusions: list[dict] | None = None,
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
                },
                "segments": segments,
            }
        ),
        encoding="utf-8",
    )


class CoverageTests(unittest.TestCase):
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
        self.assertTrue(loaded["entries"][0]["retryable_on_user_resume"])

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
            )

            self.assertFalse(destination.exists())
            self.assertTrue(Path(summary["held_checkpoint"]).exists())
        self.assertEqual(exit_code, 3)
        self.assertEqual(summary["status"], "held")
        self.assertFalse(summary["artifact_emitted"])

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
