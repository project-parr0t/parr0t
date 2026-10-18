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
