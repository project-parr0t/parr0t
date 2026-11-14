from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

from game_l10n.models import AttemptRecord, Segment, TranslationResult
from game_l10n.store import stable_hash
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
    def test_network_error_without_reported_usage_is_not_assumed_free(self) -> None:
        for code in ("authentication_error", "quota_exhausted", "rate_limited"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bundle = root / "exchange"
                prepare_translation_exchange(
                    root, [Segment("line", "Source")], bundle,
                    exchange_id="unknown-error-usage", mode="fallback",
                    source_queue="translation_fallback_pending",
                )
                result = process_translation_exchange(
                    root, bundle,
                    lambda segments: [TranslationResult(
                        "line", "Source",
                        attempts=[AttemptRecord("translate", "fallback", "error", issue_code=code)],
                    )],
                    route="fallback", max_input_tokens=100, baseline_input_tokens=0,
                )
                self.assertEqual(result["issue_code"], "E_FALLBACK_BUDGET_ACCOUNTING_UNCERTAIN")
                usage = json.loads((root / FALLBACK_USAGE_RELATIVE).read_text(encoding="utf-8"))
                self.assertIsNotNone(usage["active_event"])

    def test_usage_is_durable_before_a_progress_consumer_interrupts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("line", "Source")], bundle,
                exchange_id="progress-interruption", mode="fallback",
                source_queue="translation_fallback_pending",
            )

            def interrupt_progress(statistics: dict[str, object]) -> None:
                if statistics["completed_records"]:
                    raise RuntimeError("Synthetic consumer interruption")

            with self.assertRaisesRegex(RuntimeError, "Synthetic consumer"):
                process_translation_exchange(
                    root, bundle,
                    lambda segments: [TranslationResult(
                        "line", "Source", "Target", "translated",
                        attempts=[AttemptRecord("translate", "fallback", "success", usage={"input_tokens": 7})],
                    )],
                    route="fallback", max_input_tokens=100, baseline_input_tokens=0,
                    progress_callback=interrupt_progress,
                )
            usage = json.loads((root / FALLBACK_USAGE_RELATIVE).read_text(encoding="utf-8"))
            self.assertEqual(usage["provider_input_tokens"], 7)
            self.assertIsNone(usage["active_event"])
            self.assertEqual(seal_translation_exchange(root, bundle)["success_count"], 1)

    def test_unlimited_policy_rejects_nonfinite_deadlines_before_provider_calls(self) -> None:
        for started_at in (float("nan"), float("inf")):
            with self.subTest(started_at=started_at), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bundle = root / "exchange"
                prepare_translation_exchange(
                    root, [Segment("line", "Source")], bundle,
                    exchange_id="invalid-unlimited-deadline", mode="fallback",
                    source_queue="translation_fallback_pending",
                )
                with self.assertRaisesRegex(TranslationExchangeError, "UNLIMITED_POLICY_INVALID"):
                    process_translation_exchange(
                        root, bundle, lambda segments: self.fail("Invalid deadline must not call provider"),
                        route="fallback", input_tokens_unlimited=True,
                        max_elapsed_minutes=1, started_at_epoch=started_at,
                    )

    def test_unicode_line_separators_round_trip_through_requests_and_successes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            source = "Synthetic\u0085source\u2028with\u2029separators"
            target = "Synthetic\u2029target\u2028with\u0085separators"
            prepare_translation_exchange(
                root, [Segment("line", source, previous=source)], bundle,
                exchange_id="unicode-separators", mode="fallback",
                source_queue="translation_fallback_pending",
            )

            def translate(segments: list[Segment]) -> list[TranslationResult]:
                self.assertEqual(segments[0].source, source)
                self.assertEqual(segments[0].previous, source)
                return [TranslationResult("line", source, target, "translated")]

            result = process_translation_exchange(root, bundle, translate, route="fallback")
            self.assertEqual(result["success_count"], 1)
            self.assertEqual(
                json.loads((bundle / SUCCESS_NAME).read_text(encoding="utf-8"))["text"],
                target,
            )
            self.assertEqual(seal_translation_exchange(root, bundle)["status"], "complete")

    def test_fallback_rejects_result_from_a_stale_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("line", "Current source")], bundle,
                exchange_id="stale-result", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            with self.assertRaisesRegex(TranslationExchangeError, "immutable exchange sources"):
                process_translation_exchange(
                    root, bundle,
                    lambda segments: [TranslationResult("line", "Old source", "Target", "translated")],
                    route="fallback",
                )
            result = seal_translation_exchange(root, bundle)
            self.assertEqual(result["success_count"], 0)
            self.assertEqual(result["pending_count"], 1)

    def test_budget_checkpoint_keeps_unattempted_failures_and_retry_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("first", "One"), Segment("second", "Two")], bundle,
                exchange_id="failed-retry-checkpoint", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            process_translation_exchange(
                root, bundle,
                lambda segments: [
                    TranslationResult(
                        item.id, item.source,
                        attempts=[AttemptRecord("translate", "fallback", "error", issue_code="timeout")],
                    )
                    for item in segments
                ],
                route="fallback",
            )
            original_failure = json.loads(
                (bundle / "failure.jsonl").read_text(encoding="utf-8").splitlines()[1]
            )
            calls: list[str] = []

            def translate(segments: list[Segment]) -> list[TranslationResult]:
                calls.extend(item.id for item in segments)
                return [
                    TranslationResult(
                        item.id, item.source, "Target", "translated",
                        attempts=[AttemptRecord("translate", "fallback", "success", usage={"input_tokens": 10})],
                    )
                    for item in segments
                ]

            result = process_translation_exchange(
                root, bundle, translate, route="fallback", retry_failures=True,
                max_input_tokens=10, baseline_input_tokens=0, wave_size=1,
            )
            self.assertEqual(calls, ["first"])
            self.assertEqual(result["success_count"], 1)
            self.assertEqual(result["failure_count"], 1)
            self.assertEqual(result["pending_count"], 0)
            self.assertEqual(
                json.loads((bundle / FAILURE_NAME).read_text(encoding="utf-8")),
                original_failure,
            )
            resumed = process_translation_exchange(
                root, bundle, translate, route="fallback", max_input_tokens=10,
            )
            self.assertEqual(resumed["processed"], 0)
            self.assertEqual(calls, ["first"])

    def test_interrupted_retry_checkpoint_does_not_leave_overlapping_results(self) -> None:
        import game_l10n.translation_exchange as exchange

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("line", "Source")], bundle,
                exchange_id="interrupted-retry", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            process_translation_exchange(
                root, bundle, lambda segments: [TranslationResult("line", "Source")],
                route="fallback",
            )
            atomic_bytes = exchange._atomic_bytes

            def interrupt_success(path: Path, data: bytes) -> None:
                if path.name == SUCCESS_NAME:
                    raise OSError("Synthetic interrupted success commit")
                atomic_bytes(path, data)

            with patch.object(exchange, "_atomic_bytes", side_effect=interrupt_success):
                with self.assertRaisesRegex(OSError, "Synthetic interrupted"):
                    process_translation_exchange(
                        root, bundle,
                        lambda segments: [TranslationResult("line", "Source", "Target", "translated")],
                        route="fallback", retry_failures=True,
                    )
            result = seal_translation_exchange(root, bundle)
            self.assertEqual(result["success_count"], 0)
            self.assertEqual(result["failure_count"], 0)
            self.assertEqual(result["pending_count"], 1)

    def test_cached_results_do_not_charge_historical_usage_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("line", "Source")], bundle,
                exchange_id="cached-usage", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            result = process_translation_exchange(
                root, bundle,
                lambda segments: [
                    TranslationResult(
                        "line", "Source", "Target", "translated", from_cache=True,
                        attempts=[AttemptRecord("translate", "fallback", "success", usage={"input_tokens": 40})],
                    )
                ],
                route="fallback", max_input_tokens=100, baseline_input_tokens=40,
            )
            self.assertEqual(result["budget"]["input_tokens"], 40)
            usage = json.loads((root / FALLBACK_USAGE_RELATIVE).read_text(encoding="utf-8"))
            self.assertEqual(usage["provider_input_tokens"], 0)
            self.assertIsNone(usage["active_event"])

    def test_completed_bundle_can_reconcile_its_uncertain_usage_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("line", "Source")], bundle,
                exchange_id="completed-reconciliation", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            first = process_translation_exchange(
                root, bundle,
                lambda segments: [TranslationResult("line", "Source", "Target", "translated")],
                route="fallback", max_input_tokens=100, baseline_input_tokens=10,
            )
            self.assertEqual(first["status"], "complete")
            self.assertEqual(first["issue_code"], "E_FALLBACK_BUDGET_ACCOUNTING_UNCERTAIN")
            with self.assertRaisesRegex(TranslationExchangeError, "ACCOUNTING_UNCERTAIN"):
                process_translation_exchange(
                    root, bundle, lambda segments: self.fail("Cached success must not be called"),
                    route="fallback", max_input_tokens=100,
                )
            result = process_translation_exchange(
                root, bundle, lambda segments: self.fail("Cached success must not be called"),
                route="fallback", max_input_tokens=100, resolve_uncertain_input_tokens=7,
            )
            self.assertEqual(result["processed"], 0)
            self.assertEqual(result["budget"]["input_tokens"], 17)
            usage = json.loads((root / FALLBACK_USAGE_RELATIVE).read_text(encoding="utf-8"))
            self.assertIsNone(usage["active_event"])
            self.assertEqual(usage["events"][0]["accounting"], "user_reconciled")

    def test_omitting_an_existing_user_limit_does_not_start_unlimited_calls(self) -> None:
        from game_l10n.translation_exchange import _fallback_usage_state

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("line", "Source")], bundle,
                exchange_id="bound-user-limit", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            ledger, _ = _fallback_usage_state(
                root, max_input_tokens=100, baseline_input_tokens=100,
                max_elapsed_minutes=None, started_at_epoch=None,
                resolve_uncertain_input_tokens=None,
            )
            original = ledger.read_bytes()
            with self.assertRaisesRegex(TranslationExchangeError, "E_FALLBACK_BUDGET_REQUIRED"):
                process_translation_exchange(
                    root, bundle, lambda segments: self.fail("User limit must remain bound"),
                    route="fallback",
                )
            self.assertEqual(ledger.read_bytes(), original)

    def test_usage_limits_reject_invalid_types_and_nonfinite_deadlines(self) -> None:
        from game_l10n.translation_exchange import _fallback_usage_state

        invalid_options = [
            {"max_input_tokens": True},
            {"max_input_tokens": 10.5},
            {"baseline_input_tokens": True},
            {"baseline_input_tokens": 1.5},
            {"max_elapsed_minutes": True},
            {"max_elapsed_minutes": 1.5},
            {"max_elapsed_minutes": 1},
            {"started_at_epoch": float("nan")},
            {"started_at_epoch": float("inf")},
            {"started_at_epoch": 0},
        ]
        for invalid in invalid_options:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                options = dict(
                    max_input_tokens=100, baseline_input_tokens=0,
                    max_elapsed_minutes=None, started_at_epoch=None,
                    resolve_uncertain_input_tokens=None,
                )
                options.update(invalid)
                with self.assertRaises(TranslationExchangeError):
                    _fallback_usage_state(root, **options)
                self.assertFalse((root / FALLBACK_USAGE_RELATIVE).exists())

    def test_usage_ledger_retains_and_validates_the_original_deadline(self) -> None:
        from game_l10n.translation_exchange import _fallback_usage_state

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = dict(
                max_input_tokens=100, baseline_input_tokens=0,
                max_elapsed_minutes=1, started_at_epoch=100.0,
                resolve_uncertain_input_tokens=None,
            )
            ledger, state = _fallback_usage_state(root, **options)
            original = ledger.read_bytes()
            with self.assertRaisesRegex(TranslationExchangeError, "ELAPSED_BUDGET_MISMATCH"):
                _fallback_usage_state(root, **{**options, "started_at_epoch": 200.0})
            self.assertEqual(ledger.read_bytes(), original)
            for invalid_start in (float("nan"), float("inf")):
                state["started_at_epoch"] = invalid_start
                ledger.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaisesRegex(TranslationExchangeError, "USAGE_STATE_INVALID"):
                    _fallback_usage_state(root, **{**options, "started_at_epoch": None})

    def test_bound_optional_ui_slot_accepts_empty_fallback_success(self) -> None:
        source = "SOURCE_OPTIONAL"
        metadata = {
            "ui_slot_role": "template_fragment",
            "ui_composed_with_ids": ["fixture-label"],
            "ui_render_pattern": "{prefix}{value}",
            "ui_fixed_target_prefix": "TARGET_",
            "ui_fixed_target_suffix": "",
        }
        source_digest = _digest(source)
        composition = {
            "target_id": "optional-slot",
            "source_sha256": source_digest,
            **metadata,
        }
        metadata["ui_empty_target"] = {
            "schema": "parr0t.ui-empty-target/v1",
            "reason_code": "optional_grammatical_slot",
            "source_sha256": source_digest,
            "composition_sha256": stable_hash(composition),
            "evidence_sha256": _digest("SYNTHETIC_BINDING"),
        }
        segment = Segment("optional-slot", source, metadata=metadata)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root,
                [segment],
                bundle,
                exchange_id="optional-empty-fixture",
                mode="fallback",
                source_queue="translation_fallback_pending",
            )
            result = process_translation_exchange(
                root,
                bundle,
                lambda segments: [
                    TranslationResult(
                        segment_id=segments[0].id,
                        source=segments[0].source,
                        text="",
                        status="translated",
                    )
                ],
                route="fallback",
            )

        self.assertEqual(result["success_count"], 1)

    def test_default_fallback_has_no_whole_job_limit_and_preserves_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root,
                [Segment("spacing", "  \t")],
                bundle,
                exchange_id="whitespace-fixture",
                mode="fallback",
                source_queue="translation_fallback_pending",
            )

            result = process_translation_exchange(
                root,
                bundle,
                lambda segments: [
                    TranslationResult(
                        segment_id=segments[0].id,
                        source=segments[0].source,
                        text=segments[0].source,
                        status="translated",
                    )
                ],
                route="deepseek",
            )

        self.assertEqual(result["success_count"], 1)
        self.assertIsNone(result["budget"])

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
            progress_events: list[dict[str, object]] = []
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
                workspace,
                bundle,
                translate,
                route="fallback-provider",
                progress_callback=progress_events.append,
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
        self.assertEqual(progress_events[-1]["completed_records"], 2)
        self.assertEqual(progress_events[-1]["translation_completed"], 1)
        self.assertEqual(progress_events[-1]["failed_records"], 1)
        self.assertNotIn("target_id", json.dumps(progress_events))

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


    def test_exchange_directory_must_be_control_plane_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(TranslationExchangeError, "directory name"):
                prepare_translation_exchange(
                    root, [Segment("fixture", "S")], root / "exchange 1",
                    exchange_id="unsafe-fixture", mode="fallback",
                    source_queue="translation_fallback_pending",
                )
            self.assertFalse((root / "exchange 1").exists())

if __name__ == "__main__":
    unittest.main()
