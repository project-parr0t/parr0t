from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

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
    def test_single_exchange_collects_only_bound_first_candidates(self):
        from dataclasses import replace
        from game_l10n.native_followup import collect_translation_exchange_candidates
        from game_l10n.translation_exchange import prepare_translation_exchange
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("a", "S", scene="one"), Segment("b", "S", scene="two")]
            bundle = root / "exchange"
            prepare_translation_exchange(root, segments, bundle, exchange_id="single-fixture",
                mode="fallback", source_queue="translation_fallback_pending")
            request = json.loads((bundle / REQUESTS_NAME).read_text().splitlines()[0])
            _write_jsonl(bundle / SUCCESS_NAME, [{
                "target_id": "a", "status": "success", "text": "T", "route": "fallback",
                "attempt": 1, "source_sha256": request["source_sha256"],
                "context_sha256": request["context_sha256"],
            }])
            args = (root, segments, bundle, root / "review.jsonl", root / "unresolved.jsonl")
            result = collect_translation_exchange_candidates(*args)
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["unresolved_count"], 1)
            bound = read_segments(root / "review.jsonl")[0]
            self.assertEqual(bound.metadata[CANDIDATE_METADATA_KEY]["generation"], 1)
            self.assertEqual(bound.metadata[CANDIDATE_METADATA_KEY]["digest"], stable_hash("T"))
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
