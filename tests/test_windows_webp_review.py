from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import ConfigurationError
from game_l10n.windows_webp_review import SCHEMA, _review_counts
from tests.test_image_localization import rgba_png


class WindowsWebpReviewTests(unittest.TestCase):
    def test_pending_and_rejected_reviews_do_not_count_as_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            png_root = work / "fixture-review-pairs"
            png_root.mkdir()
            fixtures = [("approved", (1, 1)), ("revise", (3, 2)), ("unresolved", (7, 4))]
            assets, pairs, records = [], [], []
            original = rgba_png()
            def digest(body):
                return hashlib.sha256(body).hexdigest()
            for ordinal, (decision, pixel) in enumerate(fixtures, 1):
                body = rgba_png(pixel)
                edited = work / f"fixture-{ordinal}.webp"
                edited.write_bytes(bytes([ordinal]))
                assets.append({"ordinal": ordinal, "edited": edited.name,
                               "edited_sha256": digest(edited.read_bytes())})
                (png_root / f"asset-{ordinal:04d}.source.png").write_bytes(original)
                (png_root / f"asset-{ordinal:04d}.edited.png").write_bytes(body)
                pairs.append({"ordinal": ordinal, "source_sha256": digest(original), "edited_sha256": digest(body)})
                records.append({"ordinal": ordinal, "status": decision})
            (png_root / "pairs.json").write_text(json.dumps({"task_id": "fixture", "pairs": pairs}), encoding="utf-8")
            task = {"task_id": "fixture", "webp_visual_review_png_root": png_root.name,
                    "webp_visual_review": {"draft_task_id": "fixture-draft", "assets": assets}}
            report = {"schema": SCHEMA, "task_id": "fixture", "draft_task_id": "fixture-draft", "records": records}
            path = work / "report.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(_review_counts(work, task, path), {"total": 3, "success": 1, "error": 1, "pending": 1})
            edited.write_bytes(b"changed after inspection")
            with self.assertRaisesRegex(ConfigurationError, "REVIEW_BINDING_INVALID"):
                _review_counts(work, task, path)
