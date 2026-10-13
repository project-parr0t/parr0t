from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.corrections import (
    CORRECTION_REQUEST_SCHEMA,
    CorrectionError,
    apply_user_corrections,
)
from game_l10n.formats import write_results
from game_l10n.models import AttemptRecord, Segment, TranslationResult
from game_l10n.release_checkpoint import CHECKPOINT_SCHEMA
from game_l10n.store import stable_hash


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result(target_id: str, source: str, target: str) -> TranslationResult:
    return TranslationResult(
        segment_id=target_id,
        source=source,
        text=target,
        status="reviewed",
        translator="fixture-translator",
        reviewer="fixture-reviewer",
        attempts=[
            AttemptRecord(
                stage="review",
                provider="fixture-reviewer",
                status="approved",
                candidate_generation=1,
            )
        ],
        candidate_generation=1,
        candidate_digest=stable_hash(target),
        review_outcome="approved",
        acceptance_authority="reviewer",
    )


def _checkpoint(
    segments: list[Segment],
    results: list[TranslationResult],
    inventory_fingerprint: str,
    *,
    dependencies: dict[str, list[str]] | None = None,
) -> dict:
    dependencies = dependencies or {}
    result_by_id = {item.segment_id: item for item in results}
    return {
        "schema": CHECKPOINT_SCHEMA,
        "patch_id": "fixture-localization",
        "release_id": "r1",
        "sdk": {
            "branch": "sdk-fixture-1.0",
            "commit": "a" * 40,
            "adapter_fingerprint": _digest("fixture-adapter"),
        },
        "game_builds": [
            {"id": "fixture-build", "binding_fingerprint": _digest("fixture-binding")}
        ],
        "source_inventory": {
            "method": "fixture-primary",
            "fingerprint": inventory_fingerprint,
            "target_count": len(segments),
        },
        "context": {
            "fingerprint": _digest("fixture-context"),
            "review_policy_fingerprint": _digest("fixture-review-policy"),
        },
        "entries": [
            {
                "id": segment.id,
                "source_sha256": _digest(segment.source),
                "target": result_by_id[segment.id].text,
                "target_sha256": _digest(result_by_id[segment.id].text),
                "location_fingerprint": _digest(f"location:{segment.id}"),
                "dependency_fingerprint": _digest(f"dependency:{segment.id}"),
                "review_fingerprint": _digest(f"review:{segment.id}"),
                "status": "reviewed",
                "dependencies": dependencies.get(segment.id, []),
            }
            for segment in segments
        ],
        "locale_fallback": {
            "status": "unsupported",
            "mode": "none",
            "fallback_locale": "",
            "placeholder": "",
            "verified": False,
            "reason_code": "fixture_no_safe_fallback",
        },
    }


