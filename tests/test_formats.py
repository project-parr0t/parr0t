from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from game_l10n.formats import read_segments
from game_l10n.store import stable_hash, stable_hash_sequence


class FormatTests(unittest.TestCase):
    def test_streaming_sequence_hash_matches_canonical_list_hash(self) -> None:
        values = [
            {"id": "a", "text": "\u2603"},
            {"id": "b", "nested": [1, 2]},
        ]
        self.assertEqual(stable_hash_sequence(iter(values)), stable_hash(values))

    def test_prior_and_correction_results_preserve_unicode_record_contents(self) -> None:
        from game_l10n.formats import read_existing_translations
        from game_l10n.corrections import _load_results
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.jsonl"
            source = "SOURCE\u0085next\u2028line\u2029paragraph"
            text = "TARGET\u0085next\u2028line\u2029paragraph"
            rows = [{"segment_id": "fixture", "source": source, "text": text},
                    {"segment_id": "peer", "source": "S", "text": "T"}]
            path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                            encoding="utf-8")
            self.assertEqual(read_existing_translations(path), {
                "fixture": {"source": source, "text": text},
                "peer": {"source": "S", "text": "T"},
            })
            corrected = _load_results(path)
            self.assertEqual([(row.segment_id, row.source, row.text) for row in corrected],
                             [("fixture", source, text), ("peer", "S", "T")])

    def test_lossless_results_preserve_review_authority_and_unicode_boundaries(self) -> None:
        from game_l10n.formats import read_translation_results, write_results
        from game_l10n.models import AttemptRecord, TranslationResult
        text = "TARGET\u0085next\u2028line\u2029paragraph"
        row = TranslationResult(
            "fixture", "SOURCE", text, "reviewed",
            reviewer="native-exact-review",
            candidate_generation=3, candidate_digest=stable_hash(text),
            acceptance_authority="reviewer", review_outcome="approved",
            attempts=[AttemptRecord(
                "review", "native-exact-review", "approved",
                model="fixture/model:variant@upstream:high", candidate_generation=3,
            )],
        )
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".json", ".jsonl"):
                with self.subTest(suffix=suffix):
                    path = Path(directory) / ("results" + suffix)
                    write_results(path, [row])
                    loaded = read_translation_results(path)
                    self.assertEqual(
                        (loaded[0].text, loaded[0].candidate_generation,
                         loaded[0].acceptance_authority, loaded[0].review_outcome),
                        (text, 3, "reviewer", "approved"),
                    )
                    self.assertEqual(loaded[0].attempts[0].model,
                                     "fixture/model:variant@upstream:high")
                    self.assertEqual(loaded[0].attempts[0].candidate_generation, 3)
                    self.assertEqual(loaded[0].candidate_digest, stable_hash(text))

    def test_neighbor_enrichment_preserves_explicit_context_across_formats(self) -> None:
        rows = [
            {"id": "a", "source": "A", "previous": "external-before", "next": "external-after", "extra": "first"},
            {"id": "b", "source": "B", "previous": "", "next": "", "extra": "middle"},
            {"id": "c", "source": "C", "previous": "explicit-before", "next": "", "extra": "last"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            for suffix in (".json", ".jsonl", ".csv"):
                with self.subTest(format=suffix):
                    path = Path(directory) / f"segments{suffix}"
                    if suffix == ".json":
                        path.write_text(json.dumps(rows), encoding="utf-8")
                    elif suffix == ".jsonl":
                        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
                    else:
                        with path.open("w", encoding="utf-8", newline="") as handle:
                            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                            writer.writeheader()
                            writer.writerows(rows)

                    segments = read_segments(path)

                    self.assertEqual(
                        [(segment.previous, segment.next) for segment in segments],
                        [("external-before", "external-after"), ("A", "C"), ("explicit-before", "")],
                    )
                    self.assertEqual(
                        [segment.metadata for segment in segments],
                        [{"extra": "first"}, {"extra": "middle"}, {"extra": "last"}],
                    )

    def test_jsonl_segments_are_streamed_without_path_read_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "segments.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for index in range(1000):
                    handle.write(
                        json.dumps({"id": f"fixture-{index}", "source": f"SOURCE_{index}"})
                        + "\n"
                    )

            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("JSONL loader materialized the complete file"),
            ):
                segments = read_segments(path)

        self.assertEqual(len(segments), 1000)
        self.assertEqual(segments[0].next, "SOURCE_1")
        self.assertEqual(segments[-1].previous, "SOURCE_998")


if __name__ == "__main__":
    unittest.main()
