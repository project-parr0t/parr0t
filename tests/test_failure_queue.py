from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from game_l10n.failure_queue import load_failure_queue, update_failure_queue
from game_l10n.models import AttemptRecord, Segment, TranslationResult


class FailureQueueTests(unittest.TestCase):
    def test_recoverable_failures_continue_without_new_human_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segments = [Segment("pending", "PENDING"), Segment("terminal", "TERMINAL")]
            results = [
                TranslationResult("pending", "PENDING", attempts=[
                    AttemptRecord("translation", "configured", "error", issue_code="provider_error"),
                ]),
                TranslationResult("terminal", "TERMINAL", status="source_preserved"),
            ]
            state = update_failure_queue(root, segments, results, inventory_fingerprint="inventory")
            self.assertEqual(state["status"], "pending")
            self.assertEqual(state["entries"][0]["category"], "operational")
            self.assertTrue(state["entries"][0]["retryable"])
            self.assertTrue(state["entries"][0]["continuation_authorized"])
            self.assertFalse(state["entries"][0]["continuation_eligible"])
            self.assertEqual(state["runnable_count"], 0)
            self.assertFalse(state["terminal_entries"][0]["retryable"])
            self.assertFalse(state["terminal_entries"][0]["continuation_authorized"])
            unknown = update_failure_queue(
                root, [segments[0]], [TranslationResult("pending", "PENDING")],
                inventory_fingerprint="inventory",
            )
            self.assertEqual(unknown["entries"][0]["category"], "repair_required")
            self.assertEqual(unknown["entries"][0]["failure_codes"], [])
            self.assertEqual(load_failure_queue(root, "inventory"), unknown)

    def test_legacy_queue_migration_retains_an_actual_user_pause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / ".game-l10n/failure-queue.json"
            queue.parent.mkdir()
            queue.write_text(json.dumps({
                "schema": "parr0t.failure-queue.v2", "inventory_fingerprint": "inventory",
                "status": "paused", "entries": [{
                    "target_id": "pending", "resolution": "failed",
                    "retryable_on_user_resume": True,
                }], "terminal_entries": [],
            }), encoding="utf-8")
            state = load_failure_queue(root, "inventory")
            self.assertEqual(state["status"], "paused")
            self.assertTrue(state["entries"][0]["retryable"])
            self.assertTrue(state["entries"][0]["continuation_authorized"])
            self.assertTrue(state["paused"])
            self.assertFalse(state["entries"][0]["continuation_eligible"])

    def test_only_proven_next_stage_is_immediately_runnable(self) -> None:
        binding = {
            "provider": "approved-next", "model": "fixture/next",
            "provider_fingerprint": "a" * 64, "ordinal": 3,
            "candidate_generation": 0, "candidate_digest": None,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = Segment("pending", "PENDING")
            result = TranslationResult("pending", "PENDING", attempts=[
                AttemptRecord("translation", "previous", "error", issue_code="provider_error"),
            ])
            for status, runnable in (
                ({"eligible": False, "next_action": "repair", "route_binding": None, "issue_code": "operational_route_unchanged"}, False),
                ({"eligible": True, "next_action": "repair", "route_binding": binding, "issue_code": ""}, False),
                ({"eligible": True, "next_action": "translate", "route_binding": {}, "issue_code": ""}, False),
                ({"eligible": True, "next_action": "translate", "route_binding": binding, "issue_code": ""}, True),
                ({"eligible": True, "next_action": "accept", "route_binding": None, "issue_code": ""}, True),
            ):
                with self.subTest(status=status):
                    state = update_failure_queue(
                        root, [segment], [result], inventory_fingerprint="inventory",
                        continuation_status={"pending": status},
                    )
                    self.assertTrue(state["entries"][0]["continuation_authorized"])
                    self.assertEqual(state["entries"][0]["continuation_eligible"], runnable)
                    self.assertEqual(state["runnable_count"], int(runnable))
                    loaded = load_failure_queue(root, "inventory", continuation_status={"pending": status})
                    self.assertEqual(loaded, state)

    def test_update_and_load_preserve_pause_and_explicit_target_denials(self) -> None:
        proof = {"eligible": True, "next_action": "accept", "route_binding": None, "issue_code": ""}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = Segment("pending", "PENDING")
            result = TranslationResult("pending", "PENDING")
            queue = root / ".game-l10n/failure-queue.json"
            queue.parent.mkdir()
            for flags in (
                {"retryable": False, "continuation_authorized": True},
                {"retryable": True, "continuation_authorized": False},
                {"retryable_on_user_resume": False},
            ):
                with self.subTest(flags=flags):
                    queue.write_text(json.dumps({
                        "schema": "parr0t.failure-queue.v2", "inventory_fingerprint": "inventory",
                        "status": "pending", "entries": [{"target_id": "pending", "resolution": "failed", **flags}],
                        "terminal_entries": [],
                    }), encoding="utf-8")
                    loaded = load_failure_queue(root, "inventory", continuation_status={"pending": proof})
                    self.assertFalse(loaded["entries"][0]["continuation_eligible"])
                    updated = update_failure_queue(
                        root, [segment], [result], inventory_fingerprint="inventory",
                        continuation_status={"pending": proof},
                    )
                    self.assertEqual(updated["runnable_count"], 0)
            queue.unlink()
            paused = update_failure_queue(
                root, [segment], [result], inventory_fingerprint="inventory",
                continuation_status={"pending": proof}, paused=True,
            )
            retained = update_failure_queue(
                root, [segment], [result], inventory_fingerprint="inventory",
                continuation_status={"pending": proof},
            )
            self.assertTrue(paused["paused"])
            self.assertTrue(retained["paused"])
            self.assertEqual(retained["status"], "paused")
            self.assertEqual(retained["runnable_count"], 0)


if __name__ == "__main__":
    unittest.main()