class CorrectionTests(unittest.TestCase):
    def test_exact_user_correction_changes_only_named_target_without_model_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [
                Segment("fixture-a", "SOURCE_A"),
                Segment("fixture-b", "SOURCE_B"),
            ]
            results = [
                _result("fixture-a", "SOURCE_A", "TARGET_A_OLD"),
                _result("fixture-b", "SOURCE_B", "TARGET_B"),
            ]
            base = root / "base.jsonl"
            checkpoint = root / "checkpoint.json"
            request = root / "request.json"
            output = root / "corrected.jsonl"
            inventory_fingerprint = _digest("fixture-inventory")
            write_results(base, results)
            checkpoint.write_text(
                json.dumps(_checkpoint(segments, results, inventory_fingerprint)),
                encoding="utf-8",
            )
            request.write_text(
                json.dumps(
                    {
                        "schema": CORRECTION_REQUEST_SCHEMA,
                        "changes": [{"id": "fixture-a", "target": "TARGET_A_NEW"}],
                    }
                ),
                encoding="utf-8",
            )

            summary = apply_user_corrections(
                segments=segments,
                base_results_path=base,
                checkpoint_path=checkpoint,
                request_path=request,
                output_path=output,
                expected_inventory_fingerprint=inventory_fingerprint,
            )
            old_rows = [json.loads(line) for line in base.read_text().splitlines()]
            new_rows = [json.loads(line) for line in output.read_text().splitlines()]

        self.assertEqual(summary["changed_count"], 1)
        self.assertEqual(summary["unchanged_count"], 1)
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(summary["context_analysis_runs"], 0)
        self.assertEqual(new_rows[0]["text"], "TARGET_A_NEW")
        self.assertEqual(new_rows[0]["acceptance_authority"], "user")
        self.assertEqual(new_rows[0]["attempts"][-1]["stage"], "user-correction")
        self.assertEqual(new_rows[1], old_rows[1])

    def test_checkpoint_can_materialize_the_base_without_translation_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [
                Segment("fixture-a", "SOURCE_A"),
                Segment("fixture-b", "SOURCE_B"),
            ]
            results = [
                _result("fixture-a", "SOURCE_A", "TARGET_A_OLD"),
                _result("fixture-b", "SOURCE_B", "TARGET_B"),
            ]
            checkpoint = root / "checkpoint.json"
            request = root / "request.json"
            output = root / "corrected.jsonl"
            inventory_fingerprint = _digest("fixture-inventory")
            checkpoint.write_text(
                json.dumps(_checkpoint(segments, results, inventory_fingerprint)),
                encoding="utf-8",
            )
            request.write_text(
                json.dumps(
                    {
                        "schema": CORRECTION_REQUEST_SCHEMA,
                        "changes": [{"id": "fixture-a", "target": "TARGET_A_NEW"}],
                    }
                ),
                encoding="utf-8",
            )

            summary = apply_user_corrections(
                segments=segments,
                base_results_path=None,
                checkpoint_path=checkpoint,
                request_path=request,
                output_path=output,
                expected_inventory_fingerprint=inventory_fingerprint,
            )
            rows = [json.loads(line) for line in output.read_text().splitlines()]

        self.assertEqual(summary["base_source"], "release_checkpoint")
        self.assertEqual(summary["model_calls"], 0)
        self.assertEqual(rows[0]["text"], "TARGET_A_NEW")
        self.assertEqual(rows[0]["acceptance_authority"], "user")
        self.assertEqual(rows[1]["text"], "TARGET_B")
        self.assertEqual(rows[1]["metadata"]["candidate_origin"], "release_checkpoint")

    def test_correction_output_rejects_lossy_csv_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("fixture-a", "SOURCE_A")]
            results = [_result("fixture-a", "SOURCE_A", "TARGET_A_OLD")]
            checkpoint = root / "checkpoint.json"
            request = root / "request.json"
            inventory_fingerprint = _digest("fixture-inventory")
            checkpoint.write_text(
                json.dumps(_checkpoint(segments, results, inventory_fingerprint)),
                encoding="utf-8",
            )
            request.write_text(
                json.dumps(
                    {
                        "schema": CORRECTION_REQUEST_SCHEMA,
                        "changes": [{"id": "fixture-a", "target": "TARGET_A_NEW"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CorrectionError, "audit metadata"):
                apply_user_corrections(
                    segments=segments,
                    base_results_path=None,
                    checkpoint_path=checkpoint,
                    request_path=request,
                    output_path=root / "corrected.csv",
                    expected_inventory_fingerprint=inventory_fingerprint,
                )

    def test_reverse_dependency_requires_targeted_review_instead_of_scope_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("fixture-a", "SOURCE_A"), Segment("fixture-b", "SOURCE_B")]
            results = [
                _result("fixture-a", "SOURCE_A", "TARGET_A_OLD"),
                _result("fixture-b", "SOURCE_B", "TARGET_B"),
            ]
            base = root / "base.jsonl"
            checkpoint = root / "checkpoint.json"
            request = root / "request.json"
            output = root / "corrected.jsonl"
            inventory_fingerprint = _digest("fixture-inventory")
            write_results(base, results)
            checkpoint.write_text(
                json.dumps(
                    _checkpoint(
                        segments,
                        results,
                        inventory_fingerprint,
                        dependencies={"fixture-b": ["fixture-a"]},
                    )
                ),
                encoding="utf-8",
            )
            request.write_text(
                json.dumps(
                    {
                        "schema": CORRECTION_REQUEST_SCHEMA,
                        "changes": [{"id": "fixture-a", "target": "TARGET_A_NEW"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CorrectionError, "fixture-b"):
                apply_user_corrections(
                    segments=segments,
                    base_results_path=base,
                    checkpoint_path=checkpoint,
                    request_path=request,
                    output_path=output,
                    expected_inventory_fingerprint=inventory_fingerprint,
                )

        self.assertFalse(output.exists())

    def test_exact_correction_still_preserves_protected_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("fixture-a", "SOURCE {value}")]
            results = [_result("fixture-a", "SOURCE {value}", "TARGET {value}")]
            base = root / "base.jsonl"
            checkpoint = root / "checkpoint.json"
            request = root / "request.json"
            output = root / "corrected.jsonl"
            inventory_fingerprint = _digest("fixture-inventory")
            write_results(base, results)
            checkpoint.write_text(
                json.dumps(_checkpoint(segments, results, inventory_fingerprint)),
                encoding="utf-8",
            )
            request.write_text(
                json.dumps(
                    {
                        "schema": CORRECTION_REQUEST_SCHEMA,
                        "changes": [{"id": "fixture-a", "target": "TARGET"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CorrectionError, "protected-token-mismatch"):
                apply_user_corrections(
                    segments=segments,
                    base_results_path=base,
                    checkpoint_path=checkpoint,
                    request_path=request,
                    output_path=output,
                    expected_inventory_fingerprint=inventory_fingerprint,
                )


if __name__ == "__main__":
    unittest.main()
