from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from game_l10n.cli import main
from game_l10n.control_plane import (
    acknowledge_control_plane_rotation,
    control_plane_state,
    record_control_plane_leak,
    validate_control_envelope,
)
from game_l10n.errors import ConfigurationError
from game_l10n.models import Segment
from game_l10n.native_pass import prepare_native_tasks


class ControlPlaneContractTests(unittest.TestCase):
    def _write_envelope(self, root: Path, payload: dict) -> Path:
        path = root / ".game-l10n" / "control" / "worker.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _valid_translation_envelope(self, root: Path) -> dict:
        artifact = root / ".game-l10n" / "data-plane" / "result.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{}", encoding="utf-8")
        return {
            "schema": "parr0t.control-envelope.v2",
            "worker_role": "translator",
            "stage": "translation",
            "status": "fallback_required",
            "target_ids": ["fixture-1"],
            "counts": {"total": 1, "success": 0, "fallback_required": 1},
            "issue_codes": ["fallback_required"],
            "artifact_paths": [".game-l10n/data-plane/result.json"],
            "queue_transitions": [
                {
                    "from": "primary_pending",
                    "to": "translation_fallback_pending",
                    "count": 1,
                }
            ],
            "route_evidence_path": "",
        }

    def _write_routing_config(self, root: Path) -> None:
        (root / "game-l10n.json").write_text(
            json.dumps(
                {
                    "routing": {
                        "translators": ["native-primary", "fallback-provider"],
                        "reviewers": ["native-review", "fallback-provider"],
                        "rewriters": ["fallback-provider"],
                        "context_analyzers": ["native-context"],
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_valid_envelope_returns_only_allowlisted_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envelope = self._write_envelope(root, self._valid_translation_envelope(root))
            result = validate_control_envelope(
                root,
                envelope,
                expected_role="translator",
                expected_stage="translation",
            )

        self.assertEqual(result["status"], "fallback_required")
        self.assertEqual(result["target_ids"], ["fixture-1"])
        self.assertEqual(result["issue_codes"], ["fallback_required"])
        self.assertNotIn("source", result)
        self.assertNotIn("reason", result)
        self.assertNotIn("text", result)

    def test_parent_unsafe_field_is_rejected_without_echoing_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._valid_translation_envelope(root)
            payload["source"] = "PRIVATE_SOURCE_VALUE"
            envelope = self._write_envelope(root, payload)
            with self.assertRaises(ConfigurationError) as captured:
                validate_control_envelope(root, envelope)

        self.assertIn("E_CONTROL_ENVELOPE_INVALID", str(captured.exception))
        self.assertNotIn("PRIVATE_SOURCE_VALUE", str(captured.exception))

    def test_inventory_worker_cannot_emit_provider_or_content_judgment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "inputs" / "primary.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("{}", encoding="utf-8")
            payload = {
                "schema": "parr0t.control-envelope.v2",
                "worker_role": "primary_inventory",
                "stage": "inventory",
                "status": "partial",
                "target_ids": ["fixture-1"],
                "counts": {"total": 1, "pending": 1},
                "issue_codes": ["fallback_required"],
                "artifact_paths": ["inputs/primary.json"],
                "queue_transitions": [],
                "route_evidence_path": "",
            }
            envelope = self._write_envelope(root, payload)
            with self.assertRaisesRegex(ConfigurationError, "unauthorized semantic"):
                validate_control_envelope(root, envelope)

    def test_detailed_review_judgment_cannot_cross_to_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "review" / "private-result.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("{}", encoding="utf-8")
            payload = {
                "schema": "parr0t.control-envelope.v2",
                "worker_role": "review_judge",
                "stage": "review",
                "status": "partial",
                "target_ids": ["fixture-1"],
                "counts": {"total": 1, "rewrite": 1},
                "issue_codes": ["meaning"],
                "artifact_paths": ["review/private-result.json"],
                "queue_transitions": [
                    {"from": "review_pending", "to": "rewrite_pending", "count": 1}
                ],
                "route_evidence_path": "",
            }
            envelope = self._write_envelope(root, payload)
            with self.assertRaisesRegex(ConfigurationError, "unauthorized semantic"):
                validate_control_envelope(root, envelope)

    def test_v2_rejects_provider_refusal_at_parent_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._valid_translation_envelope(root)
            payload["status"] = "refused"
            payload["issue_codes"] = ["provider_refusal"]
            envelope = self._write_envelope(root, payload)
            with self.assertRaisesRegex(ConfigurationError, "fallback_required"):
                validate_control_envelope(root, envelope)

    def test_legacy_nonterminal_refusal_is_normalized_to_fallback_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._valid_translation_envelope(root)
            payload.pop("route_evidence_path")
            payload.update(
                {
                    "schema": "parr0t.control-envelope.v1",
                    "status": "refused",
                    "counts": {"total": 1, "success": 0, "refused": 1},
                    "issue_codes": ["provider_refusal"],
                }
            )
            envelope = self._write_envelope(root, payload)
            result = validate_control_envelope(root, envelope)

        self.assertEqual(result["schema"], "parr0t.control-envelope.v2")
        self.assertEqual(result["status"], "fallback_required")
        self.assertEqual(result["counts"]["fallback_required"], 1)
        self.assertEqual(result["issue_codes"], ["fallback_required"])

    def test_source_preserved_requires_every_configured_route_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_routing_config(root)
            payload = self._valid_translation_envelope(root)
            evidence_path = root / ".game-l10n" / "route-evidence" / "translation.json"
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.route-evidence.v1",
                        "stage": "translation",
                        "target_ids": ["fixture-1"],
                        "attempts": [
                            {
                                "target_id": "fixture-1",
                                "route": "native-primary",
                                "status": "provider_refusal",
                                "attempts": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payload.update(
                {
                    "status": "partial",
                    "counts": {"total": 1, "success": 0, "source_preserved": 1},
                    "issue_codes": ["source_preserved"],
                    "artifact_paths": [
                        ".game-l10n/data-plane/result.json",
                        ".game-l10n/route-evidence/translation.json",
                    ],
                    "queue_transitions": [
                        {
                            "from": "translation_fallback_pending",
                            "to": "source_preserved",
                            "count": 1,
                        }
                    ],
                    "route_evidence_path": ".game-l10n/route-evidence/translation.json",
                }
            )
            envelope = self._write_envelope(root, payload)
            with self.assertRaisesRegex(ConfigurationError, "every configured route"):
                validate_control_envelope(root, envelope)

    def test_source_preserved_accepts_complete_route_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_routing_config(root)
            payload = self._valid_translation_envelope(root)
            evidence_path = root / ".game-l10n" / "route-evidence" / "translation.json"
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.route-evidence.v1",
                        "stage": "translation",
                        "target_ids": ["fixture-1"],
                        "attempts": [
                            {
                                "target_id": "fixture-1",
                                "route": "native-primary",
                                "status": "provider_refusal",
                                "attempts": 1,
                            },
                            {
                                "target_id": "fixture-1",
                                "route": "fallback-provider",
                                "status": "quota_exhausted",
                                "attempts": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            payload.update(
                {
                    "status": "partial",
                    "counts": {"total": 1, "success": 0, "source_preserved": 1},
                    "issue_codes": ["source_preserved"],
                    "artifact_paths": [
                        ".game-l10n/data-plane/result.json",
                        ".game-l10n/route-evidence/translation.json",
                    ],
                    "queue_transitions": [
                        {
                            "from": "translation_fallback_pending",
                            "to": "source_preserved",
                            "count": 1,
                        }
                    ],
                    "route_evidence_path": ".game-l10n/route-evidence/translation.json",
                }
            )
            envelope = self._write_envelope(root, payload)
            result = validate_control_envelope(root, envelope)

        self.assertEqual(result["counts"]["source_preserved"], 1)
        self.assertEqual(
            result["route_evidence_path"],
            ".game-l10n/route-evidence/translation.json",
        )

    def test_leak_requires_new_control_plane_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "inputs" / "source.jsonl"
            source.parent.mkdir(parents=True)
            source.write_text("{}\n", encoding="utf-8")
            incident = record_control_plane_leak(root, "semantic_judgment_performed")

            with self.assertRaisesRegex(ConfigurationError, "ROTATION_REQUIRED"):
                prepare_native_tasks(
                    root,
                    source,
                    [Segment("fixture-1", "SOURCE")],
                    root / "batches",
                )

            resumed = acknowledge_control_plane_rotation(root, incident["incident_id"])
            result = prepare_native_tasks(
                root,
                source,
                [Segment("fixture-1", "SOURCE")],
                root / "batches",
            )

        self.assertEqual(resumed["generation"], 2)
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(result["tasks"], 1)

    def test_rotation_acknowledgment_must_match_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_control_plane_leak(root, "source_or_candidate_received")
            with self.assertRaisesRegex(ConfigurationError, "incident ID"):
                acknowledge_control_plane_rotation(root, "wrong")
            state = control_plane_state(root)

        self.assertEqual(state["status"], "rotation_required")

    def test_rotation_blocks_non_recovery_cli_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_control_plane_leak(root, "semantic_judgment_performed")
            stderr = io.StringIO()
            stdout = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                blocked = main(
                    [
                        "verify-inventory",
                        "--workspace",
                        str(root),
                        "--primary",
                        "primary.json",
                        "--independent",
                        "independent.json",
                    ]
                )
                status = main(["control-plane-status", "--workspace", str(root)])

        self.assertEqual(blocked, 2)
        self.assertIn("E_CONTROL_PLANE_ROTATION_REQUIRED", stderr.getvalue())
        self.assertEqual(status, 0)
        self.assertIn("rotation_required", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
