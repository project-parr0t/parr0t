from __future__ import annotations

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
