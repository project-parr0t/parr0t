from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.formats import read_segments
from game_l10n.models import Segment
from game_l10n.native_followup import (
    CANDIDATE_METADATA_KEY,
    collect_native_followup,
    prepare_native_followup,
)
from game_l10n.store import stable_hash
from game_l10n.translation_exchange import REQUESTS_NAME, SUCCESS_NAME


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class NativeFollowupTests(unittest.TestCase):
    def test_collect_partial_translation_successes_without_restarting_review(self) -> None:
        from game_l10n.native_followup import collect_native_translation_candidates
        from game_l10n.native_pass import import_native_wave, prepare_native_tasks
        from tests import write_semantic_result_rows

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "source.jsonl"
            segments = [Segment(key, "Source") for key in ("ready", "next", "failed")]
            _write_jsonl(source, [asdict(segment) for segment in segments])
            segments = read_segments(source)
            tasks, results = root / ".game-l10n/native/tasks", root / "results"
            prepare_native_tasks(
                root, source, segments, tasks, min_records=1, max_records=1, max_workers=1,
                route_binding={"provider": "native-fixture", "model": "fixture/exact", "attempt": 1},
            )
            results.mkdir()
            manifest = tasks / "manifest.json"
            binding = json.loads(manifest.read_text(encoding="utf-8"))
            for i, segment in enumerate(segments, 1):
                success = segment.id != "failed"
                write_semantic_result_rows(results / f"task-{i:04}.jsonl", [{
                    "target_id": segment.id, "status": "success" if success else "error",
                    "text": "Translation" if success else "", "issues": [],
                }], manifest=binding)
            import_native_wave(root, manifest, results, 1)
            first = root / "prefix.jsonl"
            summary = collect_native_translation_candidates(root, source, manifest, first)
            self.assertEqual((summary["new_candidates"], summary["missing"]), (1, 2))
            self.assertEqual([item.id for item in read_segments(first)], ["ready"])
            pending = root / "unstarted.jsonl"
            projected = collect_native_translation_candidates(
                root, source, manifest, root / "already-reviewed.jsonl", previous=(first,), pending_output=pending,
            )
            self.assertEqual((projected["new_candidates"], projected["pending_targets"]), (0, 2))
            self.assertEqual([item.id for item in read_segments(pending)], ["next", "failed"])
            import_native_wave(root, manifest, results, 2)
            import_native_wave(root, manifest, results, 3)
            second = root / "tail.jsonl"
            failed = root / "failed-targets.jsonl"
            summary = collect_native_translation_candidates(
                root, source, manifest, second, previous=(first,), pending_output=failed,
            )
            self.assertEqual([item.id for item in read_segments(failed)], ["failed"])
            self.assertTrue(summary["complete"])
            self.assertEqual([item.id for item in read_segments(second)], ["next"])
            stale = asdict(read_segments(first)[0])
            stale["metadata"][CANDIDATE_METADATA_KEY]["text"] = "Edited"
            _write_jsonl(root / "stale.jsonl", [stale])
            with self.assertRaisesRegex(ValueError, "E_NATIVE_TRANSLATION_PREVIOUS_CANDIDATE_INVALID"):
                collect_native_translation_candidates(root, source, manifest, root / "invalid.jsonl",
                                                      previous=(root / "stale.jsonl",))

    def test_frozen_subset_keeps_root_lineage_and_historical_success_policy(self) -> None:
        from game_l10n.content_source_policy import (
            content_source_policy, create_content_source_grant, direct_model_provenance,
        )
        from game_l10n.store import stable_hash_sequence
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("old-success", "S"), Segment("new-failure", "S")]
            source, primary = root / "cohort-source.jsonl", root / "cohort-results.jsonl"
            _write_jsonl(source, [asdict(segment) for segment in segments])
            for scope in ("historical", "active"):
                create_content_source_grant(
                    root, authority="explicit_user", scope_id=scope,
                    task_kinds=["translation"], routes=["native-pass:translation"],
                    external_sources=[f"synthetic://fixture/{scope}"], user_confirmed=True,
                )
            old_policy = content_source_policy(
                root, task_kind="translation", route="native-pass:translation", scope_id="historical"
            )
            active_policy = content_source_policy(
                root, task_kind="translation", route="native-pass:translation", scope_id="active"
            )
            plan = [{"provider": "native-configured", "model": "exact-model", "max_retries": 0}]
            lineage = {"source_fingerprint": "different-root-fingerprint",
                       "content_source_policy_digest": old_policy["policy_digest"]}
            cohort = {"schema": "parr0t.native-translation-retry-cohort.v1",
                      "source_fingerprint": stable_hash_sequence(asdict(segment) for segment in segments)}
            _write_jsonl(primary, [
                {"target_id": "old-success", "status": "success", "text": "T",
                 "candidate_generation": 1, "candidate_digest": stable_hash("T"),
                 "provider": "native-configured",
                 "content_provenance": direct_model_provenance(old_policy, "exact-model", route="native-configured")},
                {"target_id": "new-failure", "status": "error", "text": "", "provider": "native-configured"},
            ])
            verified = {"cohort_lineage": cohort, "content_source_policies": [old_policy, active_policy],
                        "primary_lineage": lineage, "route_plan": plan}
            with patch("game_l10n.native_translation_retry.verify_native_translation_retry_cohort",
                       return_value=verified) as verify:
                result = prepare_native_followup(
                    root, source, segments, primary, root / "followup", exchange_prefix="cohort",
                    content_source_policy=active_policy, retry_lineage=lineage,
                    retry_route_plan=plan, cohort_lineage=cohort,
                )
                verify.assert_called_once_with(root, cohort, source_path=source, results_path=primary)
            self.assertEqual((result["candidate_count"], result["fallback_count"]), (1, 1))
            candidate = read_segments(root / result["primary_candidates"])[0].metadata[CANDIDATE_METADATA_KEY]
            self.assertEqual(candidate["content_provenance"]["policy_digest"], old_policy["policy_digest"])
            manifest = json.loads((root / "followup/followup.json").read_text())
            self.assertEqual(manifest["retry_lineage"], lineage)
            exchange = json.loads((root / result["fallback_bundles"][0] / "exchange-manifest.json").read_text())
            self.assertEqual(exchange["retry_lineage"], lineage)
            self.assertEqual(exchange["content_source_policy_history"], [old_policy, active_policy])

    def test_valid_cohorts_progress_while_a_sibling_bundle_requires_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment(str(i), "SOURCE") for i in range(3)]
            source, primary = root / "source.jsonl", root / "primary.jsonl"
            _write_jsonl(source, [asdict(s) for s in segments])
            _write_jsonl(primary, [
                {"target_id": s.id, "status": "success" if i == 0 else "error",
                 "text": "TARGET" if i == 0 else "", "provider": "configured", "issues": [],
                 **({"candidate_generation": 1, "candidate_digest": stable_hash("TARGET")} if i == 0 else {})}
                for i, s in enumerate(segments)
            ])
            prepared = prepare_native_followup(
                root, source, segments, primary, root / "followup",
                exchange_prefix="cohorts", exchange_max_records=1,
            )
            first, second = [root / p for p in prepared["fallback_bundles"]]
            request = json.loads((first / REQUESTS_NAME).read_text())
            _write_jsonl(first / SUCCESS_NAME, [{
                "target_id": request["target_id"], "status": "success", "text": "FALLBACK",
                "route": "configured", "attempt": 1,
                "source_sha256": request["source_sha256"],
                "context_sha256": request["context_sha256"],
            }])
            (second / REQUESTS_NAME).write_text("{}\n")
            summary = collect_native_followup(
                root, root / "followup", segments, root / "review.jsonl", root / "unresolved.jsonl"
            )
            self.assertTrue(summary["review_ready"])
            self.assertFalse(summary["translation_barrier_complete"])
            self.assertEqual(summary["candidate_count"], 2)
            self.assertEqual(summary["blocked_bundles"], [prepared["fallback_bundles"][1]])
            self.assertEqual([s.id for s in read_segments(root / "review.jsonl")], ["0", "1"])
            self.assertEqual([s.id for s in read_segments(root / "unresolved.jsonl")], ["2"])

    def test_exchange_shard_size_is_configurable_above_previous_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("line", "S")]
            source, primary = root / "source.jsonl", root / "primary.jsonl"
            _write_jsonl(source, [asdict(s) for s in segments])
            _write_jsonl(primary, [{"target_id": "line", "status": "error", "text": "",
                                   "provider": "configured", "issues": []}])
            summary = prepare_native_followup(
                root, source, segments, primary, root / "followup",
                exchange_prefix="configured", exchange_max_records=4096,
            )
            self.assertEqual(len(summary["fallback_bundles"]), 1)
            manifest = json.loads((root / "followup/followup.json").read_text())
            self.assertEqual(manifest["exchange_max_records"], 4096)

    def test_single_exchange_collects_only_bound_first_candidates(self):
        from dataclasses import replace
        from game_l10n.native_followup import collect_translation_exchange_candidates
        from game_l10n.translation_exchange import prepare_translation_exchange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = "T\u0085line\u2028paragraph\u2029end"
            segments = [Segment("a", "S\u0085line\u2028paragraph\u2029end", scene="one"),
                        Segment("b", "S", scene="two")]
            bundle = root / "exchange"
            prepare_translation_exchange(root, segments, bundle, exchange_id="single-fixture",
                mode="fallback", source_queue="translation_fallback_pending")
            request = json.loads((bundle / REQUESTS_NAME).read_text().split("\n")[0])
            _write_jsonl(bundle / SUCCESS_NAME, [{
                "target_id": "a", "status": "success", "text": text, "route": "fallback",
                "attempt": 1, "source_sha256": request["source_sha256"],
                "context_sha256": request["context_sha256"],
            }])
            args = (root, segments, bundle, root / "review.jsonl", root / "unresolved.jsonl")
            result = collect_translation_exchange_candidates(*args)
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["unresolved_count"], 1)
            bound = read_segments(root / "review.jsonl")[0]
            self.assertEqual(bound.metadata[CANDIDATE_METADATA_KEY]["generation"], 1)
            self.assertEqual(bound.metadata[CANDIDATE_METADATA_KEY]["digest"], stable_hash(text))
            self.assertEqual(bound.metadata[CANDIDATE_METADATA_KEY]["text"], text)
            self.assertEqual(bound.source, segments[0].source)
            self.assertEqual(read_segments(root / "unresolved.jsonl")[0].id, "b")
            with self.assertRaisesRegex(ValueError, "context is stale"):
                collect_translation_exchange_candidates(root,
                    [replace(segments[0], scene="changed"), segments[1]], bundle,
                    root / "stale-review.jsonl", root / "stale-unresolved.jsonl")
            self.assertFalse((root / "stale-review.jsonl").exists())
            self.assertEqual(collect_translation_exchange_candidates(*args), result)

    def test_single_exchange_never_resets_an_existing_candidate_generation(self):
        from game_l10n.native_followup import collect_translation_exchange_candidates
        from game_l10n.translation_exchange import prepare_translation_exchange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = Segment("a", "S", metadata={CANDIDATE_METADATA_KEY: {
                "text": "T", "generation": 7, "digest": stable_hash("T"), "provider": "fixture"}})
            bundle = root / "exchange"
            prepare_translation_exchange(root, [segment], bundle, exchange_id="existing-fixture",
                mode="fallback", source_queue="translation_fallback_pending")
            with self.assertRaisesRegex(ValueError, "generation one"):
                collect_translation_exchange_candidates(root, [segment], bundle,
                    root / "review.jsonl", root / "unresolved.jsonl")

    def test_primary_failures_become_fixed_exchanges_and_successes_join_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            segments = [
                Segment("line-1", "SOURCE_1", speaker="A", scene="intro"),
                Segment("line-2", "SOURCE_2", speaker="B", scene="intro"),
                Segment("line-3", "SOURCE_3", speaker="A", scene="intro"),
            ]
            _write_jsonl(source, [asdict(segment) for segment in segments])
            primary = root / "primary.jsonl"
            _write_jsonl(
                primary,
                [
                    {
                        "target_id": "line-1",
                        "status": "success",
                        "text": "TARGET_1",
                        "provider": "native",
                        "issues": [],
                        "candidate_generation": 1,
                        "candidate_digest": stable_hash("TARGET_1"),
                    },
                    {
                        "target_id": "line-2",
                        "status": "error",
                        "text": "",
                        "provider": "native",
                        "issues": ["worker_ended_without_result"],
                    },
                    {
                        "target_id": "line-3",
                        "status": "refused",
                        "text": "",
                        "provider": "native",
                        "issues": [],
                    },
                ],
            )
            followup = root / "followup"
            prepared = prepare_native_followup(
                root,
                source,
                read_segments(source),
                primary,
                followup,
                exchange_prefix="fixture-fallback",
            )
            cached = prepare_native_followup(
                root,
                source,
                read_segments(source),
                primary,
                followup,
                exchange_prefix="fixture-fallback",
            )

            bundle = root / prepared["fallback_bundles"][0]
            requests = [
                json.loads(line)
                for line in (bundle / REQUESTS_NAME).read_text(encoding="utf-8").splitlines()
            ]
            _write_jsonl(
                bundle / SUCCESS_NAME,
                [
                    {
                        "target_id": row["target_id"],
                        "status": "success",
                        "text": f"TARGET_{index + 2}",
                        "route": "fallback",
                        "attempt": 1,
                        "source_sha256": row["source_sha256"],
                        "context_sha256": row["context_sha256"],
                    }
                    for index, row in enumerate(requests)
                ],
            )
            collected = collect_native_followup(
                root,
                followup,
                read_segments(source),
                root / "review.jsonl",
                root / "unresolved.jsonl",
            )
            review = read_segments(root / "review.jsonl")

        self.assertEqual(prepared["candidate_count"], 1)
        self.assertEqual(prepared["fallback_count"], 2)
        self.assertTrue(cached["cached"])
        self.assertEqual(collected["candidate_count"], 3)
        self.assertEqual(collected["unresolved_count"], 0)
        self.assertTrue(collected["translation_barrier_complete"])
        self.assertEqual([item.id for item in review], ["line-1", "line-2", "line-3"])
        for item in review:
            self.assertIn(CANDIDATE_METADATA_KEY, item.metadata, item.id)
        self.assertEqual(
            [item.metadata[CANDIDATE_METADATA_KEY]["text"] for item in review],
            ["TARGET_1", "TARGET_2", "TARGET_3"],
        )

    def test_followup_rejects_incomplete_or_reordered_primary_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            segments = [Segment("line-1", "S1"), Segment("line-2", "S2")]
            _write_jsonl(source, [asdict(segment) for segment in segments])
            primary = root / "primary.jsonl"
            _write_jsonl(
                primary,
                [
                    {
                        "target_id": "line-2",
                        "status": "error",
                        "text": "",
                        "provider": "native",
                        "issues": [],
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "every source target"):
                prepare_native_followup(
                    root,
                    source,
                    segments,
                    primary,
                    root / "followup",
                    exchange_prefix="fixture",
                )


if __name__ == "__main__":
    unittest.main()
