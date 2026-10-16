from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.models import Segment
from game_l10n.translation_exchange import (
    FAILURE_NAME,
    REQUESTS_NAME,
    SUCCESS_NAME,
    TranslationExchangeError,
    prepare_translation_exchange,
    seal_translation_exchange,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class TranslationExchangeTests(unittest.TestCase):
    def test_partial_exchange_routes_only_metadata_to_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bundle = workspace / "private" / "exchange"
            segments = [
                Segment(id="line-1", source="Hello", speaker="A", scene="intro"),
                Segment(id="line-2", source="Goodbye", speaker="B", scene="intro"),
                Segment(id="line-3", source="Wait", speaker="A", scene="intro"),
            ]
            prepared = prepare_translation_exchange(
                workspace,
                segments,
                bundle,
                exchange_id="manual-1",
                mode="manual",
                source_queue="translation_fallback_pending",
            )
            self.assertEqual(prepared["target_count"], 3)
            requests = [json.loads(line) for line in (bundle / REQUESTS_NAME).read_text(encoding="utf-8").splitlines()]
            success = {
                "target_id": "line-1",
                "status": "success",
                "text": "SYNTHETIC_TARGET_TEXT",
                "route": "human",
                "attempt": 1,
                "source_sha256": requests[0]["source_sha256"],
                "context_sha256": requests[0]["context_sha256"],
            }
            failure = {
                "target_id": "line-2",
                "status": "failed",
                "failure_code": "timeout",
                "route": "fallback-api",
                "attempt": 1,
                "source_sha256": requests[1]["source_sha256"],
                "context_sha256": requests[1]["context_sha256"],
                "retryable": True,
            }
            (bundle / SUCCESS_NAME).write_text(json.dumps(success, ensure_ascii=False) + "\n", encoding="utf-8")
            (bundle / FAILURE_NAME).write_text(json.dumps(failure) + "\n", encoding="utf-8")
            result = seal_translation_exchange(workspace, bundle)

            self.assertEqual(result["status"], "fallback_required")
            self.assertEqual(result["success_count"], 1)
            self.assertEqual(result["failure_count"], 1)
            self.assertEqual(result["pending_count"], 1)
            envelope = json.loads((bundle / "control-envelope.json").read_text(encoding="utf-8"))
            self.assertNotIn("source", json.dumps(envelope))
            self.assertNotIn("SYNTHETIC_TARGET_TEXT", json.dumps(envelope, ensure_ascii=False))
            self.assertEqual(envelope["counts"]["fallback_required"], 2)
            self.assertEqual(envelope["queue_transitions"][0]["to"], "review_pending")

    def test_exchange_rejects_modified_requests_and_ad_hoc_failure_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            bundle = workspace / "exchange"
            prepare_translation_exchange(
                workspace,
                [Segment(id="line-1", source="Hello")],
                bundle,
                exchange_id="fallback-1",
                mode="fallback",
                source_queue="translation_fallback_pending",
            )
            request = (bundle / REQUESTS_NAME).read_text(encoding="utf-8")
            (bundle / REQUESTS_NAME).write_text(request.replace("Hello", "Changed"), encoding="utf-8")
            with self.assertRaises(TranslationExchangeError):
                seal_translation_exchange(workspace, bundle)

            (bundle / REQUESTS_NAME).write_text(request, encoding="utf-8")
            row = json.loads(request)
            ad_hoc = {
                "target_id": "line-1",
                "status": "failed",
                "failure_code": "provider_error",
                "route": "fallback-api",
                "attempt": 1,
                "source_sha256": row["source_sha256"],
                "context_sha256": row["context_sha256"],
                "retryable": True,
                "reason": "free-form refusal prose must not cross the boundary",
            }
            (bundle / FAILURE_NAME).write_text(json.dumps(ad_hoc) + "\n", encoding="utf-8")
            with self.assertRaises(TranslationExchangeError):
                seal_translation_exchange(workspace, bundle)


if __name__ == "__main__":
    unittest.main()
