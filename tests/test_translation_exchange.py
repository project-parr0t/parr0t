from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import time
from pathlib import Path

from game_l10n.models import AttemptRecord, Segment, TranslationResult
from game_l10n.translation_exchange import (
    FAILURE_NAME,
    FALLBACK_USAGE_RELATIVE,
    REQUESTS_NAME,
    SUCCESS_NAME,
    TranslationExchangeError,
    prepare_translation_exchange,
    process_translation_exchange,
    seal_translation_exchange,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TranslationExchangeTests(unittest.TestCase):
    def test_explicit_unlimited_policy_preserves_uncertain_history_and_cached_success(self):
        from game_l10n.translation_exchange import _fallback_usage_state
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(root, [Segment("fixture", "S")], bundle,
                exchange_id="unlimited-fixture", mode="fallback",
                source_queue="translation_fallback_pending")
            ledger, state = _fallback_usage_state(root, max_input_tokens=100,
                baseline_input_tokens=10, max_elapsed_minutes=60,
                started_at_epoch=time.time(), resolve_uncertain_input_tokens=None)
            state["active_event"] = {"event_id": "old-unknown", "known_input_tokens": 5}
            ledger.write_text(json.dumps(state), encoding="utf-8")
            original = ledger.read_bytes()
            calls = []
            def translate(segments):
                calls.extend(s.id for s in segments)
                return [TranslationResult(s.id, s.source, "T", "translated",
                    attempts=[AttemptRecord("translate", "fallback", "success",
                              usage={"input_tokens": 2, "output_tokens": 1})]) for s in segments]
            options = dict(route="fallback", input_tokens_unlimited=True,
                           max_elapsed_minutes=60, started_at_epoch=time.time())
            result = process_translation_exchange(root, bundle, translate, **options)
            self.assertEqual(result["success_count"], 1)
            self.assertTrue(result["budget"]["input_tokens_unlimited"])
            self.assertEqual(ledger.read_bytes(), original)
            self.assertEqual(process_translation_exchange(root, bundle, translate,
                             **options)["processed"], 0)
            self.assertEqual(calls, ["fixture"])
            pending = root / "pending"
            prepare_translation_exchange(root, [Segment("other", "S")], pending,
                exchange_id="finite-fixture", mode="fallback",
                source_queue="translation_fallback_pending")
            with self.assertRaisesRegex(TranslationExchangeError, "ACCOUNTING_UNCERTAIN"):
                process_translation_exchange(root, pending, translate, route="fallback",
                    max_input_tokens=100, max_elapsed_minutes=60)

    def test_unlimited_tokens_still_enforces_original_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root, calls = Path(directory), []
            bundle = root / "exchange"
            prepare_translation_exchange(root, [Segment("fixture", "S")], bundle,
                exchange_id="expired-fixture", mode="fallback",
                source_queue="translation_fallback_pending")
            result = process_translation_exchange(root, bundle, lambda rows: calls.extend(rows),
                route="fallback", input_tokens_unlimited=True,
                max_elapsed_minutes=1, started_at_epoch=time.time()-120)
            self.assertEqual(calls, [])
            self.assertEqual(result["processed"], 0)
            self.assertEqual(result["issue_code"], "E_FALLBACK_ELAPSED_BUDGET_EXHAUSTED")
            with self.assertRaisesRegex(TranslationExchangeError, "UNLIMITED_POLICY_INVALID"):
                process_translation_exchange(root, bundle, lambda rows: [], route="fallback",
                    input_tokens_unlimited=True, max_input_tokens=100,
                    max_elapsed_minutes=1, started_at_epoch=time.time())

    def test_fallback_processor_checkpoints_usage_and_stops_at_whole_job_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bundle = workspace / "exchange"
            prepare_translation_exchange(
                workspace,
                [
                    Segment(id="line-1", source="One"),
                    Segment(id="line-2", source="Two"),
                ],
                bundle,
                exchange_id="budgeted-fallback",
                mode="fallback",
                source_queue="translation_fallback_pending",
            )

            def translate(segments: list[Segment]) -> list[TranslationResult]:
                return [
                    TranslationResult(
                        segment_id=segment.id,
                        source=segment.source,
                        text="SYNTHETIC_TARGET_TEXT",
                        status="translated",
                        attempts=[
                            AttemptRecord(
                                stage="translate",
                                provider="fallback",
                                status="success",
                                usage={"input_tokens": 20, "output_tokens": 3},
                            )
                        ],
                    )
                    for segment in segments
                ]

            result = process_translation_exchange(
                workspace,
                bundle,
                translate,
                route="fallback-provider",
                max_input_tokens=100,
                baseline_input_tokens=90,
                wave_size=1,
            )
            usage = json.loads(
                (workspace / FALLBACK_USAGE_RELATIVE).read_text(encoding="utf-8")
            )

        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["issue_code"], "E_FALLBACK_INPUT_BUDGET_EXHAUSTED")
        self.assertEqual(result["budget"]["input_tokens"], 110)
        self.assertEqual(usage["provider_input_tokens"], 20)
        self.assertIsNone(usage["active_event"])

    def test_configured_processor_resumes_fixed_fallback_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bundle = workspace / "exchange"
            prepare_translation_exchange(
                workspace,
                [
                    Segment(id="line-1", source="Hello"),
                    Segment(id="line-2", source="Wait"),
                ],
                bundle,
                exchange_id="fallback-processor",
                mode="fallback",
                source_queue="translation_fallback_pending",
            )

            def translate(segments: list[Segment]) -> list[TranslationResult]:
                self.assertEqual([item.id for item in segments], ["line-1", "line-2"])
                return [
                    TranslationResult(
                        segment_id="line-1",
                        source="Hello",
                        text="SYNTHETIC_TARGET_TEXT",
                        status="translated",
                    ),
                    TranslationResult(
                        segment_id="line-2",
                        source="Wait",
                        attempts=[
                            AttemptRecord(
                                stage="translate",
                                provider="fallback",
                                status="error",
                                issue_code="timeout",
                            )
                        ],
                    ),
                ]

            result = process_translation_exchange(
                workspace, bundle, translate, route="fallback-provider"
            )
            second = process_translation_exchange(
                workspace, bundle, translate, route="fallback-provider"
            )

            success_rows = [
                json.loads(line)
                for line in (bundle / SUCCESS_NAME).read_text(encoding="utf-8").splitlines()
            ]
            failure_rows = [
                json.loads(line)
                for line in (bundle / FAILURE_NAME).read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["success_count"], 1)
        self.assertEqual(result["failure_count"], 1)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(success_rows[0]["target_id"], "line-1")
        self.assertEqual(failure_rows[0]["failure_code"], "timeout")
        self.assertTrue(failure_rows[0]["retryable"])

    def test_partial_exchange_routes_only_metadata_to_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bundle = workspace / "private" / "exchange"
            segments = [
                Segment(id="line-1", source="Hello", speaker="A", scene="intro"),
                Segment(id="line-2", source="Goodbye", speaker="B", scene="intro"),
                Segment(id="line-3", source="Wait", speaker="A", scene="intro"),
            ]
            prepared = prepare_translation_exchange(
                workspace,
                segments,
                bundle,
                exchange_id="manual-1",
                mode="manual",
                source_queue="translation_fallback_pending",
            )
            self.assertEqual(prepared["target_count"], 3)
            requests = [json.loads(line) for line in (bundle / REQUESTS_NAME).read_text(encoding="utf-8").splitlines()]
            success = {
                "target_id": "line-1",
                "status": "success",
                "text": "SYNTHETIC_TARGET_TEXT",
                "route": "human",
                "attempt": 1,
                "source_sha256": requests[0]["source_sha256"],
                "context_sha256": requests[0]["context_sha256"],
            }
            failure = {
                "target_id": "line-2",
                "status": "failed",
                "failure_code": "timeout",
                "route": "fallback-api",
                "attempt": 1,
                "source_sha256": requests[1]["source_sha256"],
                "context_sha256": requests[1]["context_sha256"],
                "retryable": True,
            }
            (bundle / SUCCESS_NAME).write_text(json.dumps(success, ensure_ascii=False) + "\n", encoding="utf-8")
            (bundle / FAILURE_NAME).write_text(json.dumps(failure) + "\n", encoding="utf-8")
            result = seal_translation_exchange(workspace, bundle)

            self.assertEqual(result["status"], "fallback_required")
            self.assertEqual(result["success_count"], 1)
            self.assertEqual(result["failure_count"], 1)
            self.assertEqual(result["pending_count"], 1)
            envelope = json.loads((bundle / "control-envelope.json").read_text(encoding="utf-8"))
            self.assertNotIn("source", json.dumps(envelope))
            self.assertNotIn("SYNTHETIC_TARGET_TEXT", json.dumps(envelope, ensure_ascii=False))
            self.assertEqual(envelope["counts"]["fallback_required"], 2)
            self.assertEqual(envelope["queue_transitions"][0]["to"], "review_pending")

    def test_exchange_rejects_modified_requests_and_ad_hoc_failure_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bundle = workspace / "exchange"
            prepare_translation_exchange(
                workspace,
                [Segment(id="line-1", source="Hello")],
                bundle,
                exchange_id="fallback-1",
                mode="fallback",
                source_queue="translation_fallback_pending",
            )
            request = (bundle / REQUESTS_NAME).read_text(encoding="utf-8")
            (bundle / REQUESTS_NAME).write_text(request.replace("Hello", "Changed"), encoding="utf-8")
            with self.assertRaises(TranslationExchangeError):
                seal_translation_exchange(workspace, bundle)

            (bundle / REQUESTS_NAME).write_text(request, encoding="utf-8")
            row = json.loads(request)
            ad_hoc = {
                "target_id": "line-1",
                "status": "failed",
                "failure_code": "provider_error",
                "route": "fallback-api",
                "attempt": 1,
                "source_sha256": row["source_sha256"],
                "context_sha256": row["context_sha256"],
                "retryable": True,
                "reason": "free-form refusal prose must not cross the boundary",
            }
            (bundle / FAILURE_NAME).write_text(json.dumps(ad_hoc) + "\n", encoding="utf-8")
            with self.assertRaises(TranslationExchangeError):
                seal_translation_exchange(workspace, bundle)


if __name__ == "__main__":
    unittest.main()
