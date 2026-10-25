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
from game_l10n.native_pass import import_native_wave, prepare_native_tasks
from game_l10n.native_review import collect_native_review
from tests.test_native_pass import review_segment


class NativeReviewTests(unittest.TestCase):
    def test_delivery_includes_unreviewed_without_promoting_it_and_only_omits_failures(self):
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
            args = (root, source, root / "approved.json", [root / "pending.jsonl"],
                    root / "failures.jsonl", root / "delivery.json")
            with patch("game_l10n.native_review.require_inventory_attestation"):
                with self.assertRaisesRegex(ValueError, "AUTHORIZATION_REQUIRED"):
                    materialize_delivery_candidates(*args)
                summary = materialize_delivery_candidates(*args, include_unreviewed=True)
                cached = materialize_delivery_candidates(*args, include_unreviewed=True)
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
            rows = read_translation_results(root / "delivery.json")
            self.assertEqual([r.status for r in rows], ["reviewed", "accepted_unreviewed"])
            self.assertEqual(rows[1].attempts[-1].status, "rewrite")
            self.assertEqual(rows[1].acceptance_authority, "user")
            self.assertEqual(rows[1].candidate_generation, 1)
            self.assertEqual(rows[1].metadata["delivery_review_status"], "not_approved")
            (root / "wrong.jsonl").write_text("")
            with patch("game_l10n.native_review.require_inventory_attestation"):
                with self.assertRaisesRegex(ValueError, "QUEUE_MISMATCH"):
                    materialize_delivery_candidates(root, source, root / "approved.json",
                        [root / "pending.jsonl"], root / "wrong.jsonl", root / "invalid.json",
                        include_unreviewed=True)
            self.assertFalse((root / "invalid.json").exists())

    def test_delivery_source_preserves_generic_integrity_rejections(self):
        from game_l10n.formats import write_results
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

            self.assertEqual(summary["included"], 0)
            self.assertEqual(summary["integrity_rejected"], 1)
            self.assertEqual(summary["source_preserved_total"], 1)
            self.assertEqual(read_translation_results(root / "delivery.json"), [])
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
            for key in ["changed", "unchanged", "failed"]:
                base = review_segment(key)
                result = _bound_result(base)
                result.attempts = [
                    AttemptRecord("review-triage", "native-luna", "rewrite", candidate_generation=1),
                    AttemptRecord("review", "native-sol", "rewrite", candidate_generation=1),
                ]
                result.metadata["rewrite_issues"] = ["voice"]
                segments.append(_pending_segment(base, result))
            source = root / "source.jsonl"
            source.write_text("\n".join(json.dumps(asdict(s)) for s in segments) + "\n")
            segments = read_segments(source)
            tasks, results = root / "tasks", root / "results"
            prepare_native_tasks(root, source, segments, tasks, pass_kind="rewrite")
            results.mkdir()
            rows = [{"target_id": s.id, "status": "success" if i < 2 else "error",
                     "text": ["U", "T", ""][i], "issues": []}
                    for i, s in enumerate(segments)]
            (results / "task-0001.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n")
            import_native_wave(root, tasks / "manifest.json", results, 1)
            args = (root, source, tasks / "manifest.json", root / "collected")
            summary = collect_native_rewrite(*args)
            self.assertEqual(summary["accepted"], 0)
            self.assertEqual(summary["confirmation_pending"], 2)
            self.assertEqual(summary["unresolved"], 1)
            confirmation = read_segments(root / "collected/confirmation-input.jsonl")
            self.assertEqual([_bound_result(s).candidate_generation for s in confirmation], [2, 1])
            self.assertTrue(all(len(_bound_result(s).attempts) == 3 for s in confirmation))
            pending = _bound_result(read_segments(root / "collected/unresolved-input.jsonl")[0])
            self.assertEqual(pending.text, "T")
            self.assertEqual(pending.candidate_generation, 1)
            self.assertEqual(pending.attempts[-1].status, "error")
            self.assertEqual(collect_native_rewrite(*args), summary)
            self.assertTrue(all(r.status != "reviewed" and not r.acceptance_authority
                for r in read_translation_results(root / "collected/results.json")))
            # A rewriter's output cannot be accepted without a fresh judge.
            from game_l10n.native_review import collect_reviewed_candidates
            with patch("game_l10n.native_review.require_inventory_attestation"):
                with self.assertRaisesRegex(ValueError, "ACCEPTED_RESULT_INVALID"):
                    collect_reviewed_candidates(root, source,
                        [root / "collected/results.json"], root / "invalid.json")

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
            (results / "task-0001.jsonl").write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n")
            import_native_wave(root, tasks / "manifest.json", results, 1)
            summary = collect_native_review(root, source, tasks / "manifest.json",
                                            root / "collected", tier="triage", sample_percent=0)
            self.assertEqual(summary["accepted"], 1)
            self.assertEqual(summary["strong_pending"], 3)
            self.assertEqual(summary["invalid_verdicts"], 1)
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

    def test_sample_and_high_risk_are_not_exempted_by_ok(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = review_segment("fixture-1")
            source = root / "source.jsonl"
            source.write_text(json.dumps(asdict(segment)) + "\n")
            tasks, results = root / "tasks", root / "results"
            prepare_native_tasks(root, source, [segment], tasks, pass_kind="review")
            results.mkdir()
            (results / "task-0001.jsonl").write_text(json.dumps({
                "target_id": segment.id, "status": "success", "text": "OK",
            }) + "\n")
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
                result.attempts.append(AttemptRecord("review", "deepseek", "approved", candidate_generation=1))
                return ReviewVerdictEnvelope(ReviewVerdict.OK)
            fake = SimpleNamespace(
                registry=SimpleNamespace(get=lambda name: SimpleNamespace(fingerprint=lambda: {"name": name})),
                context_pack=ContextPack(), glossary=[],
                config=SimpleNamespace(extra_protected_patterns=[], max_length_ratio=4, review_max_rounds=2),
                _judge_with_refusal_retry=judge,
            )
            first = process_native_review_followup(root, source, root / "out", fake,
                                                   operation="judge", deadline_epoch=time.time()+60)
            second = process_native_review_followup(root, source, root / "out", fake,
                                                    operation="judge", deadline_epoch=time.time()+60)
            self.assertEqual(first["accepted"], 1)
            self.assertEqual(second["executed"], 0)
            self.assertEqual(calls, ["judge"])
            result.attempts[-1].status = "rewrite"
            result.metadata["rewrite_issues"] = ["voice"]
            source = root / "rewrite.jsonl"
            source.write_text(json.dumps(asdict(_pending_segment(base, result))) + "\n")
            def rewrite(result, *args):
                result.attempts.append(AttemptRecord("review-rewrite", "deepseek", "success", candidate_generation=1))
                return AgentEnvelope(AgentStatus.SUCCESS, "REVISED")
            fake._rewrite_with_refusal_retry = rewrite
            fake._set_candidate = LocalizationOrchestrator._set_candidate
            summary = process_native_review_followup(root, source, root / "rewrite-out", fake,
                                                     operation="rewrite", deadline_epoch=time.time()+60)
            self.assertEqual(summary["accepted"], 0)
            self.assertEqual(summary["confirmation_pending"], 1)
            confirmation = read_segments(root / "rewrite-out/confirmation-input.jsonl")[0]
            binding = confirmation.metadata["_parr0t_candidate"]
            self.assertEqual(binding["generation"], 2)
            self.assertEqual(binding["digest"], stable_hash("REVISED"))
