from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.formats import read_segments
from game_l10n.content_source_policy import create_content_source_grant, direct_model_provenance
from game_l10n.models import Segment
from game_l10n.native_pass import (
    merge_native_pass,
    native_control_step,
    prepare_native_tasks,
    seal_native_wave_relay,
)
from game_l10n.native_runtime import configure_native_runtime
from game_l10n.native_translation_retry import (
    STATE_NAME,
    advance_native_translation_retries,
    export_native_translation_retry_cohort,
    load_native_translation_retry_cohort_history,
    load_native_translation_retry_cohort_successes,
    load_native_translation_retry_cohort_root_source,
    native_translation_retry_route_plan,
    repair_native_translation_retries,
    revise_native_translation_retry_content,
    require_pre_fallback_retry_state,
    start_native_translation_retries,
    verify_native_translation_retry_cohort,
    NativeTranslationRetryError,
)
from tests import write_semantic_result_rows
from tests.test_validators import optional_slot_fixture

FIXTURE_MODELS = {
    "native-luna": "fixture/luna",
    "native-sol": "fixture/sol",
    "native-approved-successor": "fixture/approved-successor",
}
FIXTURE_ROUTE_PLAN = [
    {"provider": "native-luna", "model": FIXTURE_MODELS["native-luna"], "max_retries": 5},
    {"provider": "native-sol", "model": FIXTURE_MODELS["native-sol"], "max_retries": 3},
]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    provider = next((row.get("provider") for row in rows if row.get("provider") in FIXTURE_MODELS), None)
    write_semantic_result_rows(path, rows, model=FIXTURE_MODELS.get(provider))


def _result(target_id: str, status: str, text: str = "", provider: str = "native-luna") -> dict:
    return {
        "target_id": target_id,
        "status": status,
        "text": text,
        "provider": provider,
        "issues": [],
    }


