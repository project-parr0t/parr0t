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
    def test_completed_external_scope_issues_collection_not_job_completion(self):
        from game_l10n.control_plane import establish_control_plane_entry
        from game_l10n.formats import read_segments
        from game_l10n.native_followup import collect_translation_exchange_candidates
        with tempfile.TemporaryDirectory() as directory:
            root, bundle = Path(directory), Path(directory) / "exchange"
            establish_control_plane_entry(root)
            prepare_translation_exchange(
                root, [Segment("a", "SOURCE_A"), Segment("b", "SOURCE_B")], bundle,
                exchange_id="standalone", mode="fallback", source_queue="translation_fallback_pending",
            )
            processed = process_translation_exchange(
                root, bundle, lambda rows: [
                    TranslationResult(row.id, row.source, "TARGET_" + row.id.upper(), "translated",
                        translator="configured-api", attempts=[AttemptRecord(
                            "translate", "configured-api", "success", ordinal=1, model="fixture/api",
                        )]) for row in rows
                ], route="configured-api", route_models={"configured-api": "fixture/api"},
            )
            self.assertEqual(processed["next_action"], "collect_translation_exchange_candidates")
            request = processed["collection"]
            self.assertEqual(request["command"], "collect-translation-exchange-candidates")
            self.assertIn("--bundle", request["argv"])
            before = (bundle / SUCCESS_NAME).read_bytes()
            collected = collect_translation_exchange_candidates(
                root, read_segments(root / request["input"]), bundle,
                root / "review.jsonl", root / "unresolved.jsonl",
            )
            self.assertEqual((collected["candidate_count"], collected["unresolved_count"]), (2, 0))
            self.assertEqual((bundle / SUCCESS_NAME).read_bytes(), before)

    def test_partial_multi_adapter_block_keeps_known_operational_attempt_before_next_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root, bundle = Path(directory), Path(directory) / "exchange"
            graph = [
                {"provider": route, "model": "fixture/" + route, "max_retries": 0, "transport": transport}
                for route, transport in [("a", "external"), ("b", "external"), ("native-c", "native")]
            ]
            prepare_translation_exchange(root, [Segment("one", "S")], bundle,
                exchange_id="partial-block", mode="fallback", source_queue="translation_fallback_pending")
            first = AttemptRecord("translate", "a", "error", ordinal=1, model="fixture/a",
                issue_code="quota_exhausted", operational=True, provider_fingerprint="a" * 64)
            initial = process_translation_exchange(
                root, bundle, lambda rows: [TranslationResult("one", "S", "", "failed", attempts=[first])],
                route="a", route_models={"a": "fixture/a", "b": "fixture/b"}, full_route_plan=graph,
            )
            self.assertIsNone(initial["native_continuation"])
            self.assertEqual(initial["issue_code"], "E_FALLBACK_ADAPTER_ATTEMPT_POLICY_INCOMPLETE")
            def resume(rows, prior):
                self.assertEqual([item.provider for item in prior[0].attempts], ["a"])
                return [TranslationResult("one", "S", "", "failed", attempts=[
                    *prior[0].attempts, AttemptRecord("translate", "b", "refused", ordinal=1,
                        model="fixture/b", issue_code="explicit_provider_refusal", operational=False),
                ])]
            finished = process_translation_exchange(
                root, bundle, lambda rows: self.fail("Do not restart adapter a"),
                route="a", route_models={"a": "fixture/a", "b": "fixture/b"}, full_route_plan=graph,
                retry_failures=True, resume_translator=resume,
                continuation_status=lambda segment, prior: {"eligible": True},
            )
            self.assertEqual(finished["native_continuation"]["native_route_plan"][0]["provider"], "native-c")
            failure = json.loads((bundle / FAILURE_NAME).read_text())
            self.assertEqual([(item["provider"], item["ordinal"]) for item in failure["attempt_lineage"]],
                             [("a", 1), ("b", 1)])

    def test_capacity_failure_reports_specific_repair_not_user_budget_exhaustion(self):
        with tempfile.TemporaryDirectory() as directory:
            root, bundle = Path(directory), Path(directory) / "exchange"
            prepare_translation_exchange(root, [Segment("one", "SYNTHETIC_PRIVATE_SOURCE")], bundle,
                exchange_id="capacity", mode="fallback", source_queue="translation_fallback_pending")
            result = process_translation_exchange(
                root, bundle, lambda rows: [TranslationResult(
                    "one", rows[0].source, "", "failed",
                    attempts=[AttemptRecord("translate", "adapter", "error", ordinal=1,
                        model="fixture/model", issue_code="input_capacity_exceeded", operational=True)],
                )], route="adapter", route_models={"adapter": "fixture/model"},
            )
            self.assertEqual(result["issue_code"], "E_FALLBACK_INPUT_CAPACITY_EXCEEDED")
            self.assertFalse(result["capacity_repair"]["user_budget_exhausted"])
            self.assertIn("max_input_chars", result["capacity_repair"]["next_action"])
            self.assertNotIn("SYNTHETIC_PRIVATE_SOURCE", json.dumps(result))

    def test_configured_plan_resolves_host_identity_without_overwriting_it_with_bare_role(self):
        from game_l10n.translation_exchange import configured_translation_route_plan
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / ".game-l10n/native/host-models.json"
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text(json.dumps({
                "schema": "parr0t.native-host-models.v1", "host": "omp",
                "session_id": "fixture-session", "process_id": 1,
                "models": [{"provider": "publisher", "id": "literal:identity",
                            "selector": "publisher/literal:identity"}],
            }), encoding="utf-8")
            config = {
                "routing": {"translators": ["native-translation"]}, "providers": [],
                "model_policy": {"source": "user_prompt",
                                 "native_roles": {"translation": "literal:identity@upstream"}},
            }
            runtime = {"host": "omp", "models": {"native-translation": "publisher/literal:identity@upstream"}}
            with patch("game_l10n.native_runtime.native_runtime", return_value=runtime):
                plan = configured_translation_route_plan(root, config)
                self.assertEqual(plan[0]["model"], "publisher/literal:identity@upstream")
                config["model_policy"]["native_roles"]["translation"] = "publisher/changed"
                with self.assertRaisesRegex(TranslationExchangeError, "E_MODEL_POLICY_BINDING_CONFLICT"):
                    configured_translation_route_plan(root, config)

    def test_external_first_handoff_requires_receipt_and_selects_actual_native_successor(self):
        from dataclasses import replace
        from game_l10n.formats import read_segments
        from game_l10n.native_pass import prepare_native_tasks
        from game_l10n.native_runtime import configure_native_runtime
        from game_l10n.translation_exchange import configured_translation_route_plan
        with tempfile.TemporaryDirectory() as directory:
            root, bundle = Path(directory), Path(directory) / "exchange"
            configure_native_runtime(root, "codex", {"native-middle": "fixture/native"})
            config = {
                "routing": {"translators": ["external-a", "native-middle", "external-b"]},
                "providers": [{"name": name, "type": "command", "command": ["not-executed"],
                               "model": "fixture/" + name} for name in ("external-a", "external-b")],
            }
            (root / "game-l10n.json").write_text(json.dumps(config), encoding="utf-8")
            graph = configured_translation_route_plan(root, config)
            prepare_translation_exchange(root, [Segment("remaining", "S")], bundle,
                exchange_id="external-first", mode="fallback", source_queue="translation_fallback_pending")
            result = process_translation_exchange(
                root, bundle, lambda rows: [TranslationResult(
                    "remaining", "S", "", "failed", attempts=[AttemptRecord(
                        "translate", "external-a", "refused", ordinal=1,
                        model="fixture/external-a", issue_code="explicit_provider_refusal", operational=False,
                    )],
                )], route="external-a", route_models={"external-a": "fixture/external-a"}, full_route_plan=graph,
            )
            continuation = result["native_continuation"]
            source = root / continuation["source"]
            segments = read_segments(source)
            with self.assertRaisesRegex(ValueError, "E_TRANSLATION_PREDECESSOR_REQUIRED"):
                prepare_native_tasks(root, source, segments, root / "missing",
                    route_binding={"provider": "native-middle", "model": "fixture/native", "attempt": 1})
            prepare_native_tasks(root, source, segments, root / "native",
                predecessor_receipt=root / continuation["receipt"])
            manifest = json.loads((root / "native/manifest.json").read_text())
            self.assertEqual(manifest["route_binding"],
                {"provider": "native-middle", "model": "fixture/native", "attempt": 1})
            with self.assertRaisesRegex(ValueError, "E_TRANSLATION_PREDECESSOR_INVALID"):
                prepare_native_tasks(root, source, [replace(segments[0], source="CHANGED")], root / "changed",
                    predecessor_receipt=root / continuation["receipt"])
            replay = process_translation_exchange(
                root, bundle, lambda rows: self.fail("Consumed external phase cannot restart"),
                route="external-a", route_models={"external-a": "fixture/external-a"},
                full_route_plan=graph, retry_failures=True,
            )
            self.assertEqual(replay["native_continuation"], continuation)

    def test_partial_external_budget_resumes_with_bound_history_instead_of_resetting_ordinal(self):
        with tempfile.TemporaryDirectory() as directory:
            root, bundle = Path(directory), Path(directory) / "exchange"
            graph = [
                {"provider": "external-a", "model": "fixture/a", "max_retries": 1, "transport": "external"},
                {"provider": "native-b", "model": "fixture/b", "max_retries": 0, "transport": "native"},
            ]
            prepare_translation_exchange(root, [Segment("one", "S")], bundle,
                exchange_id="partial", mode="fallback", source_queue="translation_fallback_pending")
            first = AttemptRecord("translate", "external-a", "refused", ordinal=1,
                model="fixture/a", issue_code="explicit_provider_refusal", operational=False)
            initial = process_translation_exchange(
                root, bundle, lambda rows: [TranslationResult("one", "S", "", "failed", attempts=[first])],
                route="external-a", route_models={"external-a": "fixture/a"}, full_route_plan=graph,
            )
            self.assertIsNone(initial["native_continuation"])
            self.assertEqual(initial["continuation_repair_target_ids"], ["one"])
            def resume(rows, prior):
                self.assertEqual([(a.model, a.ordinal) for a in prior[0].attempts], [("fixture/a", 1)])
                return [TranslationResult("one", "S", "", "failed", attempts=[
                    *prior[0].attempts,
                    AttemptRecord("translate", "external-a", "refused", ordinal=2,
                        model="fixture/a", issue_code="explicit_provider_refusal", operational=False),
                ])]
            resumed = process_translation_exchange(
                root, bundle, lambda rows: self.fail("Fresh translator must not replace a continuation"),
                route="external-a", route_models={"external-a": "fixture/a"}, full_route_plan=graph,
                retry_failures=True, resume_translator=resume,
                continuation_status=lambda segment, prior: {"eligible": True},
            )
            self.assertIsNotNone(resumed["native_continuation"])
            outcome = json.loads((bundle / FAILURE_NAME).read_text())
            self.assertEqual([item["ordinal"] for item in outcome["attempt_lineage"]], [1, 2])

    def test_invalid_adapter_sibling_does_not_discard_a_valid_independent_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, bundle = Path(directory), Path(directory) / "exchange"
            prepare_translation_exchange(
                root, [Segment("ready", "S"), Segment("invalid", "S")], bundle,
                exchange_id="partial-adapter", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            with self.assertRaisesRegex(TranslationExchangeError, "ADAPTER_RESULT_BINDING_INVALID"):
                process_translation_exchange(
                    root, bundle, lambda rows: [
                        TranslationResult("ready", "S", "T", "translated", translator="first",
                            attempts=[AttemptRecord("translate", "first", "success", ordinal=1, model="exact")]),
                        TranslationResult("invalid", "S", "T", "translated", translator="first",
                            attempts=[AttemptRecord("translate", "first", "success", ordinal=1, model="wrong")]),
                    ], route="first", route_models={"first": "exact"},
                )
            successes = [json.loads(line) for line in (bundle / SUCCESS_NAME).read_text().splitlines()]
            self.assertEqual([row["target_id"] for row in successes], ["ready"])
            summary = seal_translation_exchange(root, bundle)
            self.assertEqual((summary["success_count"], summary["pending_count"]), (1, 1))
            with self.assertRaisesRegex(TranslationExchangeError, "ADAPTER_ATTEMPT_UNCERTAIN"):
                process_translation_exchange(
                    root, bundle, lambda rows: self.fail("Neither a success nor unknown attempt may repeat"),
                    route="first", route_models={"first": "exact"}, retry_failures=True,
                )

    def test_multi_adapter_plan_records_actual_success_and_exhausted_failure_models(self) -> None:
        from game_l10n.content_source_policy import content_source_policy
        from game_l10n.translation_exchange import ADAPTER_LEDGER_NAME
        plan = {"first": "publisher/first:high", "second": "publisher/second@configured"}
        with tempfile.TemporaryDirectory() as directory:
            root, calls = Path(directory), []
            bundle = root / "exchange"
            policy = content_source_policy(root, task_kind="translation", route="native-pass:translation")
            prepare_translation_exchange(
                root, [Segment(name, "S") for name in ("fallback", "primary", "failed")], bundle,
                exchange_id="multi-adapter", mode="fallback", source_queue="translation_fallback_pending",
                content_source_policy=policy,
            )
            def translate(rows):
                results = []
                for row in rows:
                    calls.append(row.id)
                    if row.id == "primary":
                        attempts = [AttemptRecord("translate", "first", "success", ordinal=1, model=plan["first"])]
                        provider, status, text = "first", "translated", "T"
                    elif row.id == "fallback":
                        attempts = [
                            AttemptRecord("translate", "first", "refused", ordinal=1, model=plan["first"],
                                          issue_code="explicit_provider_refusal"),
                            AttemptRecord("translate", "second", "success", ordinal=1, model=plan["second"]),
                        ]
                        provider, status, text = "second", "translated", "T"
                    else:
                        attempts = [
                            AttemptRecord("translate", "first", "error", ordinal=1, model=plan["first"],
                                          issue_code="timeout", operational=True),
                            AttemptRecord("translate", "second", "error", ordinal=1, model=plan["second"],
                                          issue_code="quota_exhausted", operational=True),
                        ]
                        provider, status, text = "", "failed", ""
                    results.append(TranslationResult(
                        row.id, row.source, text, status, translator=provider, attempts=attempts
                    ))
                return results
            result = process_translation_exchange(
                root, bundle, translate, route="first", model=plan["first"], route_models=plan, wave_size=1,
            )
            successes = [json.loads(line) for line in (bundle / SUCCESS_NAME).read_text().splitlines()]
            failure = json.loads((bundle / FAILURE_NAME).read_text())
            self.assertEqual([(row["route"], row["model"]) for row in successes],
                             [("second", plan["second"]), ("first", plan["first"])])
            self.assertEqual(successes[0]["content_provenance"]["model"], plan["second"])
            self.assertEqual([a["provider"] for a in successes[0]["attempt_lineage"]], ["first", "second"])
            self.assertEqual((failure["route"], failure["model"]), ("second", plan["second"]))
            self.assertFalse(failure["retryable"])
            self.assertEqual(result["issue_code"], "E_FALLBACK_OPERATIONAL_ROUTES_EXHAUSTED")
            from game_l10n.formats import read_segments
            from game_l10n.native_followup import collect_translation_exchange_candidates, CANDIDATE_METADATA_KEY
            collected = collect_translation_exchange_candidates(
                root, [Segment(name, "S") for name in ("fallback", "primary", "failed")],
                bundle, root / "review.jsonl", root / "unresolved.jsonl",
            )
            candidate = read_segments(root / "review.jsonl")[0].metadata[CANDIDATE_METADATA_KEY]
            self.assertEqual(collected["candidate_count"], 2)
            self.assertEqual(candidate["model"], plan["second"])
            self.assertEqual([a["provider"] for a in candidate["adapter_attempt_lineage"]], ["first", "second"])
            checkpoint = (bundle / SUCCESS_NAME).read_bytes()
            ledger = (bundle / ADAPTER_LEDGER_NAME).read_bytes()
            resumed = process_translation_exchange(
                root, bundle, lambda rows: self.fail("Locked successes and exhausted operational routes cannot repeat"),
                route="first", model=plan["first"], route_models=plan, retry_failures=True,
            )
            self.assertEqual(resumed["processed"], 0)
            self.assertEqual((bundle / SUCCESS_NAME).read_bytes(), checkpoint)
            with self.assertRaisesRegex(TranslationExchangeError, "ADAPTER_PLAN_IMMUTABLE"):
                process_translation_exchange(
                    root, bundle, lambda rows: self.fail("Changed plan must not execute"),
                    route="second", model=plan["second"],
                    route_models={"second": plan["second"], "first": plan["first"]},
                )
            self.assertEqual((bundle / ADAPTER_LEDGER_NAME).read_bytes(), ledger)
            self.assertEqual(calls, ["fallback", "primary", "failed"])
            successes[0]["text"] = "CHANGED"
            (bundle / SUCCESS_NAME).write_text("".join(json.dumps(row) + "\n" for row in successes))
            with self.assertRaisesRegex(TranslationExchangeError, "E_FALLBACK_SUCCESS_CHANGED"):
                seal_translation_exchange(root, bundle)

    def test_multi_adapter_rejects_wrong_model_and_does_not_replay_unknown_attempt(self) -> None:
        from game_l10n.translation_exchange import ADAPTER_LEDGER_NAME
        with tempfile.TemporaryDirectory() as directory:
            root, bundle = Path(directory), Path(directory) / "exchange"
            prepare_translation_exchange(
                root, [Segment("line", "S")], bundle, exchange_id="wrong-model",
                mode="fallback", source_queue="translation_fallback_pending",
            )
            with self.assertRaisesRegex(TranslationExchangeError, "RESULT_BINDING_INVALID"):
                process_translation_exchange(
                    root, bundle, lambda rows: [TranslationResult(
                        "line", "S", "T", "translated", translator="first",
                        attempts=[AttemptRecord("translate", "first", "success", ordinal=1, model="different-model")],
                    )], route="first", route_models={"first": "exact-first", "second": "exact-second"},
                )
            self.assertEqual((bundle / SUCCESS_NAME).read_bytes(), b"")
            self.assertIsNotNone(json.loads((bundle / ADAPTER_LEDGER_NAME).read_text())["active_event"])
            with self.assertRaisesRegex(TranslationExchangeError, "ADAPTER_ATTEMPT_UNCERTAIN"):
                process_translation_exchange(
                    root, bundle, lambda rows: self.fail("Unknown execution must not repeat"),
                    route="first", route_models={"first": "exact-first", "second": "exact-second"},
                )

    def test_binding_new_plan_preserves_legacy_success_without_inventing_its_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, bundle = Path(directory), Path(directory) / "exchange"
            prepare_translation_exchange(
                root, [Segment("old", "S"), Segment("new", "S")], bundle,
                exchange_id="inherited-success", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            requests = [json.loads(line) for line in (bundle / REQUESTS_NAME).read_text().splitlines()]
            old = {"target_id": "old", "status": "success", "text": "OLD", "route": "historical",
                   "attempt": 1, "source_sha256": requests[0]["source_sha256"],
                   "context_sha256": requests[0]["context_sha256"]}
            (bundle / SUCCESS_NAME).write_text(json.dumps(old) + "\n")
            def translate(rows):
                self.assertEqual([row.id for row in rows], ["new"])
                return [TranslationResult("new", "S", "NEW", "translated", translator="first",
                    attempts=[AttemptRecord("translate", "first", "success", ordinal=1, model="exact-first")])]
            result = process_translation_exchange(
                root, bundle, translate, route="first", route_models={"first": "exact-first"},
            )
            successes = [json.loads(line) for line in (bundle / SUCCESS_NAME).read_text().splitlines()]
            self.assertEqual(successes[0], old)
            self.assertNotIn("model", successes[0])
            self.assertEqual(result["success_count"], 2)

    def test_elapsed_budget_does_not_require_a_token_ceiling_or_metered_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("one", "S"), Segment("two", "S")], bundle,
                exchange_id="elapsed-only", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            result = process_translation_exchange(
                root, bundle,
                lambda rows: [TranslationResult(s.id, s.source, "T", "translated") for s in rows],
                route="configured", max_elapsed_minutes=1, wave_size=1,
            )
            self.assertEqual(result["success_count"], 2)
            self.assertEqual(result["issue_code"], "")
            self.assertIsNone(result["budget"]["max_input_tokens"])
            self.assertFalse(result["budget"]["input_tokens_known"])
            state = json.loads((root / FALLBACK_USAGE_RELATIVE).read_text())
            self.assertEqual([event["accounting"] for event in state["events"]],
                             ["unmeasured", "unmeasured"])

    def test_unlimited_input_does_not_invent_a_time_budget_or_wave_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "exchange"
            prepare_translation_exchange(
                root, [Segment("line", "S")], bundle,
                exchange_id="unlimited", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            result = process_translation_exchange(
                root, bundle, lambda rows: [TranslationResult("line", "S", "T", "translated")],
                route="configured", input_tokens_unlimited=True, wave_size=1024,
            )
            self.assertEqual(result["processed"], 1)
            self.assertFalse((root / FALLBACK_USAGE_RELATIVE).exists())

    def test_exchange_can_exceed_previous_bundle_record_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = prepare_translation_exchange(
                root, [Segment(str(i), "S") for i in range(2001)], root / "exchange",
                exchange_id="large-exchange", mode="fallback",
                source_queue="translation_fallback_pending",
            )
            self.assertEqual(prepared["target_count"], 2001)
            self.assertEqual(seal_translation_exchange(root, root / "exchange")["pending_count"], 2001)

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
                with self.assertRaisesRegex(TranslationExchangeError, "ELAPSED_BUDGET_INVALID"):
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
            result = process_translation_exchange(
                root, bundle, lambda segments: self.fail("User limit must remain bound"),
                route="fallback",
            )
            self.assertEqual(result["issue_code"], "E_FALLBACK_INPUT_BUDGET_EXHAUSTED")
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
