from __future__ import annotations

import io
import json
import unittest

from game_l10n.progress_ui import ProcessingProgress, live_processing_ui


class ProgressUITests(unittest.TestCase):
    def test_progress_state_accepts_only_categorical_counts(self) -> None:
        progress = ProcessingProgress(operation="translation_pipeline", total_records=10)
        progress.apply_event(
            {
                "stage": "translation",
                "completed_records": 4,
                "translation_completed": 6,
                "fallback_pending": 2,
                "attempts": 8,
            }
        )
        snapshot = progress.snapshot()

        self.assertEqual(snapshot["statistics"]["pending_records"], 6)
        self.assertEqual(snapshot["statistics"]["completion_percent"], 40.0)
        self.assertEqual(
            set(snapshot),
            {
                "schema",
                "status",
                "operation",
                "stage",
                "started_at_utc",
                "updated_at_utc",
                "statistics",
                "elapsed_seconds",
            },
        )
        self.assertNotIn("current_text", snapshot["statistics"])
        self.assertNotIn("target_id", snapshot["statistics"])
        with self.assertRaisesRegex(ValueError, "non-statistical"):
            progress.apply_event({"stage": "translation", "current_text": "forbidden"})

    def test_live_ui_uses_os_selected_port_and_stops_with_context(self) -> None:
        stream = io.StringIO()
        with live_processing_ui(
            operation="fallback_processing", total_records=3, stream=stream
        ) as progress:
            announcement = json.loads(stream.getvalue())
            url, token = announcement["url"].split("#", 1)
            port = int(url.rsplit(":", 1)[1].rstrip("/"))
            self.assertGreater(port, 0)
            self.assertTrue(token)
            progress.update(stage="translation_fallback", completed_records=1, attempts=1)
            payload = progress.snapshot()
            second_stream = io.StringIO()
            with live_processing_ui(
                operation="review_pipeline", total_records=1, stream=second_stream
            ):
                second_url = json.loads(second_stream.getvalue())["url"].split("#", 1)[0]
                second_port = int(second_url.rsplit(":", 1)[1].rstrip("/"))
                self.assertNotEqual(port, second_port)

        self.assertEqual(announcement["content_policy"], "aggregate_statistics_only")
        self.assertEqual(payload["statistics"]["completed_records"], 1)
        self.assertEqual(payload["statistics"]["total_records"], 3)


if __name__ == "__main__":
    unittest.main()