class NativeTranslationRetryTests(unittest.TestCase):
    def test_ordered_route_selector_stops_at_each_external_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configure_native_runtime(root, "codex", {
                "native-first": "fixture/native-first", "native-second": "fixture/native-second",
            })
            config = {
                "routing": {
                    "translators": ["native-first", "api-a", "native-second", "api-b"],
                    "translator_retries": {"native-first": 2, "native-second": 1},
                },
                "providers": [
                    {"name": name, "type": "command", "model": f"fixture/{name}"}
                    for name in ("api-a", "api-b")
                ],
            }
            self.assertEqual(native_translation_retry_route_plan(root, config), [
                {"provider": "native-first", "model": "fixture/native-first", "max_retries": 2},
            ])
            config["routing"]["translators"] = ["api-a", "native-first", "api-b", "native-second"]
            self.assertEqual(native_translation_retry_route_plan(root, config), [])

    def test_new_retry_state_never_guesses_a_model_from_its_provider_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = Segment("pending", "SOURCE")
            source, first = root / "source.jsonl", root / "first.jsonl"
            _write_jsonl(source, [asdict(segment)])
            _write_jsonl(first, [_result("pending", "refused")])
            with self.assertRaisesRegex(NativeTranslationRetryError, "E_NATIVE_RETRY_ROUTE_BINDING_REQUIRED"):
                start_native_translation_retries(
                    root, source, [segment], first, root / "retry",
                    route_plan=[{"provider": "native-luna", "max_retries": 1}],
                )
            self.assertFalse((root / "retry" / STATE_NAME).exists())

    def test_operational_successor_preserves_semantic_cohorts_and_successes(self) -> None:
        policy = {
            "schema": "parr0t.execution-environment-policy.v1", "host": "omp",
            "authorization": "user_requested",
            "fallback_on": ["provider_operational_error", "execution_guard_denied"],
            "fallback_strategy": "next_configured_native_route",
            "preserve_successes": True, "preserve_isolation": True,
            "silent_model_switch_allowed": False,
        }
        plan = [
            {"provider": "native-luna", "model": FIXTURE_MODELS["native-luna"], "max_retries": 1},
            {"provider": "native-sol", "model": FIXTURE_MODELS["native-sol"], "max_retries": 0},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment(item, item.upper()) for item in ("locked", "operational", "semantic")]
            source, first, state_dir = root / "source.jsonl", root / "first.jsonl", root / "retry"
            _write_jsonl(source, [asdict(item) for item in segments])
            _write_jsonl(first, [
                _result("locked", "success", "LOCKED"),
                {**_result("operational", "error", provider="native-control"), "issues": ["execution_guard_denied"]},
                _result("semantic", "refused"),
            ])
            with patch("game_l10n.native_runtime.active_execution_environment_policy", return_value=policy):
                state = start_native_translation_retries(root, source, segments, first, state_dir, route_plan=plan)
            self.assertEqual((state["next_provider"], state["next_attempt"]), ("native-sol", 1))
            self.assertEqual([item.id for item in read_segments(root / state["pending_input"])], ["operational"])
            self.assertEqual(state["deferred_failure_count"], 1)
            pending_path = root / state["pending_input"]
            pending_bytes = pending_path.read_bytes()
            pending_path.write_text("{unrelated pending corruption", encoding="utf-8")
            try:
                selected = export_native_translation_retry_cohort(root, state_dir, cohort="successful")
                checked = require_pre_fallback_retry_state(
                    root, root / selected["cohort_lineage"]["export_path"],
                    root / selected["cohort_input"], root / selected["cohort_results"], route_plan=plan,
                )
                self.assertEqual(checked["candidate_count"], 1)
            finally:
                pending_path.write_bytes(pending_bytes)
            stored = json.loads((state_dir / STATE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(stored["execution_environment_policy"], policy)
            self.assertEqual(stored["history"][0]["semantic_attempt_consumed_ids"], ["locked", "semantic"])
            self.assertEqual(stored["history"][0]["incomplete_execution_target_ids"], ["operational"])
            self.assertEqual(stored["target_progress"]["semantic"]["attempt"], 2)
            original_locked = (root / state["successful_results"]).read_bytes()
            old_output = state["native_pass_output"]

            operational = root / "operational.jsonl"
            _write_jsonl(operational, [
                {**_result("operational", "error", provider="native-control"), "issues": ["execution_guard_denied"]},
            ])
            state = advance_native_translation_retries(root, state_dir, operational)
            self.assertEqual((state["next_provider"], state["next_attempt"]), ("native-luna", 2))
            self.assertNotEqual(state["native_pass_output"], old_output)
            self.assertEqual(state["repair_needed_count"], 1)
            self.assertEqual([item.id for item in read_segments(root / state["pending_input"])], ["semantic"])
            self.assertEqual((root / state["successful_results"]).read_bytes(), original_locked)

            semantic = root / "semantic.jsonl"
            _write_jsonl(semantic, [_result("semantic", "refused")])
            state = advance_native_translation_retries(root, state_dir, semantic)
            self.assertEqual((state["next_provider"], state["next_attempt"]), ("native-sol", 1))
            _write_jsonl(semantic, [_result("semantic", "refused", provider="native-sol")])
            state = advance_native_translation_retries(root, state_dir, semantic)
            self.assertEqual(state["next_action"], "repair_native_runtime")
            self.assertEqual(state["native_exhausted_count"], 1)
            self.assertEqual([item.id for item in read_segments(root / state["native_exhausted_input"])], ["semantic"])
            self.assertEqual([item.id for item in read_segments(root / state["repair_input"])], ["operational"])
            repair = [json.loads(line) for line in (root / state["repair_results"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual(repair[0]["issues"], ["native_route_repair_required"])
            self.assertNotIn("source_preserved", json.dumps(repair))
            with self.assertRaisesRegex(NativeTranslationRetryError, "not complete"):
                require_pre_fallback_retry_state(root, state_dir / STATE_NAME, source, root / state["pre_fallback_results"], route_plan=plan)
            eligible = require_pre_fallback_retry_state(
                root, state_dir / STATE_NAME, root / state["native_exhausted_input"],
                root / state["native_exhausted_results"], route_plan=plan,
            )
            self.assertEqual(eligible["native_exhausted_count"], 1)
            extended = [*plan, {"provider": "native-approved-successor", "model": FIXTURE_MODELS["native-approved-successor"], "max_retries": 0}]
            previous_output = state["native_pass_output"]
            with patch("game_l10n.native_runtime.active_execution_environment_policy", return_value=policy):
                state = repair_native_translation_retries(root, state_dir, route_plan=extended)
            self.assertEqual(state["repaired_count"], 2)
            self.assertEqual((state["next_provider"], state["next_attempt"]), ("native-approved-successor", 1))
            self.assertNotEqual(state["native_pass_output"], previous_output)
            self.assertEqual(state["native_exhausted_count"], 0)
            self.assertEqual([item.id for item in read_segments(root / state["pending_input"])], ["operational", "semantic"])
            self.assertEqual((root / state["successful_results"]).read_bytes(), original_locked)
            with self.assertRaisesRegex(NativeTranslationRetryError, "Configured native routes changed"):
                require_pre_fallback_retry_state(
                    root, root / eligible["cohort_lineage"]["export_path"],
                    root / eligible["cohort_input"], root / eligible["cohort_results"],
                    route_plan=extended,
                )

    def test_explicit_output_retry_requires_sealed_import_and_keeps_primary_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [
                Segment("locked", "LOCKED"), Segment("missing", "MISSING"),
                Segment("malformed", "MALFORMED"), Segment("not-started", "NOT STARTED"),
            ]
            source, first, state_dir = root / "source.jsonl", root / "first.jsonl", root / "retry"
            _write_jsonl(source, [asdict(item) for item in segments])
            tasks, results = root / "tasks", root / "results"
            manifest_path = tasks / "manifest.json"
            binding = {"provider": "native-luna", "model": FIXTURE_MODELS["native-luna"], "attempt": 1}
            prepare_native_tasks(
                root, source, segments, tasks, max_records=1, min_records=1, max_workers=4,
                route_binding=binding,
            )
            step = native_control_step(root, manifest_path, results)
            for file in sorted(tasks.glob("task-*.jsonl")):
                target = json.loads(file.read_text(encoding="utf-8"))["target_id"]
                if target == "locked":
                    _write_jsonl(results / file.name, [_result(target, "success", "LOCKED TARGET")])
                elif target == "malformed":
                    (results / file.name).write_text("{invalid result\n", encoding="utf-8")
                elif target == "not-started":
                    row = {**_result(target, "error", provider="native-control"), "issues": ["worker_not_started"]}
                    (results / file.name).write_text(json.dumps(row) + "\n", encoding="utf-8")
            seal_native_wave_relay(root, root / step["relay"]["dispatch"], relay_ended=True)
            collected = native_control_step(root, manifest_path, results, require_relay_receipt=True)
            self.assertEqual(collected["action"], "aggregate_completed_pass")
            merge_native_pass(root, segments, first, manifest_path=manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            policy = manifest["content_source_policy"]
            lineage = {
                "manifest": "tasks/manifest.json",
                "manifest_contract_fingerprint": manifest["contract_fingerprint"],
                "source_fingerprint": manifest["source_fingerprint"],
                "execution_namespace": manifest["execution_namespace"],
                "content_source_policy_digest": policy["policy_digest"],
                "route_binding": binding,
            }
            state = start_native_translation_retries(
                root, source, segments, first, state_dir, route_plan=FIXTURE_ROUTE_PLAN,
                content_policy=policy, primary_lineage=lineage,
            )
            self.assertEqual(state["next_action"], "repair_native_runtime")
            locked = (root / state["successful_results"]).read_bytes()
            original = (state_dir / STATE_NAME).read_bytes()
            self.assertEqual(
                repair_native_translation_retries(root, state_dir, route_plan=FIXTURE_ROUTE_PLAN)["repaired_count"],
                0,
            )
            receipt, = (root / ".game-l10n" / "native" / "waves").glob("*.json")
            receipt_bytes = receipt.read_bytes()
            receipt.unlink()
            self.assertEqual(repair_native_translation_retries(
                root, state_dir, route_plan=FIXTURE_ROUTE_PLAN, retry_failed_output=True,
            )["repaired_count"], 0)
            self.assertFalse(receipt.exists())
            self.assertEqual((state_dir / STATE_NAME).read_bytes(), original)
            receipt.write_bytes(receipt_bytes)

            with self.assertRaisesRegex(NativeTranslationRetryError, "OUTPUT_ROUTE_EXTENSION_CONFLICT"):
                repair_native_translation_retries(
                    root, state_dir, retry_failed_output=True, route_plan=[
                        *FIXTURE_ROUTE_PLAN,
                        {"provider": "native-extra", "model": "fixture/extra", "max_retries": 0},
                    ],
                )
            repaired = repair_native_translation_retries(
                root, state_dir, route_plan=FIXTURE_ROUTE_PLAN,
                retry_failed_output=True, incomplete_output_max_records=1,
            )
            self.assertEqual(repaired["repaired_count"], 2)
            self.assertEqual(repaired["incomplete_output_max_records"], 1)
            self.assertEqual((repaired["next_provider"], repaired["next_attempt"]), ("native-luna", 1))
            self.assertEqual(repaired["route_plan"], FIXTURE_ROUTE_PLAN)
            self.assertEqual(repaired["repair_needed_count"], 1)
            self.assertNotEqual(repaired["native_pass_output"], state["native_pass_output"])
            self.assertNotEqual(repaired["native_pass_results"], state["native_pass_results"])
            self.assertEqual((root / repaired["successful_results"]).read_bytes(), locked)
            self.assertEqual(
                [segment.id for segment in read_segments(root / repaired["pending_input"])],
                ["missing", "malformed"],
            )
            history = json.loads((state_dir / STATE_NAME).read_bytes())["history"]
            self.assertEqual(history[0]["semantic_attempt_consumed_ids"], ["locked"])
            self.assertFalse(repaired["provider_attempt_consumed"])
            prepared = root / repaired["native_pass_output"]
            prepare_native_tasks(
                root, root / repaired["pending_input"], read_segments(root / repaired["pending_input"]),
                prepared, max_records=1, route_binding=binding,
            )
            with self.assertRaisesRegex(NativeTranslationRetryError, "ATTEMPT_ACTIVE"):
                repair_native_translation_retries(
                    root, state_dir, route_plan=FIXTURE_ROUTE_PLAN, retry_failed_output=True,
                )

    def test_versioned_grant_revision_preserves_locked_candidates_and_ordinals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("locked", "LOCKED"), Segment("pending", "PENDING")]
            source, first, state_dir = root / "source.jsonl", root / "first.jsonl", root / "retry"
            _write_jsonl(source, [asdict(item) for item in segments])
            _write_jsonl(first, [_result("locked", "success", "LOCKED TARGET"), _result("pending", "refused")])
            state = start_native_translation_retries(root, source, segments, first, state_dir, route_plan=FIXTURE_ROUTE_PLAN)
            original_candidates = (root / state["successful_results"]).read_bytes()
            original_policy = state["content_source_policy"]
            previous_output = state["native_pass_output"]
            grant_path, _ = create_content_source_grant(
                root, authority="explicit_user", scope_id="synthetic-retry-v2",
                task_kinds=["translation"], routes=["native-pass:translation"],
                external_sources=["https://example.invalid/exact-reference"], user_confirmed=True,
            )
            state = revise_native_translation_retry_content(
                root, state_dir, grant_path=grant_path, scope_id="synthetic-retry-v2",
            )
            self.assertEqual(state["next_attempt"], 2)
            self.assertNotEqual(state["native_pass_output"], previous_output)
            self.assertEqual((root / state["successful_results"]).read_bytes(), original_candidates)
            exported = export_native_translation_retry_cohort(root, state_dir, cohort="successful")
            stored = json.loads((state_dir / STATE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(stored["content_source_policy"], original_policy)
            self.assertEqual(state["content_source_policy"]["scope_id"], "synthetic-retry-v2")
            retry = root / "scope-retry.jsonl"
            row = {
                **_result("pending", "success", "PENDING TARGET"),
                "content_provenance": direct_model_provenance(
                    state["content_source_policy"], FIXTURE_MODELS["native-luna"], route="native-luna",
                ),
            }
            retry.write_text(json.dumps(row) + "\n", encoding="utf-8")
            completed = advance_native_translation_retries(root, state_dir, retry)
            self.assertEqual((completed["next_action"], completed["candidate_count"]), ("complete", 2))
            (state_dir / STATE_NAME).write_text("{unrelated later state corruption", encoding="utf-8")
            checked = require_pre_fallback_retry_state(
                root, root / exported["cohort_lineage"]["export_path"],
                root / exported["cohort_input"], root / exported["cohort_results"],
                route_plan=[{"provider": "native-future", "model": "fixture/future", "max_retries": 0}],
            )
            self.assertEqual(checked["candidate_count"], 1)
            self.assertEqual(checked["content_source_policy"]["scope_id"], "synthetic-retry-v2")
            self.assertEqual(len(checked["content_source_policies"]), 2)
            self.assertEqual(checked["cohort_lineage"], exported["cohort_lineage"])
            without_native_routes = require_pre_fallback_retry_state(
                root, root / exported["cohort_lineage"]["export_path"],
                root / exported["cohort_input"], root / exported["cohort_results"],
                route_plan=[],
            )
            self.assertEqual(without_native_routes["cohort_lineage"], exported["cohort_lineage"])

    def test_all_export_rejects_same_id_source_mutation_and_freezes_consumed_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = Segment("pending", "ORIGINAL")
            source, first, state_dir = root / "source.jsonl", root / "first.jsonl", root / "retry"
            _write_jsonl(source, [asdict(segment)])
            _write_jsonl(first, [_result("pending", "refused")])
            start_native_translation_retries(
                root, source, [segment], first, state_dir,
                route_plan=[{**FIXTURE_ROUTE_PLAN[0], "max_retries": 0}],
            )
            exported = export_native_translation_retry_cohort(root, state_dir, cohort="all")
            frozen = exported["cohort_lineage"]
            history = load_native_translation_retry_cohort_history(root, frozen)
            self.assertEqual(history["target_ids"], ["pending"])
            self.assertEqual(history["history"][0]["model"], FIXTURE_MODELS["native-luna"])
            self.assertEqual(history["history"][0]["semantic_attempt_consumed_ids"], ["pending"])
            self.assertEqual(history["history"][0]["incomplete_execution_target_ids"], [])
            _write_jsonl(source, [asdict(Segment("pending", "MODIFIED"))])
            with self.assertRaisesRegex(NativeTranslationRetryError, "source changed"):
                export_native_translation_retry_cohort(root, state_dir, cohort="all")
            (state_dir / STATE_NAME).write_text("{unrelated mutable-state corruption", encoding="utf-8")
            self.assertEqual(load_native_translation_retry_cohort_history(root, frozen), history)
            history_path = root / frozen["attempt_history_artifact"]
            history_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(NativeTranslationRetryError, "history changed"):
                load_native_translation_retry_cohort_history(root, frozen)

    def test_failure_cohort_recovers_full_root_successes_from_immutable_historical_policies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment(key, f"SOURCE {key}") for key in ("a", "b", "c")]
            source, first, state_dir = root / "source.jsonl", root / "first.jsonl", root / "retry"
            plan = [{**route, "max_retries": 0} for route in FIXTURE_ROUTE_PLAN]
            _write_jsonl(source, [asdict(item) for item in segments])
            _write_jsonl(first, [
                _result("a", "success", "TARGET A"), _result("b", "refused"), _result("c", "refused"),
            ])
            state = start_native_translation_retries(root, source, segments, first, state_dir, route_plan=plan)
            original_policy = state["content_source_policy"]
            grant, _ = create_content_source_grant(
                root, authority="explicit_user", scope_id="history-policy-v2",
                task_kinds=["translation"], routes=["native-pass:translation"],
                external_sources=["https://example.invalid/exact-reference"], user_confirmed=True,
            )
            state = revise_native_translation_retry_content(
                root, state_dir, grant_path=grant, scope_id="history-policy-v2",
            )
            active_policy = state["content_source_policy"]
            second = root / "second.jsonl"
            second.write_text("".join(json.dumps(row) + "\n" for row in [
                {
                    **_result("b", "success", "TARGET B", provider="native-sol"),
                    "content_provenance": direct_model_provenance(
                        active_policy, FIXTURE_MODELS["native-sol"], route="native-sol",
                    ),
                },
                _result("c", "refused", provider="native-sol"),
            ]), encoding="utf-8")
            state = advance_native_translation_retries(root, state_dir, second)
            exported = export_native_translation_retry_cohort(root, state_dir, cohort="native_exhausted")
            self.assertEqual([item.id for item in read_segments(root / exported["cohort_input"])], ["c"])
            frozen = exported["cohort_lineage"]
            frozen_root = root / frozen["root_source_artifact"]
            self.assertEqual(load_native_translation_retry_cohort_root_source(root, frozen), segments)
            history = load_native_translation_retry_cohort_history(root, frozen)["history"]
            source.write_text("{mutable source corruption", encoding="utf-8")
            first.write_text("{mutable original result corruption", encoding="utf-8")
            (state_dir / STATE_NAME).write_text("{mutable state corruption", encoding="utf-8")
            (root / state["successful_results"]).write_text("{mutable candidate corruption", encoding="utf-8")
            locked = load_native_translation_retry_cohort_successes(root, frozen, segments=segments)
            self.assertEqual({key: row["text"] for key, row in locked.items()}, {"a": "TARGET A", "b": "TARGET B"})
            self.assertEqual(locked["a"]["content_provenance"]["policy_digest"], original_policy["policy_digest"])
            self.assertEqual(locked["b"]["content_provenance"]["policy_digest"], active_policy["policy_digest"])
            self.assertEqual((locked["a"]["provider"], locked["b"]["provider"]), ("native-luna", "native-sol"))
            with self.assertRaisesRegex(NativeTranslationRetryError, "complete bound root inventory"):
                load_native_translation_retry_cohort_successes(root, frozen, segments=segments[1:])
            with self.assertRaisesRegex(NativeTranslationRetryError, "complete bound root inventory"):
                load_native_translation_retry_cohort_successes(
                    root, frozen, segments=[Segment("a", "MODIFIED"), *segments[1:]],
                )
            archive = root / history[0]["result_artifact"]
            archive.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(NativeTranslationRetryError, "immutable result history changed"):
                load_native_translation_retry_cohort_successes(root, frozen, segments=segments)
            _write_jsonl(frozen_root, [asdict(Segment("a", "MODIFIED")), *[asdict(item) for item in segments[1:]]])
            with self.assertRaisesRegex(NativeTranslationRetryError, "frozen root inventory changed"):
                load_native_translation_retry_cohort_root_source(root, frozen)
            checked = verify_native_translation_retry_cohort(
                root, frozen, source_path=root / exported["cohort_input"],
                results_path=root / exported["cohort_results"],
            )
            self.assertEqual(checked["root_source_artifact"], frozen["root_source_artifact"])
            frozen_root.unlink()
            verify_native_translation_retry_cohort(
                root, frozen, source_path=root / exported["cohort_input"],
                results_path=root / exported["cohort_results"],
            )
            with self.assertRaisesRegex(NativeTranslationRetryError, "frozen root inventory changed"):
                load_native_translation_retry_cohort_root_source(root, frozen)

    def test_successful_export_can_omit_unavailable_unrelated_root_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("done", "DONE SOURCE"), Segment("pending", "PENDING SOURCE")]
            source, first, state_dir = root / "source.jsonl", root / "first.jsonl", root / "retry"
            _write_jsonl(source, [asdict(item) for item in segments])
            _write_jsonl(first, [_result("done", "success", "DONE TARGET"), _result("pending", "refused")])
            start_native_translation_retries(
                root, source, segments, first, state_dir,
                route_plan=[{**FIXTURE_ROUTE_PLAN[0], "max_retries": 0}],
            )
            source.write_text("{unrelated root corruption", encoding="utf-8")
            exported = export_native_translation_retry_cohort(root, state_dir, cohort="successful")
            frozen = exported["cohort_lineage"]
            self.assertNotIn("root_source_artifact", frozen)
            self.assertNotIn("root_source_sha256", frozen)
            checked = verify_native_translation_retry_cohort(
                root, frozen, source_path=root / exported["cohort_input"],
                results_path=root / exported["cohort_results"],
            )
            self.assertNotIn("root_source_artifact", checked)
            with self.assertRaisesRegex(NativeTranslationRetryError, "root inventory is unavailable"):
                load_native_translation_retry_cohort_root_source(root, frozen)

    def test_missing_output_is_not_fabricated_operational_evidence_or_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = Segment("pending", "PENDING")
            source, first = root / "source.jsonl", root / "first.jsonl"
            _write_jsonl(source, [asdict(segment)])
            _write_jsonl(first, [{**_result("pending", "error", provider="native-control"), "issues": ["worker_ended_without_result"]}])
            with patch("game_l10n.native_runtime.active_execution_environment_policy", return_value={"fallback_on": ["provider_operational_error", "execution_guard_denied"]}):
                state = start_native_translation_retries(root, source, [segment], first, root / "retry", route_plan=FIXTURE_ROUTE_PLAN)
            self.assertEqual(state["next_action"], "repair_native_runtime")
            self.assertEqual(state["native_exhausted_count"], 0)
            self.assertFalse(state["provider_attempt_consumed"])
            stored = json.loads((root / "retry" / STATE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(stored["history"][0]["operational_failure_codes"], {})

    def test_unapproved_operational_successor_returns_actionable_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = Segment("pending", "PENDING")
            source, first = root / "source.jsonl", root / "first.jsonl"
            _write_jsonl(source, [asdict(segment)])
            _write_jsonl(first, [{**_result("pending", "error", provider="native-control"), "issues": ["execution_guard_denied"]}])
            with patch("game_l10n.native_runtime.active_execution_environment_policy", return_value=None):
                state = start_native_translation_retries(root, source, [segment], first, root / "retry", route_plan=FIXTURE_ROUTE_PLAN)
            self.assertEqual(state["next_action"], "repair_native_runtime")
            self.assertEqual(state["repair_needed_count"], 1)
            self.assertEqual(state["native_exhausted_count"], 0)
            self.assertTrue(state["repair_actions"])

    def test_resume_reads_the_original_json_source_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"id": "a", "source": "A"},
                            {"id": "b", "source": "B"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            segments = read_segments(source)
            first = root / "first.jsonl"
            _write_jsonl(
                first,
                [_result("a", "success", "TA\u0085next\u2028line\u2029paragraph"), _result("b", "error")],
            )
            state_dir = root / "retry"
            start_native_translation_retries(
                root, source, segments, first, state_dir, route_plan=FIXTURE_ROUTE_PLAN,
            )
            retry = root / "retry.jsonl"
            _write_jsonl(retry, [_result("b", "success", "TB\u0085next\u2028line\u2029paragraph")])

            state = advance_native_translation_retries(root, state_dir, retry)

        self.assertEqual(state["next_action"], "complete")
        self.assertEqual(state["pending_count"], 0)

    def test_failed_only_luna_then_sol_attempts_gate_external_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("a", "A"), Segment("b", "B"), Segment("c", "C")]
            source = root / "source.jsonl"
            _write_jsonl(source, [asdict(segment) for segment in segments])
            first = root / "first.jsonl"
            _write_jsonl(
                first,
                [
                    _result("a", "success", "TA"),
                    _result("b", "refused"),
                    _result("c", "error"),
                ],
            )
            state_dir = root / "retry"
            state = start_native_translation_retries(
                root, source, segments, first, state_dir, route_plan=FIXTURE_ROUTE_PLAN,
            )
            self.assertEqual(state["next_provider"], "native-luna")
            self.assertEqual(state["next_attempt"], 2)
            self.assertEqual(state["pending_count"], 2)

            for attempt in range(2, 6):
                result_path = root / f"luna-{attempt}.jsonl"
                _write_jsonl(
                    result_path,
                    [_result("b", "refused"), _result("c", "error")],
                )
                state = advance_native_translation_retries(
                    root, state_dir, result_path
                )
            fifth = root / "luna-6.jsonl"
            _write_jsonl(
                fifth,
                [_result("b", "success", "TB"), _result("c", "refused")],
            )
            state = advance_native_translation_retries(root, state_dir, fifth)
            self.assertEqual(state["next_provider"], "native-sol")
            self.assertEqual(state["next_attempt"], 1)
            self.assertEqual(state["pending_count"], 1)

            for attempt in range(1, 5):
                result_path = root / f"sol-{attempt}.jsonl"
                _write_jsonl(
                    result_path,
                    [_result("c", "refused", provider="native-sol")],
                )
                state = advance_native_translation_retries(
                    root, state_dir, result_path
                )
            self.assertEqual(state["next_action"], "external_fallback")
            self.assertEqual(state["pending_count"], 1)
            final_path = root / state["pre_fallback_results"]
            final = [json.loads(line) for line in final_path.read_text().splitlines()]
            self.assertEqual([row["target_id"] for row in final], ["a", "b", "c"])
            self.assertEqual([row["status"] for row in final], ["success", "success", "error"])
            checked = require_pre_fallback_retry_state(
                root, state_dir / STATE_NAME, source, final_path, route_plan=FIXTURE_ROUTE_PLAN,
            )
            self.assertEqual(checked["next_action"], "external_fallback")

    def test_optional_empty_success_survives_failed_only_retry_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            optional = optional_slot_fixture(target_id="optional")
            segments = [Segment("label", "SOURCE_LABEL"), optional]
            source = root / "source.jsonl"
            source.write_text("".join(json.dumps(asdict(item)) + "\n" for item in segments), encoding="utf-8")
            first = root / "first.jsonl"
            _write_jsonl(first, [_result("label", "success", "TARGET_LABEL"), _result("optional", "error")])
            state_dir = root / "retry"
            state = start_native_translation_retries(root, source, segments, first, state_dir, route_plan=FIXTURE_ROUTE_PLAN)
            self.assertEqual([item.id for item in read_segments(root / state["pending_input"])], ["optional"])
            retry = root / "retry.jsonl"
            _write_jsonl(retry, [_result("optional", "success", "")])
            state = advance_native_translation_retries(root, state_dir, retry)
            self.assertEqual((state["next_action"], state["pending_count"], state["candidate_count"]), ("complete", 0, 2))
            rows = [json.loads(line) for line in (root / state["pre_fallback_results"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([(row["target_id"], row["status"], row["text"]) for row in rows], [
                ("label", "success", "TARGET_LABEL"), ("optional", "success", ""),
            ])

    def test_empty_success_without_bound_authority_and_optional_whitespace_stay_invalid(self) -> None:
        for segment, text in ((Segment("plain", "SOURCE"), ""), (optional_slot_fixture(), " ")):
            with self.subTest(target=segment.id, text=text), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source.jsonl"
                source.write_text(json.dumps(asdict(segment)) + "\n", encoding="utf-8")
                result = root / "result.jsonl"
                _write_jsonl(result, [_result(segment.id, "success", text)])
                with self.assertRaises(NativeTranslationRetryError):
                    start_native_translation_retries(root, source, [segment], result, root / "retry", route_plan=FIXTURE_ROUTE_PLAN)
                self.assertFalse((root / "retry" / STATE_NAME).exists())


if __name__ == "__main__":
    unittest.main()
