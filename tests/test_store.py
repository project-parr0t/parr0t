from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from game_l10n.models import TranslationResult
from game_l10n.store import ResultStore, stable_hash


class ResultStoreTests(unittest.TestCase):
    def test_corrupt_staged_candidate_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.sqlite3"
            store = ResultStore(path)
            candidate = "TARGET"
            result = TranslationResult(
                segment_id="one",
                source="SOURCE",
                candidate_digest=stable_hash(candidate),
            )
            store.put_staged_candidate("batch", 0, result, candidate)
            with closing(sqlite3.connect(path)) as database:
                database.execute(
                    "UPDATE staged_candidates SET result_payload = ? WHERE batch_key = ?",
                    ("{broken", "batch"),
                )
                database.commit()

            self.assertEqual(store.get_staged_candidates("batch"), {})


if __name__ == "__main__":
    unittest.main()
