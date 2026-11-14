from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import asdict
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


    def test_malformed_final_cache_row_is_a_miss_without_losing_healthy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.sqlite3"
            store = ResultStore(path)
            result = TranslationResult("fixture", "SOURCE", text="TARGET", status="reviewed")
            store.put("healthy", result)
            store.put("damaged", result)
            payload = asdict(result)
            variants = ["{broken", "null", "[]", json.dumps({}), *(
                json.dumps({**payload, **changes}) for changes in (
                    {"attempts": None}, {"attempts": [None]}, {"text": []},
                    {"status": []}, {"metadata": []}, {"unexpected": True},
                )
            )]
            for value in variants:
                with self.subTest(payload=value):
                    with closing(sqlite3.connect(path)) as database:
                        database.execute("UPDATE results SET payload=? WHERE cache_key='damaged'", (value,))
                        database.commit()
                    self.assertIsNone(store.get("damaged"))
                    self.assertEqual(store.get("healthy").text, "TARGET")

    def test_cached_review_cannot_certify_changed_text_or_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.sqlite3"
            store = ResultStore(path)
            result = TranslationResult("fixture", "SOURCE", text="TARGET", status="reviewed",
                                       candidate_generation=1, candidate_digest=stable_hash("TARGET"))
            payload = asdict(result)
            for changes in ({"text": "CHANGED"}, {"segment_id": "different"}):
                with self.subTest(changes=changes):
                    store.put("fixture", result)
                    with closing(sqlite3.connect(path)) as database:
                        database.execute("UPDATE results SET payload=? WHERE cache_key='fixture'",
                                         (json.dumps({**payload, **changes}),))
                        database.commit()
                    self.assertIsNone(store.get("fixture"))
            store.put("fixture", result)
            self.assertEqual(store.get("fixture").candidate_digest, stable_hash("TARGET"))
            store.put("legacy", TranslationResult("fixture", "SOURCE", text="TARGET", status="reviewed"))
            self.assertEqual(store.get("legacy").text, "TARGET")
            store.put("preserved", TranslationResult("fixture", "SOURCE", text="SOURCE",
                      status="source_preserved", candidate_digest=stable_hash("REJECTED_CANDIDATE")))
            self.assertEqual(store.get("preserved").text, "SOURCE")

    def test_malformed_staged_rows_do_not_discard_completed_peers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.sqlite3"
            store = ResultStore(path)
            result = TranslationResult("fixture", "SOURCE", candidate_digest=stable_hash("TARGET"))
            store.put_staged_candidate("batch", 0, result, "TARGET")
            store.put_staged_candidate("batch", 1, result, "TARGET")
            payload = asdict(result)
            for value in ("null", "[]", json.dumps({**payload, "attempts": None})):
                with self.subTest(payload=value):
                    with closing(sqlite3.connect(path)) as database:
                        database.execute("UPDATE staged_candidates SET result_payload=? WHERE segment_index=0", (value,))
                        database.commit()
                    staged = store.get_staged_candidates("batch")
                    self.assertEqual(set(staged), {1})
                    self.assertEqual(staged[1][1], "TARGET")
            with closing(sqlite3.connect(path)) as database:
                database.execute("UPDATE staged_candidates SET segment_index=?, result_payload=? WHERE segment_index=0",
                                 (0.5, json.dumps(payload)))
                database.commit()
            self.assertEqual(set(store.get_staged_candidates("batch")), {1})


if __name__ == "__main__":
    unittest.main()
