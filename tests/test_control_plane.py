from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.cli import build_parser, main
from game_l10n.control_plane import (
    CONTROL_HANDOFF_PATH,
    REVIEW_CONTROL_ALLOWED_ACTIONS,
    REVIEW_CONTROL_CONTRACT_SCHEMA,
    REVIEW_CONTROL_MODE,
    review_control_contract,
    validate_review_control_action,
    acknowledge_control_plane_rotation,
    control_plane_entry_state,
    control_target_id,
    control_plane_state,
    establish_control_plane_entry,
    record_control_plane_leak,
    require_control_plane_entry,
    validate_control_envelope,
)
from game_l10n.errors import ConfigurationError
from game_l10n.data_plane_task import (
    prepare_data_plane_task,
    seal_data_plane_task,
)
from game_l10n.coverage import save_inventory_attestation
from game_l10n.models import ContextPack, Segment
from game_l10n.native_pass import _detail_contract, prepare_native_tasks


class ControlPlaneContractTests(unittest.TestCase):
    def test_discovery_cli_uses_a_core_bound_data_plane_relay(self) -> None:
        args = build_parser().parse_args(
            [
                "prepare-data-plane-task",
                "--workspace",
                "fixture-workspace",
                "--worker-role",
                "source_probe",
            ]
        )
        self.assertEqual(args.func.__name__, "cmd_prepare_data_plane_task")

    def test_data_plane_seal_cli_accepts_categorical_worker_end(self) -> None:
        args = build_parser().parse_args(
            [
                "seal-data-plane-task",
                "--workspace",
                "fixture-workspace",
                "--relay",
                ".game-l10n/data-plane/source_probe/relay.json",
                "--worker-ended",
            ]
        )
        self.assertTrue(args.worker_ended)

    def test_ended_data_plane_worker_is_sealed_without_forwarding_prose(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            work = root / "work"
            sdk = root / "sdk"
            game.mkdir()
            work.mkdir()
            sdk.mkdir()
            (work / "game-l10n.json").write_text("{}", encoding="utf-8")
            establish_control_plane_entry(work)
            config = {
                "game_roots": [str(game)],
                "sdk_root": str(sdk),
                "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
            }
            output = work / ".game-l10n" / "data-plane" / "source_probe"
            prepared = prepare_data_plane_task(
                work, output, worker_role="source_probe", config=config
            )
            relay = work / prepared["relay_path"]
            relay_payload = json.loads(relay.read_text(encoding="utf-8"))

            sealed = seal_data_plane_task(work, relay, worker_ended=True)
            receipt = json.loads(
                (work / sealed["receipt_path"]).read_text(encoding="utf-8")
            )
            retried = prepare_data_plane_task(
                work, output, worker_role="source_probe", config=config
            )

        self.assertEqual(relay_payload["schema"], "parr0t.data-plane-relay.v2")
        self.assertEqual(
            relay_payload["relay_contract"]["child_response_policy"],
            "never_read_interpret_summarize_or_forward",
        )
        self.assertIn(
            "--worker-ended",
            relay_payload["relay_contract"]["terminal_seal_command"],
        )
        self.assertEqual(sealed["status"], "error")
        self.assertEqual(sealed["issue_codes"], ["task_incomplete"])
        self.assertEqual(receipt["artifact_paths"], [])
        self.assertEqual(receipt["queue_transitions"], [])
        self.assertNotIn("response", json.dumps(receipt))
        self.assertNotEqual(prepared["task_id"], retried["task_id"])

    def test_ended_data_plane_cli_records_error_receipt_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            work = root / "work"
            sdk = root / "sdk"
            game.mkdir()
            work.mkdir()
            sdk.mkdir()
            (work / "game-l10n.json").write_text("{}", encoding="utf-8")
            establish_control_plane_entry(work)
            prepared = prepare_data_plane_task(
                work,
                work / ".game-l10n" / "data-plane" / "source_probe",
                worker_role="source_probe",
                config={
                    "game_roots": [str(game)],
                    "sdk_root": str(sdk),
                    "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
                },
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "seal-data-plane-task",
                        "--workspace",
                        str(work),
                        "--relay",
                        prepared["relay_path"],
                        "--worker-ended",
                    ]
                )
            result = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["issue_codes"], ["task_incomplete"])

    def test_ended_data_plane_worker_seals_malformed_envelope_categorically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            work = root / "work"
            sdk = root / "sdk"
            game.mkdir()
            work.mkdir()
            sdk.mkdir()
            (work / "game-l10n.json").write_text("{}", encoding="utf-8")
            establish_control_plane_entry(work)
            config = {
                "game_roots": [str(game)],
                "sdk_root": str(sdk),
                "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
            }
            prepared = prepare_data_plane_task(
                work,
                work / ".game-l10n" / "data-plane" / "source_probe",
                worker_role="source_probe",
                config=config,
            )
            relay = work / prepared["relay_path"]
            relay_payload = json.loads(relay.read_text(encoding="utf-8"))
            envelope = work / relay_payload["expected_envelope"]
            envelope.write_text("{invalid", encoding="utf-8")

            sealed = seal_data_plane_task(work, relay, worker_ended=True)

        self.assertEqual(sealed["status"], "error")
        self.assertEqual(sealed["issue_codes"], ["malformed_response"])

    def test_legacy_data_plane_relay_remains_sealable_for_checkpoint_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            work = root / "work"
            sdk = root / "sdk"
            game.mkdir()
            work.mkdir()
            sdk.mkdir()
            (work / "game-l10n.json").write_text("{}", encoding="utf-8")
            establish_control_plane_entry(work)
            prepared = prepare_data_plane_task(
                work,
                work / ".game-l10n" / "data-plane" / "source_probe",
                worker_role="source_probe",
                config={
                    "game_roots": [str(game)],
                    "sdk_root": str(sdk),
                    "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
                },
            )
            relay = work / prepared["relay_path"]
            payload = json.loads(relay.read_text(encoding="utf-8"))
            payload["schema"] = "parr0t.data-plane-relay.v1"
            payload.pop("relay_contract")
            relay.write_text(json.dumps(payload), encoding="utf-8")

            sealed = seal_data_plane_task(work, relay, worker_ended=True)

        self.assertEqual(sealed["status"], "error")
        self.assertEqual(sealed["issue_codes"], ["task_incomplete"])

    def test_data_plane_relay_rejects_a_weakened_child_response_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            work = root / "work"
            sdk = root / "sdk"
            game.mkdir()
            work.mkdir()
            sdk.mkdir()
            (work / "game-l10n.json").write_text("{}", encoding="utf-8")
            establish_control_plane_entry(work)
            prepared = prepare_data_plane_task(
                work,
                work / ".game-l10n" / "data-plane" / "source_probe",
                worker_role="source_probe",
                config={
                    "game_roots": [str(game)],
                    "sdk_root": str(sdk),
                    "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
                },
            )
            relay = work / prepared["relay_path"]
            payload = json.loads(relay.read_text(encoding="utf-8"))
            payload["relay_contract"]["child_response_policy"] = "summarize"
            relay.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                ConfigurationError, "E_DATA_PLANE_RELAY_INVALID"
            ):
                seal_data_plane_task(work, relay, worker_ended=True)

    def test_core_bound_source_probe_seals_only_its_expected_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            work = root / "work"
            sdk = root / "sdk"
            game.mkdir()
            work.mkdir()
            sdk.mkdir()
            (work / "game-l10n.json").write_text("{}", encoding="utf-8")
            establish_control_plane_entry(work)
            config = {
                "game_roots": [str(game)],
                "sdk_root": str(sdk),
                "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
            }
            prepared = prepare_data_plane_task(
                work,
                work / ".game-l10n" / "data-plane" / "source_probe",
                worker_role="source_probe",
                config=config,
            )
            relay = work / prepared["relay_path"]
            relay_payload = json.loads(relay.read_text(encoding="utf-8"))
            (work / "game-l10n.json").write_text(
                json.dumps(
                    {
                        "sdk_selection": {
                            "status": "selected",
                            "inventory_fingerprint": "fixture-inventory",
                        }
                    }
                ),
                encoding="utf-8",
            )
            envelope = work / relay_payload["expected_envelope"]
            envelope.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.control-envelope.v2",
                        "worker_role": "source_probe",
                        "stage": "discovery",
                        "status": "success",
                        "target_ids": [],
                        "counts": {"total": 1, "success": 1},
                        "issue_codes": [],
                        "artifact_paths": ["game-l10n.json"],
                        "queue_transitions": [],
                        "route_evidence_path": "",
                    }
                ),
                encoding="utf-8",
            )

            sealed = seal_data_plane_task(work, relay, worker_ended=True)

        self.assertEqual(sealed["status"], "success")
        self.assertEqual(sealed["worker_role"], "source_probe")
        self.assertEqual(sealed["stage"], "discovery")
        self.assertTrue(sealed["receipt_path"].endswith("receipt.json"))

    def test_reprepared_data_plane_task_gets_a_new_bound_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            work = root / "work"
            sdk = root / "sdk"
            game.mkdir()
            work.mkdir()
            sdk.mkdir()
            (work / "game-l10n.json").write_text("{}", encoding="utf-8")
            establish_control_plane_entry(work)
            config = {
                "game_roots": [str(game)],
                "sdk_root": str(sdk),
                "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
            }
            output = work / ".game-l10n" / "data-plane" / "source_probe"
            first = prepare_data_plane_task(
                work, output, worker_role="source_probe", config=config
            )
            second = prepare_data_plane_task(
                work, output, worker_role="source_probe", config=config
            )
            task = json.loads((output / "task.json").read_text(encoding="utf-8"))

        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(task["attempt"], 2)

    def test_native_context_worker_is_sealed_into_core_context_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            work = root / "work"
            sdk = root / "sdk"
            game.mkdir()
            work.mkdir()
            sdk.mkdir()
            establish_control_plane_entry(work)
            inputs = work / "inputs"
            inputs.mkdir()
            inventory = {
                "schema": "parr0t.source-inventory.v1",
                "method": "fixture",
                "implementation_fingerprint": "fixture-implementation",
                "coverage": {"declared_complete": True, "exclusions": []},
                "segments": [{"id": "fixture-1", "source": "SOURCE"}],
            }
            primary = inputs / "primary-inventory.json"
            independent = inputs / "independent-inventory.json"
            primary.write_text(json.dumps(inventory), encoding="utf-8")
            independent.write_text(
                json.dumps(
                    {
                        **inventory,
                        "method": "fixture-independent",
                        "implementation_fingerprint": "fixture-independent-implementation",
                    }
                ),
                encoding="utf-8",
            )
            verified = save_inventory_attestation(work, primary, independent)
            config = {
                "game_roots": [str(game)],
                "sdk_root": str(sdk),
                "sdk_selection": {"inventory_fingerprint": "fixture-sdk"},
                "files": {"context_pack": "context/context-pack.json"},
                "project": {"context_mode": "auto"},
                "routing": {"context_analyzers": ["native-sol"]},
            }
            prepared = prepare_data_plane_task(
                work,
                work / ".game-l10n" / "data-plane" / "context_analyzer",
                worker_role="context_analyzer",
                config=config,
                sdk_activation={"status": "active"},
            )
            relay = work / prepared["relay_path"]
            relay_payload = json.loads(relay.read_text(encoding="utf-8"))
            context_path = work / "context" / "context-pack.json"
            context_path.parent.mkdir()
            context_path.write_text(
                json.dumps(
                    asdict(
                        ContextPack(
                            context_mode="functional",
                            functional_summary="Fixture screen flow.",
                            global_style_rules=("Use consistent UI terms.",),
                            screen_flows={
                                "fixture": {"evidence_line_ids": ["fixture-1"]}
                            },
                        )
                    )
                ),
                encoding="utf-8",
            )
            envelope = work / relay_payload["expected_envelope"]
            envelope.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.control-envelope.v2",
                        "worker_role": "context_analyzer",
                        "stage": "context",
                        "status": "success",
                        "target_ids": [],
                        "counts": {"total": 1, "success": 1},
                        "issue_codes": [],
                        "artifact_paths": ["context/context-pack.json"],
                        "queue_transitions": [],
                        "route_evidence_path": "",
                    }
                ),
                encoding="utf-8",
            )

            sealed = seal_data_plane_task(work, relay)
            state = json.loads(
                (work / ".game-l10n" / "context-state.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(sealed["status"], "success")
        self.assertEqual(state["status"], "success")
        self.assertEqual(
            state["source_inventory_fingerprint"], verified["primary_fingerprint"]
        )
        self.assertTrue(state["attestation"]["functional_coverage"]["complete"])

    def test_native_review_pass_kind_is_registered(self) -> None:
        args = build_parser().parse_args(
            [
                "prepare-native-pass",
                "--workspace",
                "fixture-workspace",
                "--input",
                "source.jsonl",
                "--output",
                "tasks",
                "--pass-kind",
                "review",
            ]
        )
        self.assertEqual(args.pass_kind, "review")

    def test_native_pass_step_cli_is_registered(self) -> None:
        args = build_parser().parse_args(
            [
                "native-pass-step",
                "--workspace",
                "fixture-workspace",
                "--manifest",
                "tasks/manifest.json",
                "--results-dir",
                "results",
            ]
        )
        self.assertEqual(args.func.__name__, "cmd_native_pass_step")

    def test_native_relay_seal_accepts_repeated_ended_tasks(self) -> None:
        args = build_parser().parse_args(
            [
                "seal-native-wave-relay",
                "--workspace",
                "fixture-workspace",
                "--dispatch",
                "relay.json",
                "--ended-task",
                "task-0002.jsonl",
                "--ended-task",
                "task-0003.jsonl",
            ]
        )

        self.assertEqual(
            args.ended_task,
            ["task-0002.jsonl", "task-0003.jsonl"],
        )

    def test_native_relay_seal_accepts_terminal_relay_state(self) -> None:
        args = build_parser().parse_args(
            [
                "seal-native-wave-relay",
                "--workspace",
                "fixture-workspace",
                "--dispatch",
                "relay.json",
                "--relay-ended",
            ]
        )

        self.assertTrue(args.relay_ended)

    def test_native_relay_cli_rejects_missing_terminal_state(self) -> None:
        args = build_parser().parse_args(
            [
                "seal-native-wave-relay",
                "--workspace",
                "fixture-workspace",
                "--dispatch",
                "relay.json",
            ]
        )

        with (
            patch("game_l10n.cli.load_config", return_value={}),
            patch("game_l10n.cli.require_sdk_activation"),
            self.assertRaisesRegex(ValueError, "E_NATIVE_RELAY_TERMINAL_STATE_REQUIRED"),
        ):
            args.func(args)

    def test_review_control_contract_is_deterministic_and_text_free(self) -> None:
        contract = review_control_contract()

        self.assertEqual(contract["schema"], REVIEW_CONTROL_CONTRACT_SCHEMA)
        self.assertEqual(contract["mode"], REVIEW_CONTROL_MODE)
        self.assertEqual(
            contract["allowed_actions"], list(REVIEW_CONTROL_ALLOWED_ACTIONS)
        )
        self.assertFalse(contract["semantic_access_allowed"])
        self.assertFalse(contract["free_form_planning_allowed"])
        self.assertFalse(contract["ad_hoc_contracts_allowed"])
        self.assertFalse(contract["autonomous_resume_allowed"])
        self.assertFalse(contract["autonomous_sdk_or_tool_changes_allowed"])
        self.assertTrue(contract["status_action_match_required"])
        self.assertTrue(contract["native_step_command_required"])
        self.assertEqual(
            contract["native_step_command"], "game-l10n native-pass-step"
        )
        self.assertFalse(contract["model_selected_wave_or_recovery_allowed"])
        self.assertEqual(
            contract["halted_state_allowed_actions"],
            ["read_status", "record_aggregate_usage", "checkpoint_and_stop"],
        )
        self.assertEqual(
            contract["resume_requirements"],
            [
                "explicit_user_approval",
                "complete_budget_contract",
                "regenerated_validated_manifest",
            ],
        )
        self.assertEqual(contract["unknown_state_action"], "checkpoint_and_stop")
        self.assertEqual(
            contract["review_stage_order"],
            [
                "deterministic_validation",
                "complete_low_cost_triage",
                "selective_strong_review",
                "flagged_only_rewrite",
                "review_fallback_for_failures_only",
            ],
        )
        self.assertFalse(contract["universal_strong_review_required"])
        self.assertEqual(_detail_contract()["review_control"], contract)

    def test_review_control_action_allowlist_rejects_autonomous_recovery(self) -> None:
        permit = validate_review_control_action("dispatch_isolated_wave_relay")
        self.assertTrue(permit["authorized"])

        for action in (
            "resume_review",
            "clone_framework",
            "rebind_sdk",
            "repair_tool",
            "invent_result_format",
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    ConfigurationError, "E_REVIEW_CONTROL_ACTION_DENIED"
                ):
                    validate_review_control_action(action)

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

    def test_entry_gate_is_pending_until_clean_parent_confirms_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ConfigurationError, "ENTRY_REQUIRED"):
                require_control_plane_entry(root)
            result = establish_control_plane_entry(root)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["issue_code"], "")

    def test_cli_blocks_ordinary_work_before_entry_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
                entered = main(
                    [
                        "enter-control-plane",
                        "--workspace",
                        str(root),
                        "--contracts-read",
                        "--source-unopened",
                    ]
                )

        self.assertEqual(blocked, 2)
        self.assertEqual(entered, 0)
        self.assertIn("E_CONTROL_PLANE_ENTRY_REQUIRED", stderr.getvalue())
        self.assertIn('"status": "ready"', stdout.getvalue())

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
        self.assertEqual(result["target_ids"], [control_target_id("fixture-1")])
        self.assertNotIn("fixture-1", result["target_ids"])
        self.assertEqual(result["issue_codes"], ["fallback_required"])
        self.assertNotIn("source", result)
        self.assertNotIn("reason", result)
        self.assertNotIn("text", result)

    def test_source_probe_can_return_metadata_only_discovery_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / ".game-l10n" / "sdk-selection.json"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("{}", encoding="utf-8")
            payload = {
                "schema": "parr0t.control-envelope.v2",
                "worker_role": "source_probe",
                "stage": "discovery",
                "status": "success",
                "target_ids": [],
                "counts": {"total": 1, "success": 1},
                "issue_codes": [],
                "artifact_paths": [".game-l10n/sdk-selection.json"],
                "queue_transitions": [],
                "route_evidence_path": "",
            }
            envelope = self._write_envelope(root, payload)
            result = validate_control_envelope(
                root,
                envelope,
                expected_role="source_probe",
                expected_stage="discovery",
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["target_ids"], [])
        self.assertEqual(result["issue_codes"], [])

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

            resumed = acknowledge_control_plane_rotation(
                root, root / CONTROL_HANDOFF_PATH
            )
            self.assertEqual(control_plane_entry_state(root)["status"], "pending")
            establish_control_plane_entry(root)
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
            receipt = root / CONTROL_HANDOFF_PATH
            payload = json.loads(receipt.read_text(encoding="utf-8"))
            payload["incident_id"] = "0" * 32
            receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "HANDOFF_INVALID"):
                acknowledge_control_plane_rotation(root, receipt)
            state = control_plane_state(root)

        self.assertEqual(state["status"], "rotation_required")

    def test_control_state_rejects_unknown_fields_without_echoing_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / ".game-l10n" / "control-plane-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.control-plane-state.v1",
                        "status": "ready",
                        "generation": 1,
                        "incident_id": "",
                        "issue_code": "",
                        "incident_code": "",
                        "source": "PRIVATE_SOURCE_VALUE",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigurationError) as captured:
                control_plane_state(root)

        self.assertIn("unsupported fields", str(captured.exception))
        self.assertNotIn("PRIVATE_SOURCE_VALUE", str(captured.exception))

    def test_cli_hides_incident_id_and_requires_fresh_session_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incident = record_control_plane_leak(root, "source_or_candidate_received")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(["control-plane-status", "--workspace", str(root)])
                rejected = main(["resume-control-plane", "--workspace", str(root)])
            self.assertEqual(control_plane_state(root)["status"], "rotation_required")
            with redirect_stdout(stdout), redirect_stderr(stderr):
                resumed = main(
                    [
                        "resume-control-plane",
                        "--workspace",
                        str(root),
                        "--fresh-session-confirmed",
                        "--contracts-read",
                        "--source-unopened",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(rejected, 2)
        self.assertEqual(resumed, 0)
        self.assertNotIn(incident["incident_id"], stdout.getvalue())
        self.assertIn("E_CONTROL_PLANE_FRESH_SESSION_REQUIRED", stderr.getvalue())
        self.assertIn('"status": "ready"', stdout.getvalue())

    def test_resume_rejects_arbitrary_continuation_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_control_plane_leak(root, "unvalidated_worker_response")
            continuation = root / "continuation.txt"
            continuation.write_text("metadata only", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "HANDOFF_REQUIRED"):
                acknowledge_control_plane_rotation(root, continuation)

    def test_legacy_rotation_without_receipt_gets_canonical_core_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incident = record_control_plane_leak(root, "unvalidated_worker_response")
            (root / CONTROL_HANDOFF_PATH).unlink()
            resumed = acknowledge_control_plane_rotation(
                root, root / CONTROL_HANDOFF_PATH
            )

        self.assertEqual(resumed["generation"], incident["generation"] + 1)
        self.assertEqual(resumed["status"], "ready")

    def test_resume_resets_only_generation_local_native_polling_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native_state = root / ".game-l10n" / "native" / "control" / "state.json"
            native_state.parent.mkdir(parents=True)
            native_state.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.native-control-state.v2",
                        "control_steps_in_wave": 7,
                        "last_progress_marker": "unchanged",
                        "unchanged_observations": 3,
                        "last_action": "checkpoint_and_stop",
                        "input_tokens": 1234,
                        "imported_tasks": 9,
                    }
                ),
                encoding="utf-8",
            )
            record_control_plane_leak(root, "unvalidated_worker_response")
            acknowledge_control_plane_rotation(root, root / CONTROL_HANDOFF_PATH)
            reset = json.loads(native_state.read_text(encoding="utf-8"))

        self.assertEqual(reset["control_steps_in_wave"], 0)
        self.assertEqual(reset["last_progress_marker"], "")
        self.assertEqual(reset["unchanged_observations"], 0)
        self.assertEqual(reset["last_action"], "")
        self.assertEqual(reset["input_tokens"], 1234)
        self.assertEqual(reset["imported_tasks"], 9)

    def test_rotation_blocks_non_recovery_cli_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incident = record_control_plane_leak(root, "semantic_judgment_performed")
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
        self.assertNotIn(incident["incident_id"], stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
