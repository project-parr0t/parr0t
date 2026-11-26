from __future__ import annotations

import json
import tempfile
import unittest
import time
from types import SimpleNamespace
from unittest.mock import patch
from dataclasses import asdict
from pathlib import Path

from game_l10n.formats import read_segments, read_translation_results
from game_l10n.native_pass import import_native_wave
from game_l10n.native_review import collect_native_review
from tests.test_native_pass import review_segment, prepare_native_tasks, write_semantic_result_rows
from game_l10n.orchestrator import OrchestratorConfig


class NativeReviewTests(unittest.TestCase):
    def test_additional_fallback_is_one_call_and_only_for_the_bound_candidate(self):
        from dataclasses import replace
        from game_l10n.models import AttemptRecord
        from game_l10n.native_pass import _load_task_manifest, _native_review_call_budget
        from game_l10n.native_review import _bound_result, _pending_segment
        from game_l10n.review_policy import _record_additional_fallback_review

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            provider, model = "native-review-fallback", "fixture/exact"
            (root / "game-l10n.json").write_text(json.dumps({"routing": {
                "reviewers": ["native-sol", provider], "review_attempt_limits": {provider: 2},
            }}), encoding="utf-8")
            segment = review_segment("held")
            result = _bound_result(segment)
            result.attempts = [AttemptRecord("review", provider, "rewrite", ordinal=ordinal, model=model)
                               for ordinal in (1, 2)]
            source = root / "held.jsonl"
            source.write_text(json.dumps(asdict(_pending_segment(segment, result))) + "\n", encoding="utf-8")
            segments = read_segments(source)
            output = _record_additional_fallback_review(root, segments, provider, model)
            ordinary = root / "ordinary"
            for dest in (output, ordinary):
                prepare_native_tasks(root, source, segments, dest, pass_kind="review", review_tier="strong",
                                     route_binding={"provider": provider, "model": model, "attempt": 1})
            scoped_path, ordinary_path = output / "manifest.json", ordinary / "manifest.json"
            scoped = _load_task_manifest(root, scoped_path)
            self.assertEqual(_native_review_call_budget(root, scoped_path, scoped), 1)
            self.assertEqual(_native_review_call_budget(root, ordinary_path, _load_task_manifest(root, ordinary_path)), 0)
            with self.assertRaisesRegex(ValueError, "APPROVAL_INVALID"):
                _native_review_call_budget(root, ordinary_path, scoped)
            with self.assertRaisesRegex(ValueError, "SCOPE_IMMUTABLE"):
                _record_additional_fallback_review(root, [replace(segments[0], source="different")], provider, model)

    def test_explicit_review_budget_does_not_reset_when_candidate_generation_advances(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, _pending_segment, partition_native_review_routes
        segment = review_segment("same-task-new-candidate")
        result = _bound_result(segment)
        result.attempts = [
            AttemptRecord("review", "configured", "rewrite", ordinal=3,
                          model="fixture/exact", candidate_generation=0, operational=False),
        ]
        groups = partition_native_review_routes(
            [_pending_segment(segment, result)], ["configured"], stage="review",
            route_models={"configured": "fixture/exact"}, attempt_limits={"configured": 3},
        )
        self.assertEqual(list(groups), [""])

    def test_authorized_alternate_judge_only_reviews_that_models_translations(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, _pending_segment, partition_native_review_routes
        fallback, alternate = "native-fallback", "native-alternate"
        author, judge = "fixture/author", "fixture/judge"
        direct = review_segment("ordinary")
        routed = review_segment("requires-independent-judge")
        routed.metadata["_parr0t_candidate"]["content_provenance_lineage"] = [{"model": author}]
        policy = {"route_models": {fallback: author, alternate: judge},
                  "candidate_model_routes": {author: alternate}, "attempt_limits": {alternate: 2}}
        groups = partition_native_review_routes([direct, routed], [fallback], stage="review", **policy)
        self.assertEqual(groups, {fallback: [direct], alternate: [routed]})
        self.assertEqual(partition_native_review_routes([routed], [alternate], stage="review-triage", **policy),
                         {alternate: [routed]})
        result = _bound_result(routed)
        result.attempts = [
            AttemptRecord("review", alternate, "rewrite", model=judge, ordinal=i,
                          candidate_generation=result.candidate_generation)
            for i in (1, 2)
        ]
        self.assertEqual(list(partition_native_review_routes(
            [_pending_segment(routed, result)], [fallback], stage="review", **policy)), [""])

    def test_new_exact_model_under_same_role_is_not_suppressed_by_old_failure_or_rewrite(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, _pending_segment, partition_native_review_routes
        segment = review_segment("changed-model")
        result = _bound_result(segment)
        result.attempts = [
            AttemptRecord("review", "configured-role", "error", ordinal=ordinal,
                          model="fixture/old", candidate_generation=1)
            for ordinal in (1, 2)
        ]
        groups = partition_native_review_routes(
            [_pending_segment(segment, result)], ["configured-role"], stage="review",
            route_models={"configured-role": "fixture/new"},
            attempt_limits={"configured-role": 2},
        )
        self.assertEqual(list(groups), ["configured-role"])
        result.attempts = [AttemptRecord(
            "review-rewrite", "configured-role", "success", model="fixture/old", candidate_generation=1,
        )]
        groups = partition_native_review_routes(
            [_pending_segment(segment, result)], ["configured-role"], stage="review-rewrite",
            route_models={"configured-role": "fixture/new"},
        )
        self.assertEqual(list(groups), ["configured-role"])

    def test_language_noop_allows_one_repair_without_reopening_other_rewrites(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, _pending_segment, partition_native_review_routes

        segments = []
        for key in ("language-noop", "ordinary-noop", "repair-already-attempted"):
            segment = review_segment(key, candidate="visible untranslated words")
            result = _bound_result(segment)
            result.attempts = [
                AttemptRecord("review-rewrite", "native-rewrite", "success", candidate_generation=1),
                AttemptRecord("review", "native-fallback", "approved", candidate_generation=1),
            ]
            if key != "ordinary-noop":
                result.metadata["target_language_risk"] = ["target_script_absent"]
            if key == "repair-already-attempted":
                result.attempts.append(AttemptRecord("review-rewrite", "native-rewrite", "error",
                                                     candidate_generation=1))
            segments.append(_pending_segment(segment, result))
        groups = partition_native_review_routes(segments, ["native-rewrite"], stage="review-rewrite")
        self.assertEqual({k: [s.id for s in v] for k, v in groups.items()},
                         {"native-rewrite": ["language-noop"],
                          "": ["ordinary-noop", "repair-already-attempted"]})

    def test_mixed_pending_routes_partition_by_configured_policy_not_model_name(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, _pending_segment, partition_native_review_routes
        routes = ["native-sol", "native-configured", "external-configured"]
        segments = []
        for name, attempts in (("primary", []), ("secondary", [("native-sol", "error")]),
                               ("external", [("native-sol", "error"), ("native-configured", "error")]),
                               ("exhausted", [(route, "error") for route in routes])):
            segment = review_segment(name)
            result = _bound_result(segment)
            result.attempts = [AttemptRecord("review", route, status, candidate_generation=1)
                               for route, status in attempts]
            segments.append(_pending_segment(segment, result))
        groups = partition_native_review_routes(segments, routes, stage="review")
        self.assertEqual(list(groups), [*routes, ""])
        self.assertEqual([[s.id for s in group] for group in groups.values()],
                         [["primary"], ["secondary"], ["external"], ["exhausted"]])
        segment = review_segment("configured-four-calls")
        result = _bound_result(segment)
        result.attempts = [AttemptRecord("review", "native-sol", "rewrite", ordinal=3,
                                        candidate_generation=1)]
        groups = partition_native_review_routes(
            [_pending_segment(segment, result)], routes, stage="review",
            attempt_limits={"native-sol": 4},
        )
        self.assertEqual(list(groups), ["native-sol"])

    def test_completed_review_cohort_progresses_while_sibling_wave_is_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            segments = [review_segment("ready"), review_segment("pending")]
            source.write_text("".join(json.dumps(asdict(s)) + "\n" for s in segments))
            segments = read_segments(source)
            tasks, results = root / "tasks", root / "results"
            prepare_native_tasks(
                root, source, segments, tasks, pass_kind="review",
                max_records=1, max_workers=1,
                route_binding={"provider": "native-configured", "model": "exact-model", "attempt": 1},
            )
            results.mkdir()
            write_semantic_result_rows(results / "task-0001.jsonl", [{
                "target_id": "ready", "status": "success", "text": "OK",
                "provider": "native-configured",
            }])
            import_native_wave(root, tasks / "manifest.json", results, 1)
            summary = collect_native_review(
                root, source, tasks / "manifest.json", root / "collected", tier="strong"
            )
            self.assertEqual(summary["accepted"], 1)
            self.assertEqual(summary["awaiting"], 1)
            self.assertFalse(summary["complete"])
            self.assertEqual([s.id for s in read_segments(root / "collected/awaiting-input.jsonl")],
                             ["pending"])
            approved = read_translation_results(root / "collected/accepted.json")[0]
            self.assertEqual(approved.reviewer, "native-configured")
            self.assertEqual(approved.attempts[0].model, "exact-model")
            accepted_bytes = (root / "collected/accepted.json").read_bytes()
            self.assertEqual(collect_native_review(
                root, source, tasks / "manifest.json", root / "collected", tier="strong"
            ), summary)
            self.assertEqual((root / "collected/accepted.json").read_bytes(), accepted_bytes)
            self.assertEqual(len(approved.attempts), 1)

    def test_delivery_preserves_failures_and_only_includes_pending_when_authorized(self):
        from game_l10n.models import AttemptRecord, TranslationResult
        from game_l10n.formats import write_results
        from game_l10n.native_review import (_bound_result, _pending_segment,
                                             materialize_delivery_candidates)
        from game_l10n.store import stable_hash
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [review_segment(k) for k in ["approved", "pending", "missing"]]
            source = root / "source.jsonl"
            source.write_text("\n".join(json.dumps(asdict(s)) for s in segments)+"\n")
            approved = TranslationResult("approved", "S", "T", "reviewed",
                attempts=[AttemptRecord("review", "native-sol", "approved", candidate_generation=1)],
                candidate_generation=1, candidate_digest=stable_hash("T"), acceptance_authority="reviewer")
            write_results(root / "approved.json", [approved])
            pending = _bound_result(segments[1])
            pending.attempts = [AttemptRecord("review", "native-sol", "rewrite", candidate_generation=1)]
            (root / "pending.jsonl").write_text(json.dumps(asdict(_pending_segment(segments[1], pending)))+"\n")
            (root / "failures.jsonl").write_text(json.dumps(asdict(segments[2]))+"\n")
            default_args = (
                root, source, root / "approved.json", [root / "pending.jsonl"],
                root / "failures.jsonl", root / "delivery-default.json",
            )
            authorized_args = (
                root, source, root / "approved.json", [root / "pending.jsonl"],
                root / "failures.jsonl", root / "delivery-authorized.json",
            )
            with patch("game_l10n.native_review.require_inventory_attestation"):
                default_summary = materialize_delivery_candidates(*default_args)
                summary = materialize_delivery_candidates(
                    *authorized_args, include_unreviewed=True
                )
                cached = materialize_delivery_candidates(
                    *authorized_args, include_unreviewed=True
                )
            self.assertEqual(
                (
                    default_summary["included"],
                    default_summary["reviewed"],
                    default_summary["included_unreviewed"],
                    default_summary["translation_failed"],
                    default_summary["source_preserved_total"],
                ),
                (2, 1, 0, 1, 1),
            )
            default_rows = read_translation_results(root / "delivery-default.json")
            self.assertEqual(
                [row.status for row in default_rows],
                ["reviewed", "source_preserved"],
            )
            self.assertEqual(default_rows[1].text, default_rows[1].source)
            self.assertEqual(default_rows[1].acceptance_authority, "route_exhaustion")
            self.assertEqual(
                (
                    summary["reviewed"],
                    summary["included_unreviewed"],
                    summary["translation_failed"],
                    summary["integrity_rejected"],
                ),
                (1, 1, 1, 0),
            )
            self.assertFalse(summary["cached"])
            self.assertTrue(cached["cached"])
            rows = read_translation_results(root / "delivery-authorized.json")
            self.assertEqual(
                [row.status for row in rows],
                ["reviewed", "accepted_unreviewed", "source_preserved"],
            )
            self.assertEqual(rows[1].attempts[-1].status, "rewrite")
            self.assertEqual(rows[1].acceptance_authority, "user_policy")
            self.assertEqual(rows[1].candidate_generation, 1)
            self.assertEqual(rows[1].metadata["delivery_review_status"], "not_approved")
            self.assertEqual(rows[2].text, rows[2].source)
            self.assertEqual(rows[2].resolution_code, "source_preserved")
            (root / "wrong.jsonl").write_text("")
            with patch("game_l10n.native_review.require_inventory_attestation"):
                with self.assertRaisesRegex(ValueError, "QUEUE_MISMATCH"):
                    materialize_delivery_candidates(root, source, root / "approved.json",
                        [root / "pending.jsonl"], root / "wrong.jsonl", root / "invalid.json",
                        include_unreviewed=True)
            self.assertFalse((root / "invalid.json").exists())

    def test_delivery_source_preserves_generic_integrity_rejections(self):
        from game_l10n.formats import write_results
        from game_l10n.models import AttemptRecord, FailureCode
        from game_l10n.native_review import (
            _bound_result,
            _pending_segment,
            materialize_delivery_candidates,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = review_segment(
                "unsafe",
                source="SOURCE_VALUE {name}",
                candidate="TARGET_WITHOUT_PLACEHOLDER",
            )
            source = root / "source.jsonl"
            source.write_text(json.dumps(asdict(segment)) + "\n", encoding="utf-8")
            write_results(root / "approved.json", [])
            pending = _bound_result(segment)
            pending.attempts = [
                AttemptRecord(
                    "review", "native-sol", "rewrite", candidate_generation=1
                )
            ]
            (root / "pending.jsonl").write_text(
                json.dumps(asdict(_pending_segment(segment, pending))) + "\n",
                encoding="utf-8",
            )
            (root / "failures.jsonl").write_text("", encoding="utf-8")

            with patch("game_l10n.native_review.require_inventory_attestation"):
                summary = materialize_delivery_candidates(
                    root,
                    source,
                    root / "approved.json",
                    [root / "pending.jsonl"],
                    root / "failures.jsonl",
                    root / "delivery.json",
                    include_unreviewed=True,
                    report=root / "delivery-report.json",
                )

            self.assertEqual(summary["included"], 1)
            self.assertEqual(summary["integrity_rejected"], 1)
            self.assertEqual(summary["source_preserved_total"], 1)
            preserved = read_translation_results(root / "delivery.json")
            self.assertEqual([row.status for row in preserved], ["source_preserved"])
            self.assertEqual(preserved[0].text, preserved[0].source)
            self.assertEqual(preserved[0].acceptance_authority, "integrity_gate")
            self.assertEqual(
                preserved[0].resolution_code, FailureCode.INTEGRITY_INVALID.value
            )
            self.assertEqual(preserved[0].attempts[0].stage, "review")
            self.assertEqual(
                preserved[0].attempts[-1].issue_code,
                FailureCode.INTEGRITY_INVALID.value,
            )
            report = json.loads((root / "delivery-report.json").read_text())
            self.assertEqual(report["integrity_rejections"][0]["target_id"], "unsafe")
            self.assertTrue(report["integrity_rejections"][0]["issue_codes"])

    def test_native_rewrite_preserves_history_and_requires_current_confirmation(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import (_bound_result, _pending_segment,
                                             collect_native_rewrite)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = []
            for key in ["changed", "unchanged", "failed", "untranslated"]:
                base = review_segment(key)
                result = _bound_result(base)
                result.attempts = [
                    AttemptRecord("review-triage", "native-luna", "rewrite", candidate_generation=1),
                    AttemptRecord("review", "native-sol", "rewrite", candidate_generation=1),
                ]
                result.metadata["rewrite_issues"] = ["voice"]
                if key in {"changed", "untranslated"}:
                    result.metadata["target_language_risk"] = ["target_script_absent"]
                segments.append(_pending_segment(base, result))
            source = root / "source.jsonl"
            source.write_text("\n".join(json.dumps(asdict(s)) for s in segments) + "\n")
            segments = read_segments(source)
            tasks, results = root / "tasks", root / "results"
            prepare_native_tasks(root, source, segments, tasks, pass_kind="rewrite",
                                 min_records=1, max_records=2, max_workers=1)
            results.mkdir()
            rows = [{"target_id": s.id, "status": "success" if i != 2 else "error",
                     "text": ["\ubc88\uc5ed", "T", "", "untranslated text"][i], "issues": []}
                    for i, s in enumerate(segments)]
            write_semantic_result_rows(results / "task-0001.jsonl", rows[:2])
            import_native_wave(root, tasks / "manifest.json", results, 1)
            args = (root, source, tasks / "manifest.json", root / "collected")
            prefix_output = root / "prefix"
            prefix = collect_native_rewrite(root, source, tasks / "manifest.json", prefix_output,
                                            target_language="ko")
            self.assertEqual((prefix["confirmation_pending"], prefix["awaiting"]), (2, 2))
            write_semantic_result_rows(results / "task-0002.jsonl", rows[2:])
            import_native_wave(root, tasks / "manifest.json", results, 2)
            tail = root / "tail"
            pending = prefix_output / "awaiting-input.jsonl"
            collect_native_rewrite(root, source, tasks / "manifest.json", tail,
                                   awaiting_input=pending, target_language="ko")
            tail_rows = read_translation_results(tail / "results.json")
            self.assertEqual([r.segment_id for r in tail_rows], ["failed", "untranslated"])
            self.assertEqual(tail_rows[1].attempts[-1].issues, ["target_script_absent"])
            self.assertEqual(read_segments(tail / "confirmation-input.jsonl"), [])
            invalid = root / "reordered.jsonl"
            invalid.write_text("\n".join(json.dumps(asdict(s)) for s in reversed(segments)) + "\n")
            with self.assertRaisesRegex(ValueError, "E_NATIVE_REWRITE_AWAITING_INPUT_INVALID"):
                collect_native_rewrite(root, source, tasks / "manifest.json", root / "invalid",
                                       awaiting_input=invalid)
            summary = collect_native_rewrite(*args, target_language="ko")
            self.assertEqual(summary["accepted"], 0)
            self.assertEqual(summary["confirmation_pending"], 2)
            self.assertEqual(summary["unresolved"], 2)
            confirmation = read_segments(root / "collected/confirmation-input.jsonl")
            self.assertEqual([_bound_result(s).candidate_generation for s in confirmation], [2, 1])
            self.assertTrue(all(len(_bound_result(s).attempts) == 3 for s in confirmation))
            pending = _bound_result(read_segments(root / "collected/unresolved-input.jsonl")[0])
            self.assertEqual(pending.text, "T")
            self.assertEqual(pending.candidate_generation, 1)
            self.assertEqual(pending.attempts[-1].status, "error")
            self.assertEqual(collect_native_rewrite(*args, target_language="ko"), summary)
            self.assertTrue(all(r.status != "reviewed" and not r.acceptance_authority
                for r in read_translation_results(root / "collected/results.json")))
            # A rewriter's output cannot be accepted without a fresh judge.
            from game_l10n.native_review import collect_reviewed_candidates
            with patch("game_l10n.native_review.require_inventory_attestation"):
                with self.assertRaisesRegex(ValueError, "ACCEPTED_RESULT_INVALID"):
                    collect_reviewed_candidates(root, source,
                        [root / "collected/results.json"], root / "invalid.json")

    def test_retranslation_keeps_sealed_review_history_and_requires_confirmation(self):
        from dataclasses import replace
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import (
            _bound_result, _issue_review_queue, _pending_segment, _review_queue_parents, collect_native_rewrite,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            provider, model = "native-sol", "fixture/sol"
            bases = [replace(review_segment(key), previous="previous", next="next")
                     for key in ("repaired", "unsafe")]
            parents = {}
            for index, route in enumerate(("escalation", "rewrite", "rewrite_retry")):
                pending = []
                for base in bases:
                    state = _bound_result(base)
                    state.attempts = [
                        AttemptRecord("review", "native-luna", "rewrite", candidate_generation=1),
                        AttemptRecord("review-rewrite", "native-rewrite", "error", candidate_generation=1,
                                      issues=["target_script_absent"]),
                    ]
                    state.metadata["rewrite_issues"] = ["meaning"]
                    state.metadata["target_language_risk"] = ["target_script_absent"]
                    pending.append(_pending_segment(base, state))
                _issue_review_queue(root, pending, route, str(index), parents)
                parents = _review_queue_parents(pending)
            source, tasks, results = root / "pending.jsonl", root / "tasks", root / "results"
            source.write_text("".join(json.dumps(asdict(row)) + "\n" for row in pending), encoding="utf-8")
            prepare_native_tasks(root, source, read_segments(source), tasks, pass_kind="translation",
                                 route_binding={"provider": provider, "model": model, "attempt": 1})
            results.mkdir()
            write_semantic_result_rows(results / "task-0001.jsonl", [
                {"target_id": "repaired", "status": "success", "text": "\ubc88\uc5ed", "issues": []},
                {"target_id": "unsafe", "status": "success", "text": "untranslated", "issues": []},
            ])
            manifest = tasks / "manifest.json"
            import_native_wave(root, manifest, results, 1)
            (root / "game-l10n.json").write_text(json.dumps({"routing": {
                "review_triagers": ["native-luna"], "translators": [provider],
            }}), encoding="utf-8")
            destination = root / "collected"
            with self.assertRaisesRegex(ValueError, "REWRITE_PASS_REQUIRED"):
                collect_native_rewrite(root, source, manifest, destination, target_language="ko")
            summary = collect_native_rewrite(root, source, manifest, destination, target_language="ko",
                                             language_integrity_recovery=True)
            self.assertEqual((summary["accepted"], summary["confirmation_pending"], summary["unresolved"]), (0, 1, 1))
            recovered = _bound_result(read_segments(destination / "confirmation-input.jsonl")[0])
            self.assertEqual((recovered.segment_id, recovered.text, recovered.candidate_generation), ("repaired", "\ubc88\uc5ed", 2))
            self.assertEqual([(a.provider, a.status) for a in recovered.attempts],
                             [("native-luna", "rewrite"), ("native-rewrite", "error"), (provider, "success")])
            self.assertEqual(_bound_result(read_segments(destination / "unresolved-input.jsonl")[0]).attempts[-1].issues,
                             ["target_script_absent"])

    def test_native_rewrite_rejects_unflagged_inputs(self):
        from game_l10n.native_review import collect_native_rewrite
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = review_segment("fixture")
            source = root / "source.jsonl"
            source.write_text(json.dumps(asdict(segment)) + "\n")
            tasks = root / "tasks"
            prepare_native_tasks(root, source, [segment], tasks, pass_kind="rewrite")
            with self.assertRaisesRegex(ValueError, "REWRITE_NOT_FLAGGED"):
                collect_native_rewrite(root, source, tasks / "manifest.json", root / "out")
            self.assertFalse((root / "out").exists())

    def test_collect_approved_disjoint_results_never_fills_missing_approvals(self):
        from game_l10n.models import AttemptRecord, TranslationResult
        from game_l10n.native_review import collect_reviewed_candidates
        from game_l10n.formats import write_results
        from game_l10n.store import stable_hash
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("\n".join(json.dumps(asdict(review_segment(k))) for k in ["a", "b"]) + "\n")
            segments = read_segments(source)
            prepare_native_tasks(root, source, segments, root / "tasks")
            result = TranslationResult("a", "S", "T", "reviewed", reviewer="native-sol",
                attempts=[AttemptRecord("review", "native-sol", "approved", candidate_generation=1)],
                metadata={"review_tier": "strong"}, candidate_generation=1,
                candidate_digest=stable_hash("T"), acceptance_authority="reviewer")
            write_results(root / "accepted.json", [result])
            args = (root, source, [root / "accepted.json"], root / "joined.json")
            with patch("game_l10n.native_review.require_inventory_attestation"):
                summary = collect_reviewed_candidates(*args)
            self.assertEqual(summary["reviewed"], 1)
            self.assertEqual(summary["unresolved"], 1)
            with patch("game_l10n.native_review.require_inventory_attestation"):
                self.assertEqual(collect_reviewed_candidates(*args), summary)
            result.text = "English prose remains here"
            result.candidate_digest = stable_hash(result.text)
            write_results(root / "wrong-language.json", [result])
            with patch("game_l10n.native_review.require_inventory_attestation"):
                with self.assertRaisesRegex(ValueError, "ACCEPTED_RESULT_INVALID"):
                    collect_reviewed_candidates(
                        root,
                        source,
                        [root / "wrong-language.json"],
                        root / "wrong-language-output.json",
                        target_language="ko",
                    )
            result.candidate_generation = 2
            write_results(root / "stale.json", [result])
            with patch("game_l10n.native_review.require_inventory_attestation"):
                with self.assertRaisesRegex(ValueError, "ACCEPTED_RESULT_INVALID"):
                    collect_reviewed_candidates(root, source,
                                                [root / "stale.json"], root / "invalid.json")
            self.assertFalse((root / "invalid.json").exists())

    def test_resume_review_queues_uses_only_latest_unapproved_evidence(self):
        from game_l10n.formats import write_results
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, _pending_segment, _resume_review_queues

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = {key: review_segment(key) for key in ("approved", "later", "conflict", "missing")}

            def save(name, rows):
                path = root / name
                path.write_text("".join(json.dumps(asdict(row)) + "\n" for row in rows), encoding="utf-8")
                return path

            original = save("inventory.jsonl", items.values())
            result = _bound_result(items["approved"])
            result.status = "reviewed"
            result.acceptance_authority = "reviewer"
            write_results(root / "approved.json", [result])

            def pending(key, *attempts):
                item = items[key]
                state = _bound_result(item)
                state.attempts = list(attempts)
                return _pending_segment(item, state)

            triage = AttemptRecord("review-triage", "native-luna", "rewrite")
            strong = AttemptRecord("review", "native-sol", "error")
            prior = save("strong-input.jsonl", [
                pending("approved", triage), pending("later", triage), pending("conflict", triage)
            ])
            later = save("fallback-input.jsonl", [
                pending("later", triage, strong), pending("conflict", strong)
            ])
            output = root / "continuation"
            with patch("game_l10n.native_review.require_inventory_attestation"):
                summary = _resume_review_queues(
                    root, original, root / "approved.json", [later, prior], output,
                )
            self.assertEqual((summary["unreviewed"], summary["missing"], summary["conflicting"]), (3, 1, 1))
            self.assertEqual(set(summary["queues"]), {"fallback"})
            self.assertEqual(read_segments(output / "fallback.jsonl"), read_segments(later)[:1])

    def test_resume_review_queues_coalesces_only_exact_ready_routes(self):
        from dataclasses import replace
        from game_l10n.formats import write_results
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, _pending_segment, _resume_review_queues
        from game_l10n.native_runtime import RUNTIME_PATH, RUNTIME_SCHEMA
        from game_l10n.store import stable_hash

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            primary, shared, alternate, unbound = (
                "native-primary", "native-shared", "native-alternate", "native-unbound",
            )
            allowed, denied = "external-allowed", "external-denied"
            judges = [primary, shared, alternate, unbound, allowed, denied]
            models = {primary: "fixture/primary", shared: "fixture/exact", alternate: "fixture/exact"}
            (root / "game-l10n.json").write_text(json.dumps({
                "routing": {"reviewers": judges, "rewriters": [shared], "strong_max_calls": 2,
                            "review_attempt_limits": {primary: 1}},
                "native_workflow": {"external_provider_roles": ["judge_fallback"]},
                "providers": [
                    {"name": allowed, "model": "fixture/api", "allowed_roles": ["judge_fallback"]},
                    {"name": denied, "model": "fixture/api", "allowed_roles": []},
                ],
            }), encoding="utf-8")
            runtime = {"schema": RUNTIME_SCHEMA, "host": "codex", "control_generation": 1, "models": models}
            runtime_path = root / RUNTIME_PATH
            runtime_path.parent.mkdir(parents=True)
            runtime_path.write_text(json.dumps({**runtime, "fingerprint": stable_hash(runtime)}), encoding="utf-8")

            ids = ("fallback", "waiting", "confirmation", "alternate", "rewrite", "denied", "unbound",
                   "exhausted", "approved", "context-edge", "conflict", "missing", "api", "unstarted",
                   "rewrite-retry")
            items = {key: replace(review_segment(key), previous="P", next="N") for key in ids}

            def save(name, rows):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("".join(json.dumps(asdict(row)) + "\n" for row in rows), encoding="utf-8")
                return path

            def pending(item, *attempts, rewrite=False):
                result = _bound_result(item)
                result.attempts.extend(attempts)
                if rewrite:
                    result.metadata["rewrite_issues"] = ["voice"]
                return _pending_segment(item, result)

            def fail(provider, status="error"):
                return AttemptRecord("review", provider, status, candidate_generation=1,
                                     model=models.get(provider, "" if provider == unbound else "fixture/api"))

            fallback = pending(items["fallback"], fail(primary))
            waiting = pending(items["waiting"])
            prior = pending(items["confirmation"], fail(primary, "rewrite"), rewrite=True)
            rewritten = replace(items["confirmation"], metadata={
                "_parr0t_candidate": {"text": "R", "generation": 2, "digest": stable_hash("R"),
                                       "provider": shared},
            })
            confirmation = pending(rewritten, fail(primary, "rewrite"), AttemptRecord(
                "review-rewrite", shared, "success", candidate_generation=1, model=models[shared],
            ))
            edge = pending(replace(items["context-edge"], previous=""), fail(primary))
            other_route = pending(items["alternate"], *map(fail, judges[:2]))
            rewrite = pending(items["rewrite"], fail(primary, "rewrite"), rewrite=True)
            retry = pending(items["rewrite-retry"], fail(primary, "rewrite"), AttemptRecord(
                "review-rewrite", shared, "error", candidate_generation=1,
                model=models[shared], operational=True,
            ), rewrite=True)
            api = pending(items["api"], *map(fail, judges[:4]))
            forbidden = pending(items["denied"], *map(fail, judges[:5]))
            missing_binding = pending(items["unbound"], *map(fail, judges[:3]))
            exhausted = pending(items["exhausted"], *map(fail, judges))
            approved = _bound_result(items["approved"])
            approved.status, approved.acceptance_authority = "reviewed", "reviewer"
            approved_path = root / "approved.json"
            write_results(approved_path, [approved])
            inventory = save("inventory.jsonl", items.values())
            queues = [
                save("confirmation-input.jsonl", [confirmation]),
                save("fallback-input.jsonl", [other_route, forbidden, missing_binding, exhausted,
                                             fallback, api, pending(items["conflict"], fail(primary))]),
                save("other/fallback-input.jsonl", [edge, pending(items["conflict"], fail(primary, "refused"))]),
                save("rewrite-input.jsonl", [prior, rewrite]),
                save("unresolved-input.jsonl", [retry]),
                save("strong-input.jsonl", [waiting, pending(items["approved"])]),
                save("awaiting-input.jsonl", [waiting, items["unstarted"]]),
            ]
            evidence = {path: path.read_bytes() for path in [inventory, approved_path, *queues]}
            with patch("game_l10n.native_review.require_inventory_attestation"):
                summary = _resume_review_queues(root, inventory, approved_path, queues, root / "resume")
            self.assertEqual((summary["unreviewed"], summary["missing"], summary["conflicting"]), (14, 1, 1))
            actual = [
                (entry["status"], entry.get("operation"), entry.get("provider"), entry.get("model"),
                 entry["count"], read_segments(root / entry["input"]))
                for entry in summary["queues"].values()
            ]
            self.assertEqual(actual, [
                ("native_route_pending", "judge", shared, models[shared], 2, [fallback, confirmation]),
                ("native_route_pending", "judge", shared, models[shared], 1, [edge]),
                ("awaiting", None, None, None, 2, [waiting, items["unstarted"]]),
                ("native_route_pending", "judge", alternate, models[alternate], 1, [other_route]),
                ("native_route_pending", "rewrite", shared, models[shared], 2, [rewrite, retry]),
                ("repair_required", "judge", None, None, 2, [forbidden, missing_binding]),
                ("routes_exhausted", "judge", None, None, 1, [exhausted]),
                ("external_route_required", "judge", allowed, "fixture/api", 1, [api]),
            ])
            resumed_paths = [root / entry["input"] for entry in summary["queues"].values()]
            with patch("game_l10n.native_review.require_inventory_attestation"):
                resumed = _resume_review_queues(
                    root, inventory, approved_path, [*resumed_paths, *reversed(queues)], root / "resume-again",
                )
            self.assertEqual((resumed["missing"], resumed["conflicting"]), (1, 1))
            self.assertEqual([read_segments(root / entry["input"]) for entry in resumed["queues"].values()],
                             [entry[-1] for entry in actual])
            self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)

    def test_resume_review_queues_coalesces_only_compatible_native_judgments(self):
        from dataclasses import replace
        from game_l10n.formats import write_results
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import (
            _bound_result, _issue_review_queue, _lock_review_approvals, _pending_segment,
            _resume_review_queues, _review_queue_parents,
        )
        from game_l10n.native_runtime import RUNTIME_PATH, RUNTIME_SCHEMA
        from game_l10n.store import stable_hash

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            primary, shared, alternate = "native-primary", "native-shared", "native-alternate"
            denied, model = "external-denied", "fixture/exact"
            routing = {
                "review_triagers": ["native-triage"], "reviewers": [primary, shared, denied],
                "rewriters": [shared], "review_candidate_model_routes": {
                    "fixture/author": shared, "fixture/other": alternate, "fixture/unbound": "native-missing",
                },
            }
            (root / "game-l10n.json").write_text(json.dumps({
                "routing": routing, "providers": [{"name": denied, "model": "fixture/api", "allowed_roles": []}],
            }), encoding="utf-8")
            runtime_path = root / RUNTIME_PATH
            runtime_path.parent.mkdir(parents=True, exist_ok=True)
            runtime = {"schema": RUNTIME_SCHEMA, "host": "codex", "control_generation": 1,
                       "models": {primary: "fixture/primary", shared: model, alternate: model}}
            runtime_path.write_text(json.dumps({**runtime, "fingerprint": stable_hash(runtime)}), encoding="utf-8")
            authors = {"confirmation": "fixture/author", "alternate": "fixture/other", "unbound": "fixture/unbound"}
            ids = ("fallback", "waiting", "confirmation", "alternate", "rewrite", "denied", "unbound",
                   "exhausted", "approved", "locked", "stale", "conflict", "missing")
            items = {key: replace(review_segment(key), previous="P", next="N") for key in ids}
            for key, author in authors.items():
                items[key].metadata["_parr0t_candidate"]["content_provenance_lineage"] = [{"model": author}]

            def save(name, rows):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("".join(json.dumps(asdict(row)) + "\n" for row in rows), encoding="utf-8")
                return path

            def pending(item, *attempts, rewrite=False):
                result = _bound_result(item)
                result.attempts.extend(attempts)
                if rewrite:
                    result.metadata["rewrite_issues"] = ["voice"]
                return _pending_segment(item, result)

            def attempt(provider, status="error", stage="review"):
                return AttemptRecord(stage, provider, status, candidate_generation=1,
                                     model=runtime["models"].get(provider, "fixture/api"))

            def issue(route, rows, parents=()):
                _issue_review_queue(root, rows, route, stable_hash(route), _review_queue_parents(list(parents)))
                return rows

            inventory = save("inventory.jsonl", items.values())
            initial = {s.id: s for s in issue("escalation", [pending(s) for s in items.values()])}
            confirmation_parent = issue("rewrite", [
                pending(initial["confirmation"], attempt(primary, "rewrite"), rewrite=True),
            ], [initial["confirmation"]])
            confirmation = issue("confirmation", [
                pending(confirmation_parent[0], attempt(shared, "success", "review-rewrite")),
            ], confirmation_parent)[0]
            fallback_items = {
                "fallback": [attempt(primary)],
                "denied": [attempt(primary), attempt(shared)],
                "exhausted": [attempt(primary), attempt(shared), attempt(denied)],
                "conflict": [attempt(primary)],
            }
            fallback = issue("judge_fallback", [
                pending(initial[key], *attempts) for key, attempts in fallback_items.items()
            ], [initial[key] for key in fallback_items])
            conflicting = issue("judge_fallback", [pending(initial["conflict"], attempt(primary, "refused"))],
                                [initial["conflict"]])
            rewrite = issue("rewrite", [pending(initial["rewrite"], attempt(primary, "rewrite"), rewrite=True)],
                            [initial["rewrite"]])
            issue("judge_fallback", [pending(initial["stale"], attempt(primary))], [initial["stale"]])

            approved, locked = (_bound_result(items[key]) for key in ("approved", "locked"))
            for result in (approved, locked):
                result.status = "reviewed"
                result.acceptance_authority = "reviewer"
            approved_path = root / "approved.json"
            write_results(approved_path, [approved])
            _lock_review_approvals(root, [locked])
            queues = [
                save("confirmation-input.jsonl", [confirmation]), save("fallback-input.jsonl", fallback),
                save("other/fallback-input.jsonl", conflicting), save("rewrite-input.jsonl", rewrite),
                save("awaiting-input.jsonl", [initial["waiting"]]),
                save("strong-input.jsonl", [initial[key] for key in
                     ("waiting", "alternate", "unbound", "approved", "locked", "stale")]),
            ]
            with patch("game_l10n.native_review.require_inventory_attestation"):
                summary = _resume_review_queues(root, inventory, approved_path, queues, root / "resume")
            self.assertEqual((summary["unreviewed"], summary["missing"], summary["conflicting"]), (12, 3, 1))
            rows_by_route = {
                (entry["status"], entry.get("operation"), entry.get("provider")):
                    [s.id for s in read_segments(root / entry["input"])]
                for entry in summary["queues"].values() if entry["status"] != "repair_required"
            }
            self.assertEqual(rows_by_route, {
                ("native_route_pending", "judge", shared): ["fallback", "confirmation"],
                ("native_route_pending", "judge", alternate): ["alternate"],
                ("native_route_pending", "rewrite", shared): ["rewrite"],
                ("awaiting", None, None): ["waiting"],
                ("routes_exhausted", "judge", None): ["exhausted"],
            })
            repairs = [s.id for entry in summary["queues"].values() if entry["status"] == "repair_required"
                       for s in read_segments(root / entry["input"])]
            self.assertEqual(repairs, ["denied", "unbound"])
            action = next(entry for entry in summary["queues"].values()
                          if entry.get("operation") == "judge" and entry.get("provider") == shared)
            self.assertEqual(action["model"], model)
            combined_path = root / action["input"]
            combined = read_segments(combined_path)
            self.assertEqual(combined, [fallback[0], confirmation])
            prepared = prepare_native_tasks(
                root, combined_path, combined, root / "tasks", pass_kind="review", review_tier="strong",
                route_binding={"provider": shared, "model": model, "attempt": 1},
            )
            self.assertEqual(prepared["records"], 2)
            self.assertEqual(prepared["review_selection"]["routes"], {"confirmation": 1, "judge_fallback": 1})
            with patch("game_l10n.native_review.require_inventory_attestation"):
                resumed = _resume_review_queues(root, inventory, approved_path, [combined_path, *reversed(queues)],
                                                root / "resume-again")
                self.assertEqual(resumed["conflicting"], 1)
                self.assertEqual(read_segments(root / resumed["queues"][combined_path.parent.name]["input"]), combined)
                tampered = save("tampered/judge.jsonl", [replace(combined[0], speaker="changed"), combined[1]])
                with self.assertRaisesRegex(ValueError, "E_NATIVE_REVIEW_EVIDENCE_INVALID"):
                    _resume_review_queues(root, inventory, approved_path, [tampered], root / "rejected")

            # Separate source inputs can have real blank context at the seam.
            # Combining those rows would have the ordinary reader change a seal.
            isolated = [review_segment(key) for key in ("first-source", "second-source")]
            isolated_source = save("isolated-inventory.jsonl", isolated)
            isolated_parents = issue("escalation", [pending(s) for s in isolated])
            isolated_fallbacks = issue("judge_fallback", [
                pending(s, attempt(primary)) for s in isolated_parents
            ], isolated_parents)
            isolated_queues = [save(f"{s.id}/fallback-input.jsonl", [s]) for s in isolated_fallbacks]
            empty_approved = root / "empty-approved.json"
            write_results(empty_approved, [])
            with patch("game_l10n.native_review.require_inventory_attestation"):
                isolated_summary = _resume_review_queues(
                    root, isolated_source, empty_approved, isolated_queues, root / "resume-isolated",
                )
            self.assertEqual(
                [read_segments(root / entry["input"])[0] for entry in isolated_summary["queues"].values()],
                isolated_fallbacks,
            )

    def test_only_current_ok_is_accepted_and_other_targets_are_routed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            segments = [review_segment(f"fixture-{i}") for i in range(4)]
            source.write_text("\n".join(json.dumps(asdict(s)) for s in segments) + "\n")
            segments = read_segments(source)
            tasks, results = root / "tasks", root / "results"
            prepare_native_tasks(root, source, segments, tasks, pass_kind="review")
            results.mkdir()
            texts = ['{"verdict":"ok","issues":[]}',
                     '{"verdict":"rewrite","issues":["voice"]}',
                     'this is not a judge verdict', '']
            rows = [{"target_id": s.id, "status": "success" if i < 3 else "error",
                     "text": texts[i], "issues": [] if i < 3 else ["worker_ended_without_result"]}
                    for i, s in enumerate(segments)]
            write_semantic_result_rows(results / "task-0001.jsonl", rows)
            import_native_wave(root, tasks / "manifest.json", results, 1)
            summary = collect_native_review(root, source, tasks / "manifest.json",
                                            root / "collected", tier="triage", sample_percent=0)
            self.assertEqual(summary["accepted"], 1)
            self.assertEqual(summary["strong_pending"], 3)
            self.assertEqual(summary["invalid_verdicts"], 1)
            metrics = json.loads((root / "collected/review-metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(metrics["verdicts"], {"approved": 1, "error": 2, "rewrite": 1})
            self.assertEqual(metrics["issue_code_counts"], {"voice": 1})
            accepted = read_translation_results(root / "collected/accepted.json")
            self.assertEqual(accepted[0].text, "T")
            self.assertEqual(accepted[0].candidate_digest, segments[0].metadata["_parr0t_candidate"]["digest"])
            pending = read_segments(root / "collected/strong-input.jsonl")
            self.assertEqual([s.id for s in pending], [s.id for s in segments[1:]])
            # Re-collection creates neither semantic calls nor duplicate attempts.
            again = collect_native_review(root, source, tasks / "manifest.json",
                                          root / "collected", tier="triage", sample_percent=0)
            self.assertEqual(summary, again)
            self.assertEqual(len(read_translation_results(root / "collected/accepted.json")[0].attempts), 1)

    def test_plain_verdict_grammar_preserves_rewrites_and_rejects_malformed_results(self):
        from game_l10n.native_review import _bound_result
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            texts = ["ok", "rewrite VOICE,voice", "REWRITE voice,", "REWRITE ,voice",
                     "REWRITE\nvoice", "OK\n", "REWRITE grammar"]
            segments = [review_segment(f"fixture-{i}") for i in range(len(texts))]
            source = root / "source.jsonl"
            source.write_text("\n".join(json.dumps(asdict(s)) for s in segments) + "\n")
            segments = read_segments(source)
            tasks, results = root / "tasks", root / "results"
            prepare_native_tasks(root, source, segments, tasks, pass_kind="review")
            results.mkdir()
            write_semantic_result_rows(
                results / "task-0001.jsonl",
                [
                    {"target_id": s.id, "status": "success", "text": text}
                    for s, text in zip(segments, texts, strict=True)
                ],
            )
            import_native_wave(root, tasks / "manifest.json", results, 1)
            summary = collect_native_review(root, source, tasks / "manifest.json",
                                            root / "collected", tier="strong")
            accepted = read_translation_results(root / "collected/accepted.json")
            self.assertEqual([item.segment_id for item in accepted], [segments[0].id])
            rewrite = read_segments(root / "collected/rewrite-input.jsonl")
            self.assertEqual([item.id for item in rewrite], [segments[1].id])
            self.assertEqual(_bound_result(rewrite[0]).metadata["rewrite_issues"], ["voice"])
            fallback = read_segments(root / "collected/fallback-input.jsonl")
            self.assertEqual([item.id for item in fallback], [s.id for s in segments[2:]])
            self.assertEqual(summary["invalid_verdicts"], 5)

    def test_sample_and_high_risk_are_not_exempted_by_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = review_segment("fixture-1")
            source = root / "source.jsonl"
            source.write_text(json.dumps(asdict(segment)) + "\n")
            tasks, results = root / "tasks", root / "results"
            prepare_native_tasks(root, source, [segment], tasks, pass_kind="review")
            results.mkdir()
            write_semantic_result_rows(
                results / "task-0001.jsonl",
                [{"target_id": segment.id, "status": "success", "text": "OK"}],
            )
            import_native_wave(root, tasks / "manifest.json", results, 1)
            triage = collect_native_review(root, source, tasks / "manifest.json",
                                           root / "triage", tier="triage", sample_percent=100)
            self.assertEqual(triage["accepted"], 0)
            self.assertEqual(triage["strong_pending"], 1)
            strong_source = root / "triage/strong-input.jsonl"
            strong_segments = read_segments(strong_source)
            strong_tasks = root / "strong-tasks"
            prepare_native_tasks(root, strong_source, strong_segments, strong_tasks, pass_kind="review")
            import_native_wave(root, strong_tasks / "manifest.json", results, 1)
            strong = collect_native_review(root, strong_source, strong_tasks / "manifest.json",
                                           root / "strong", tier="strong")
            self.assertEqual(strong["accepted"], 1)
            accepted = read_translation_results(root / "strong/accepted.json")[0]
            self.assertEqual([a.stage for a in accepted.attempts], ["review-triage", "review"])
            from game_l10n.models import GlossaryEntry
            checked = collect_native_review(root, strong_source, strong_tasks / "manifest.json",
                root / "glossary-check", tier="strong", glossary=[GlossaryEntry("S", "REQUIRED")])
            self.assertEqual(checked["accepted"], 0)
            self.assertEqual(checked["rewrite_pending"], 1)

    def test_arbitrary_success_text_never_approves(self):
        from game_l10n.native_review import _compact_verdict
        for text in ('{"decision":"approved"}', '{"verdict":"ok","issues":["voice"]}',
                     '{"verdict":"rewrite","issues":[]}', 'translated text'):
            with self.assertRaises(Exception):
                _compact_verdict(text)

    def test_fallback_locks_results_and_rewrite_requires_fresh_confirmation(self):
        from game_l10n.models import (AgentEnvelope, AgentStatus, AttemptRecord,
                                      ContextPack, ReviewVerdictEnvelope, ReviewVerdict)
        from game_l10n.native_review import (_bound_result, _pending_segment,
                                            process_native_review_followup)
        from game_l10n.orchestrator import LocalizationOrchestrator
        from game_l10n.store import stable_hash
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = review_segment("fixture-1")
            result = _bound_result(base)
            result.attempts.append(AttemptRecord("review", "native-sol", "error", candidate_generation=1))
            segment = _pending_segment(base, result)
            source = root / "fallback.jsonl"
            source.write_text(json.dumps(asdict(segment)) + "\n")
            calls = []
            def judge(result, *args):
                calls.append("judge")
                result.attempts.append(AttemptRecord("review", "native-looking-adapter", "approved", candidate_generation=1))
                return ReviewVerdictEnvelope(ReviewVerdict.OK)
            fake = SimpleNamespace(
                registry=SimpleNamespace(get=lambda name: SimpleNamespace(name=name, fingerprint=lambda: {"name": name})),
                context_pack=ContextPack(), glossary=[], project=SimpleNamespace(target_language=""),
                config=OrchestratorConfig(["native-looking-adapter"], ["native-looking-adapter"], require_review=False,
                                          review_attempt_limits={"native-looking-adapter": 1},
                                          rewriter_chain=["native-looking-adapter"]),
                _judge_with_refusal_retry=judge,
            )
            first = process_native_review_followup(root, source, root / "out", fake,
                                                   operation="judge", deadline_epoch=time.time()+60,
                                                   primary_review_provider="native-sol")
            second = process_native_review_followup(root, source, root / "out", fake,
                                                    operation="judge", primary_review_provider="native-sol")
            self.assertEqual(first["accepted"], 1)
            self.assertEqual(second["executed"], 0)
            self.assertEqual(second["deadline_pending"], 0)
            self.assertEqual(calls, ["judge"])
            result.attempts[-1].status = "rewrite"
            result.metadata["rewrite_issues"] = ["voice"]
            source = root / "rewrite.jsonl"
            source.write_text(json.dumps(asdict(_pending_segment(base, result))) + "\n")
            def rewrite(result, *args):
                result.attempts.append(AttemptRecord("review-rewrite", "native-looking-adapter", "success", candidate_generation=1))
                return AgentEnvelope(AgentStatus.SUCCESS, "REVISED")
            fake._rewrite_with_refusal_retry = rewrite
            fake._set_candidate = LocalizationOrchestrator._set_candidate
            summary = process_native_review_followup(root, source, root / "rewrite-out", fake,
                                                     operation="rewrite", deadline_epoch=time.time()+60,
                                                     primary_review_provider="native-sol")
            self.assertEqual(summary["accepted"], 0)
            self.assertEqual(summary["confirmation_pending"], 1)
            confirmation = read_segments(root / "rewrite-out/confirmation-input.jsonl")[0]
            binding = confirmation.metadata["_parr0t_candidate"]
            self.assertEqual(binding["generation"], 2)
            self.assertEqual(binding["digest"], stable_hash("REVISED"))

    @staticmethod
    def _followup_fixture(root, *, operation, attempts, routes, response):
        from game_l10n.models import AttemptRecord, ContextPack
        from game_l10n.native_review import _bound_result, _pending_segment
        from game_l10n.orchestrator import LocalizationOrchestrator
        base = review_segment("fixture")
        result = _bound_result(base)
        result.attempts = [
            AttemptRecord("review", "configured-primary",
                          "error" if operation == "judge" else "rewrite", candidate_generation=1),
            *attempts,
        ]
        if operation == "rewrite":
            result.metadata["rewrite_issues"] = ["voice"]
        source = root / "input.jsonl"
        source.write_text(json.dumps(asdict(_pending_segment(base, result))) + "\n", encoding="utf-8")
        (root / "game-l10n.json").write_text(json.dumps({
            "routing": {"reviewers": ["configured-primary", *routes]},
        }), encoding="utf-8")
        fake = SimpleNamespace(
            registry=SimpleNamespace(get=lambda name: SimpleNamespace(
                name=name, fingerprint=lambda: {"name": name})),
            context_pack=ContextPack(), glossary=[], project=SimpleNamespace(target_language=""),
            config=OrchestratorConfig(
                routes, routes, require_review=False,
                review_attempt_limits={route: 1 for route in routes}, rewriter_chain=routes,
            ),
            _judge_with_refusal_retry=response, _rewrite_with_refusal_retry=response,
            _set_candidate=LocalizationOrchestrator._set_candidate,
        )
        return source, fake

    def test_failed_rewrite_advances_to_next_route_and_locks_confirmation_cache(self):
        from game_l10n.models import AgentEnvelope, AgentStatus, AttemptRecord
        from game_l10n.native_review import _bound_result, process_native_review_followup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def rewrite(result, provider, *args):
                calls.append(provider.name)
                result.attempts.append(AttemptRecord(
                    "review-rewrite", provider.name, "success", candidate_generation=1))
                return AgentEnvelope(AgentStatus.SUCCESS, "REVISED")
            source, fake = self._followup_fixture(root, operation="rewrite", attempts=[
                AttemptRecord("review-rewrite", "external-a", "error", candidate_generation=1),
                AttemptRecord("review-rewrite", "external-b", "error", candidate_generation=0),
            ], routes=["external-a", "external-b"], response=rewrite)
            original = source.read_bytes()
            first = process_native_review_followup(root, source, root / "out", fake, operation="rewrite")
            self.assertEqual((first["executed"], first["accepted"], first["confirmation_pending"]), (1, 0, 1))
            confirmation = read_segments(root / "out/confirmation-input.jsonl")[0]
            candidate = _bound_result(confirmation)
            self.assertEqual((candidate.text, candidate.candidate_generation), ("REVISED", 2))
            self.assertEqual(confirmation.metadata["_parr0t_candidate"]["provider"], "external-b")
            snapshots = {name: (root / "out" / name).read_bytes() for name in
                         ("results.json", "accepted.json", "confirmation-input.jsonl")}
            second = process_native_review_followup(root, source, root / "out", fake, operation="rewrite")
            self.assertEqual((second["executed"], second["confirmation_pending"]), (0, 1))
            self.assertEqual(calls, ["external-b"])
            self.assertEqual(source.read_bytes(), original)
            for name, content in snapshots.items():
                self.assertEqual((root / "out" / name).read_bytes(), content)

    def test_failed_followup_cache_advances_then_exhausts_without_replay(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, process_native_review_followup
        for operation in ("judge", "rewrite"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls = []
                def fail(result, provider, *args):
                    calls.append(provider.name)
                    result.attempts.append(AttemptRecord(
                        "review" if operation == "judge" else "review-rewrite",
                        provider.name, "error", candidate_generation=result.candidate_generation))
                    return None
                source, fake = self._followup_fixture(root, operation=operation, attempts=[],
                    routes=["external-a", "external-b"], response=fail)
                for expected in (1, 1, 0, 0):
                    summary = process_native_review_followup(
                        root, source, root / "out", fake, operation=operation)
                    self.assertEqual((summary["executed"], summary["unresolved"], summary["accepted"]),
                                     (expected, 1, 0))
                self.assertEqual(calls, ["external-a", "external-b"])
                pending = _bound_result(read_segments(root / "out/unresolved-input.jsonl")[0])
                self.assertEqual(pending.text, "T")
                self.assertEqual(pending.candidate_generation, 1)
                self.assertFalse(pending.acceptance_authority)
                self.assertEqual([a.provider for a in pending.attempts[-2:]], calls)

    def test_unstarted_authentication_block_does_not_write_review_checkpoint(self):
        from game_l10n.models import (
            AgentEnvelope, AgentStatus, AttemptRecord, ReviewVerdict, ReviewVerdictEnvelope,
        )
        from game_l10n.native_review import _bound_result, process_native_review_followup

        for operation in ("judge", "rewrite"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                blocked = {"external-a", "external-b"}
                stage = "review" if operation == "judge" else "review-rewrite"

                def respond(result, provider, *args):
                    if provider.name in blocked:
                        result.metadata["provider_authentication_blocked"] = True
                        result.errors.append("fixture unstarted")
                        return None
                    if provider.name == "external-a":
                        # An actual attempted failure is still recorded, even with the marker.
                        result.metadata["provider_authentication_blocked"] = True
                        result.attempts.append(AttemptRecord(stage, provider.name, "error", candidate_generation=1))
                        return None
                    status = "approved" if operation == "judge" else "success"
                    result.attempts.append(AttemptRecord(stage, provider.name, status, candidate_generation=1))
                    return (ReviewVerdictEnvelope(ReviewVerdict.OK) if operation == "judge"
                            else AgentEnvelope(AgentStatus.SUCCESS, "REVISED"))

                source, fake = self._followup_fixture(root, operation=operation, attempts=[],
                    routes=["external-a", "external-b"], response=respond)
                output = root / "out"

                def followup():
                    return process_native_review_followup(root, source, output, fake, operation=operation)

                def counts(summary):
                    return (summary["executed"], summary["authentication_pending"], summary["deadline_pending"])

                self.assertEqual(counts(followup()), (0, 1, 0))
                self.assertFalse((output / "results.json").exists())
                self.assertEqual(read_segments(output / "unresolved-input.jsonl"), read_segments(source))
                blocked.remove("external-a")
                self.assertEqual(counts(followup()), (1, 0, 0))
                checkpoint = (output / "results.json").read_bytes()
                self.assertEqual(counts(followup()), (0, 1, 0))
                self.assertEqual((output / "results.json").read_bytes(), checkpoint)
                pending = _bound_result(read_segments(output / "unresolved-input.jsonl")[0])
                self.assertEqual([a.provider for a in pending.attempts], ["configured-primary", "external-a"])
                self.assertEqual(pending.errors, [])
                blocked.remove("external-b")
                recovered = followup()
                self.assertEqual(counts(recovered), (1, 0, 0))
                self.assertEqual(recovered["accepted" if operation == "judge" else "confirmation_pending"], 1)

    def test_exhausted_rewrite_history_stays_pending_without_provider_lookup(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import process_native_review_followup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, fake = self._followup_fixture(root, operation="rewrite", attempts=[
                AttemptRecord("review-rewrite", route, "error", candidate_generation=1)
                for route in ("external-a", "external-b")
            ], routes=["external-a", "external-b"], response=None)
            def unexpected(route):
                self.fail("exhausted rewrite must not resolve another provider")
            fake.registry.get = unexpected
            summary = process_native_review_followup(root, source, root / "out", fake, operation="rewrite")
            self.assertEqual((summary["executed"], summary["unresolved"], summary["accepted"]), (0, 1, 0))
            self.assertEqual(read_translation_results(root / "out/accepted.json"), [])

    def test_judge_preserves_external_then_native_route_order(self):
        from game_l10n.errors import ConfigurationError
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import process_native_review_followup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def fail(result, provider, *args):
                calls.append(provider.name)
                result.attempts.append(AttemptRecord(
                    "review", provider.name, "error", candidate_generation=1))
                return None
            source, fake = self._followup_fixture(root, operation="judge", attempts=[],
                routes=["external-a", "native-second", "external-b"], response=fail)
            get = fake.registry.get
            def provider(route):
                if route == "native-second":
                    raise ConfigurationError("native route")
                return get(route)
            fake.registry.get = provider
            first = process_native_review_followup(root, source, root / "out", fake, operation="judge")
            self.assertEqual(first["executed"], 1)
            saved = (root / "out/results.json").read_bytes()
            with self.assertRaisesRegex(ValueError, "NATIVE_FALLBACK_REQUIRED"):
                process_native_review_followup(root, source, root / "out", fake, operation="judge")
            self.assertEqual(calls, ["external-a"])
            self.assertEqual((root / "out/results.json").read_bytes(), saved)

    def test_default_primary_uses_configured_role_and_current_generation_only(self):
        from game_l10n.models import AttemptRecord
        from game_l10n.native_review import _bound_result, _pending_segment, process_native_review_followup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, fake = self._followup_fixture(root, operation="judge", attempts=[],
                routes=["external-a"], response=None)
            base = read_segments(source)[0]
            result = _bound_result(base)
            for provider, generation in (("foreign-primary", 1), ("configured-primary", 0)):
                result.attempts = [AttemptRecord("review", provider, "error", candidate_generation=generation)]
                source.write_text(json.dumps(asdict(_pending_segment(base, result))) + "\n", encoding="utf-8")
                with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "PRIMARY_BARRIER_REQUIRED"):
                    process_native_review_followup(root, source, root / "wrong", fake,
                        operation="judge", deadline_epoch=0)
                if generation == 1:
                    explicit = process_native_review_followup(root, source, root / "explicit", fake,
                        operation="judge", deadline_epoch=0, primary_review_provider=provider)
                    self.assertEqual((explicit["executed"], explicit["unresolved"]), (0, 1))
            result.attempts = [
                AttemptRecord("review", "configured-primary", "rewrite", ordinal=4, candidate_generation=0),
                AttemptRecord("review", "configured-primary", "rewrite", candidate_generation=1),
            ]
            source.write_text(json.dumps(asdict(_pending_segment(base, result))) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "FALLBACK_NOT_FAILED"):
                process_native_review_followup(root, source, root / "limit", fake,
                    operation="judge", deadline_epoch=0, primary_review_limit=2)
            result.attempts[-1].ordinal = 2
            source.write_text(json.dumps(asdict(_pending_segment(base, result))) + "\n", encoding="utf-8")
            summary = process_native_review_followup(root, source, root / "limit", fake,
                operation="judge", deadline_epoch=0, primary_review_limit=2)
            self.assertEqual((summary["executed"], summary["unresolved"]), (0, 1))
            (root / "game-l10n.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "PRIMARY_BARRIER_REQUIRED"):
                process_native_review_followup(root, source, root / "unbound", fake,
                    operation="judge", deadline_epoch=0, primary_review_limit=2)
            explicit = process_native_review_followup(root, source, root / "bound", fake,
                operation="judge", deadline_epoch=0, primary_review_limit=2,
                primary_review_provider="configured-primary")
            self.assertEqual((explicit["executed"], explicit["unresolved"]), (0, 1))

    def test_independent_accepted_followup_stays_immutable_while_sibling_advances(self):
        from dataclasses import replace
        from game_l10n.models import AttemptRecord, ReviewVerdict, ReviewVerdictEnvelope
        from game_l10n.native_review import process_native_review_followup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def judge(result, provider, *args):
                calls.append((result.segment_id, provider.name))
                approved = result.segment_id == "ready" or provider.name == "external-b"
                result.attempts.append(AttemptRecord(
                    "review", provider.name, "approved" if approved else "error",
                    candidate_generation=result.candidate_generation))
                return ReviewVerdictEnvelope(ReviewVerdict.OK) if approved else None
            source, fake = self._followup_fixture(root, operation="judge", attempts=[],
                routes=["external-a", "external-b"], response=judge)
            segment = read_segments(source)[0]
            source.write_text("".join(
                json.dumps(asdict(replace(segment, id=target))) + "\n"
                for target in ("ready", "pending")
            ), encoding="utf-8")
            original = source.read_bytes()
            first = process_native_review_followup(root, source, root / "out", fake, operation="judge")
            self.assertEqual((first["executed"], first["accepted"], first["unresolved"]), (2, 1, 1))
            ready = read_translation_results(root / "out/results.json")[0].to_dict()
            second = process_native_review_followup(root, source, root / "out", fake, operation="judge")
            self.assertEqual((second["executed"], second["accepted"], second["unresolved"]), (1, 2, 0))
            self.assertEqual(read_translation_results(root / "out/results.json")[0].to_dict(), ready)
            self.assertEqual(calls.count(("ready", "external-a")), 1)
            self.assertEqual([provider for target, provider in calls if target == "pending"],
                             ["external-a", "external-b"])
            cache = (root / "out/results.json").read_bytes()
            third = process_native_review_followup(root, source, root / "out", fake, operation="judge")
            self.assertEqual((third["executed"], third["accepted"]), (0, 2))
            self.assertEqual((root / "out/results.json").read_bytes(), cache)
            self.assertEqual(source.read_bytes(), original)

    def test_review_followup_cli_does_not_require_a_time_ceiling(self) -> None:
        from game_l10n.cli import build_parser, cmd_process_native_review_followup
        from game_l10n.errors import ConfigurationError

        args = build_parser().parse_args([
            "process-native-review-followup", "--workspace", "w", "--input", "i",
            "--output", "o", "--operation", "judge",
        ])
        self.assertIsNone(args.max_elapsed_minutes)
        args.max_elapsed_minutes = 5.0
        with self.assertRaisesRegex(ConfigurationError, "E_REVIEW_FALLBACK_BUDGET_INVALID"):
            cmd_process_native_review_followup(args)

    def test_fallback_judge_approval_cannot_waive_target_language(self) -> None:
        from game_l10n.models import AttemptRecord, ContextPack, ReviewVerdictEnvelope, ReviewVerdict
        from game_l10n.native_review import _bound_result, _pending_segment, process_native_review_followup
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = review_segment("fixture-1", candidate="untranslated latin sentence")
            result = _bound_result(base)
            result.attempts.append(AttemptRecord("review", "native-sol", "error", candidate_generation=1))
            source = root / "fallback.jsonl"
            source.write_text(json.dumps(asdict(_pending_segment(base, result))) + "\n")
            def judge(result, *args):
                result.attempts.append(AttemptRecord("review", "deepseek", "approved", candidate_generation=result.candidate_generation))
                return ReviewVerdictEnvelope(ReviewVerdict.OK)
            fake = SimpleNamespace(
                registry=SimpleNamespace(get=lambda name: SimpleNamespace(name=name, fingerprint=lambda: {"name": name})),
                context_pack=ContextPack(), glossary=[], project=SimpleNamespace(target_language="ko"),
                config=OrchestratorConfig(["deepseek"], ["deepseek"], require_review=False,
                                          review_attempt_limits={"deepseek": 1}),
                _judge_with_refusal_retry=judge,
            )
            summary = process_native_review_followup(root, source, root / "out", fake,
                operation="judge", primary_review_provider="native-sol")
            # A cache written before the gate existed: the same row stored as approved.
            cache = root / "out" / "results.json"
            rows = json.loads(cache.read_text(encoding="utf-8"))
            rows["results"][0]["status"] = "reviewed"
            cache.write_text(json.dumps(rows), encoding="utf-8")
            resumed = process_native_review_followup(root, source, root / "out", fake,
                operation="judge", primary_review_provider="native-sol")
        self.assertEqual(summary["accepted"], 0)
        self.assertEqual(summary["rewrite_pending"], 1)
        self.assertEqual((resumed["executed"], resumed["accepted"], resumed["rewrite_pending"]), (0, 0, 1))

    def test_rewrite_issue_codes_keep_judge_and_validator_findings(self) -> None:
        from game_l10n.models import ValidationIssue
        from game_l10n.review_selection import rewrite_issue_codes

        integrity = [ValidationIssue("glossary-missing", "x"), ValidationIssue("ui-length-limit", "y")]
        self.assertEqual(
            rewrite_issue_codes(["meaning", "omission"], integrity, language_risk=True),
            ["terminology", "length_risk", "fluency", "meaning", "omission"],
        )
        # The cap applies to judge codes alone, never to hard-gate findings.
        self.assertEqual(
            rewrite_issue_codes(
                ["meaning", "voice", "register", "literalism"],
                [ValidationIssue("glossary-missing", "x")], language_risk=True,
            ),
            [
                "terminology",
                "fluency",
                "meaning",
                "voice",
                "register",
                "literalism",
            ],
        )
        self.assertEqual(rewrite_issue_codes(["voice"], language_risk=True), ["fluency", "voice"])
        self.assertEqual(rewrite_issue_codes(), ["formatting"])

    def test_native_triage_ok_escalates_cue_and_fallback_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            segments = [review_segment(f"fixture-{i}") for i in range(3)]
            segments[2].metadata["_parr0t_candidate"]["provider"] = "fixture-fallback"
            source.write_text("\n".join(json.dumps(asdict(s)) for s in segments) + "\n")
            segments = read_segments(source)
            tasks, results = root / "tasks", root / "results"
            prepare_native_tasks(root, source, segments, tasks, pass_kind="review")
            results.mkdir()
            write_semantic_result_rows(
                results / "task-0001.jsonl",
                [
                    {
                        "target_id": s.id,
                        "status": "success",
                        "text": '{"verdict":"ok","issues":[]}',
                    }
                    for s in segments
                ],
            )
            import_native_wave(root, tasks / "manifest.json", results, 1)
            summary = collect_native_review(
                root, source, tasks / "manifest.json", root / "collected", tier="triage",
                sample_percent=0, reference_cues=({"evidence_line_ids": ["fixture-1"]},),
                primary_translator="fixture",
            )
            pending = {s.id: s for s in read_segments(root / "collected/strong-input.jsonl")}
        self.assertEqual(summary["accepted"], 1)
        self.assertEqual(sorted(pending), ["fixture-1", "fixture-2"])
