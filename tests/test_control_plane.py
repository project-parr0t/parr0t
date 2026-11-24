from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from tests.execution_policy_fixture import approved_execution_policy

from game_l10n.cli import build_parser, main
from game_l10n.control_plane import (
    CONTROL_HANDOFF_PATH,
    ISSUE_CODES_BY_STAGE,
    REVIEW_CONTROL_ALLOWED_ACTIONS,
    REVIEW_CONTROL_CONTRACT_SCHEMA,
    REVIEW_CONTROL_MODE,
    TERMINAL_ROUTE_STATUSES,
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
    claim_context_chunk,
    commit_context_fragment,
    finalize_context_fragments,
    prepare_data_plane_task,
    record_data_plane_failure,
    record_data_plane_relay_event,
    seal_data_plane_task,
)
from game_l10n.coverage import save_inventory_attestation
from game_l10n.models import ContextPack, Segment
from game_l10n.native_pass import _detail_contract, prepare_native_tasks
from game_l10n.native_runtime import (
    bind_data_plane_launch,
    configure_native_runtime,
    record_native_launch,
)
from game_l10n.workspace import initialize_workspace, load_config, record_sdk_selection
from tests import context_result_provenance


class ControlPlaneContractTests(unittest.TestCase):
    def test_preflight_constraint_failure_cannot_prove_provider_exhaustion(self) -> None:
        self.assertIn("constraint_unsatisfiable", ISSUE_CODES_BY_STAGE["translation"])
        self.assertNotIn("constraint_unsatisfiable", TERMINAL_ROUTE_STATUSES)

    def test_locale_probe_seals_only_task_bound_applicability_without_replacing_discovery(self) -> None:
        for valid in (False, True):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                sdk, game, work = root / "sdk", root / "game", root / "work"
                sdk.mkdir()
                game.mkdir()
                initialize_workspace(work, sdk, game_roots=[game], git_choice="disabled")
                establish_control_plane_entry(work)
                config = load_config(work)
                config["sdk_selection"]["inventory_fingerprint"] = "synthetic-selection"
                original_config = (work / "game-l10n.json").read_bytes()
                output = work / ".game-l10n/data-plane/locale_applicability"
                from game_l10n.locale_probe import INVENTORY_CHECKS, SCHEMA

                with patch("game_l10n.workspace.require_sdk_activation", return_value={"status": "active"}):
                    prepared = prepare_data_plane_task(
                        work, output, worker_role="source_probe", config=config,
                        sdk_activation={"status": "active"}, purpose="locale_applicability",
                    )
                    relay_path = work / prepared["relay_path"]
                    relay = json.loads(relay_path.read_bytes())
                    task = json.loads((work / relay["task_path"]).read_bytes())
                    artifact = task["output_artifact"]
                    (work / artifact).write_text(json.dumps({
                        "schema": SCHEMA, "task_id": task["task_id"] if valid else "unbound",
                        "outcome": "applicable", "checks": dict.fromkeys(INVENTORY_CHECKS, "verified"),
                        "evidence_sha256": "1" * 64,
                    }), encoding="utf-8")
                    (work / relay["expected_envelope"]).write_text(json.dumps({
                        "schema": "parr0t.control-envelope.v2", "worker_role": "source_probe",
                        "stage": "discovery", "status": "success", "target_ids": [],
                        "counts": {"total": 1, "success": 1}, "issue_codes": [],
                        "artifact_paths": [artifact], "queue_transitions": [], "route_evidence_path": "",
                    }), encoding="utf-8")
                    sealed = seal_data_plane_task(work, relay_path, worker_ended=True)
                self.assertEqual(sealed["issue_codes"], [
                    "locale_applicability_applicable" if valid else "malformed_response",
                ])
                self.assertEqual((work / "game-l10n.json").read_bytes(), original_config)
                self.assertFalse((work / ".game-l10n/data-plane/source_probe/receipt.json").exists())

    def test_locale_inventory_requires_source_proof_but_not_target_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk, game, work = root / "sdk", root / "game", root / "work"
            sdk.mkdir()
            game.mkdir()
            initialize_workspace(work, sdk, game_roots=[game], git_choice="disabled")
            establish_control_plane_entry(work)
            record_sdk_selection(work, "private-new", target_engine="fixture-engine", target_version="1.0")
            config = load_config(work)
            activation = {"target": {"engine": "fixture-engine"}}
            stage_dir = work / ".game-l10n/data-plane/locale_applicability"
            with (
                patch("game_l10n.cli.require_sdk_activation", return_value=activation),
                patch("game_l10n.workspace.require_sdk_activation", return_value=activation),
                patch("game_l10n.cli.preparation_state", return_value={"status": "not_required"}),
                patch("game_l10n.data_plane_task.require_extractor_preparation"),
            ):
                from game_l10n.locale_probe import CHECKS, SCHEMA

                def seal(outcome: str, checks: dict[str, str], *, malformed_wrapper: bool = False) -> None:
                    prepared = prepare_data_plane_task(
                        work, stage_dir, worker_role="source_probe", config=config,
                        sdk_activation=activation, purpose="locale_applicability",
                    )
                    relay_path = work / prepared["relay_path"]
                    relay = json.loads(relay_path.read_bytes())
                    task = json.loads((work / relay["task_path"]).read_bytes())
                    if outcome == "unavailable":
                        self.assertEqual(task["previous_locale_checks"], dict.fromkeys(CHECKS, "unresolved"))
                    artifact = task["output_artifact"]
                    (work / artifact).write_text(json.dumps({
                        "schema": SCHEMA, "task_id": task["task_id"], "outcome": outcome,
                        "checks": checks, "evidence_sha256": "1" * 64,
                    }), encoding="utf-8")
                    partial = outcome == "inconclusive"
                    (work / relay["expected_envelope"]).write_text(json.dumps({
                        "schema": "parr0t.control-envelope.v2", "worker_role": "source_probe",
                        "stage": "discovery", "status": "partial" if partial else "success",
                        "target_ids": [], "counts": {"total": 1, "success": int(not partial and not malformed_wrapper),
                                                     "pending": int(partial or malformed_wrapper)},
                        "issue_codes": [], "artifact_paths": [artifact], "queue_transitions": [],
                        "route_evidence_path": "",
                    }), encoding="utf-8")
                    expected = "malformed_response" if malformed_wrapper else "locale_applicability_" + outcome
                    self.assertEqual(seal_data_plane_task(work, relay_path, worker_ended=True)["issue_codes"], [expected])

                def status() -> dict:
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(main(["status", "--workspace", str(work)]), 0)
                    return json.loads(output.getvalue())

                seal("inconclusive", dict.fromkeys(CHECKS, "unresolved"))
                self.assertEqual(status()["next_actions"], ["verify_locale_applicability"])
                self.assertNotIn("evidence_fingerprint", status()["locale_applicability"])
                checks = dict.fromkeys(CHECKS, "unresolved")
                checks["loader_binding"] = "disproved"
                seal("unavailable", checks, malformed_wrapper=True)
                recovered = prepare_data_plane_task(
                    work, stage_dir, worker_role="source_probe", config=config,
                    sdk_activation=activation, purpose="locale_applicability",
                )
                self.assertFalse(recovered["dispatch_allowed"])
                self.assertEqual(recovered["issue_codes"], ["locale_applicability_unavailable"])
                self.assertEqual(status()["next_actions"], ["unpack_and_extract_ordered_script"])

                checks = dict.fromkeys(CHECKS, "verified")
                checks["target_route"] = "unresolved"
                seal("inconclusive", checks)
                state = status()
                self.assertEqual(state["next_actions"], ["unpack_and_extract_ordered_script"])
                self.assertEqual(state["locale_applicability"]["status"], "inconclusive")
                self.assertEqual(state["locale_applicability"]["inventory_status"], "applicable")

                # A prepared adapter may predate the current sealed probe.
                config["extractor_requirements"] = {"source_metadata": {
                    "locale_catalog_adapter": {"evidence_fingerprint": "2" * 64},
                }}
                for method in ("primary", "independent"):
                    with self.subTest(method=method):
                        role = f"{method}_inventory"
                        runner = work / "tools" / f"locale_inventory_{method}.py"
                        runner.write_text("raise SystemExit('synthetic runner must not execute')\n",
                                          encoding="utf-8")
                        prepared = prepare_data_plane_task(
                            work, stage_dir.parent / role, worker_role=role, config=config,
                            sdk_activation=activation,
                        )
                        relay = json.loads((work / prepared["relay_path"]).read_bytes())
                        task = json.loads((work / relay["task_path"]).read_bytes())
                        self.assertEqual(task["locale_probe_binding"], {
                            "route": "locale_data", "locale_probe": "applicable",
                            "evidence_fingerprint": "1" * 64,
                        })
                        self.assertEqual(task["locale_inventory_runner"]["path"],
                                         runner.relative_to(work).as_posix())

    def test_failed_discovery_receipt_blocks_sdk_activation_until_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk, game, work = root / "sdk", root / "game", root / "work"
            sdk.mkdir()
            game.mkdir()
            initialize_workspace(work, sdk, game_roots=[game], git_choice="disabled")
            establish_control_plane_entry(work)
            record_sdk_selection(
                work, "private-new", target_engine="fixture-engine", target_version="1.0"
            )
            generation = control_plane_state(work).get("generation", 0)
            receipt_path = work / ".game-l10n/data-plane/source_probe/receipt.json"
            receipt_path.parent.mkdir(parents=True)
            for status, receipt_generation in (
                ("partial", generation), ("error", generation),
                ("success", generation), ("partial", generation - 1),
            ):
                with self.subTest(status=status, generation=receipt_generation):
                    receipt_path.write_text(json.dumps({
                        "schema": "parr0t.data-plane-receipt.v1",
                        "task_id": "fixture-discovery", "worker_role": "source_probe",
                        "stage": "discovery", "status": status,
                        "counts": {"total": 1, "success": int(status == "success"),
                                   "error": int(status != "success"), "pending": 0},
                        "issue_codes": [] if status == "success" else ["worker_error"],
                        "artifact_paths": ["game-l10n.json"], "queue_transitions": [],
                        "control_generation": receipt_generation,
                    }), encoding="utf-8")
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(main(["status", "--workspace", str(work)]), 0)
                    result = json.loads(output.getvalue())
                    if status != "success" and receipt_generation == generation:
                        self.assertEqual(result["next_actions"],
                                         ["record_target_identity_evaluate_and_select_sdk"])
                        task = result["data_plane_task"]
                        self.assertTrue(task["continuation_required"])
                        self.assertEqual(task["last_receipt"]["status"], status)
                        self.assertEqual(result["sdk_activation"]["status"], "not_checked")
                    else:
                        self.assertEqual(result["next_actions"],
                                         ["activate_selected_sdk_implementation"])

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

    def test_context_relay_lifecycle_cli_is_registered(self) -> None:
        args = build_parser().parse_args(
            [
                "record-data-plane-relay-event",
                "--workspace",
                "fixture-workspace",
                "--relay",
                ".game-l10n/data-plane/context_analyzer/relay.json",
                "--event",
                "worker-started",
                "--slot",
                "1",
            ]
        )
        self.assertEqual(args.func.__name__, "cmd_record_data_plane_relay_event")
        self.assertEqual(args.slot, 1)

    def test_context_fragment_protocol_cli_is_registered(self) -> None:
        cases = (
            (
                [
                    "claim-context-chunk",
                    "--workspace",
                    "fixture-workspace",
                    "--task",
                    "task.json",
                    "--slot",
                    "1",
                ],
                "cmd_claim_context_chunk",
            ),
            (
                [
                    "commit-context-fragment",
                    "--workspace",
                    "fixture-workspace",
                    "--task",
                    "task.json",
                    "--claim",
                    "claim.json",
                    "--result",
                    "result.json",
                ],
                "cmd_commit_context_fragment",
            ),
            (
                [
                    "finalize-context-fragments",
                    "--workspace",
                    "fixture-workspace",
                    "--task",
                    "task.json",
                ],
                "cmd_finalize_context_fragments",
            ),
        )
        for argv, name in cases:
            with self.subTest(command=argv[0]):
                self.assertEqual(build_parser().parse_args(argv).func.__name__, name)



    def _sealed_inventory_pair(
        self, root: Path, *, namespace_mismatch: bool = True, primary_success: bool = False,
        request_skip: bool = False, approved_skip: bool = False,
    ):
        game, sdk, work = root / "game", root / "sdk", root / "work"
        game.mkdir()
        sdk.mkdir()
        initialize_workspace(work, sdk, game_roots=[game], git_choice="disabled")
        establish_control_plane_entry(work)
        if approved_skip:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main([
                    "screen-review-policy", "--workspace", str(work), "--mode", "skip", "--user-requested",
                ]), 0)
        config = load_config(work)
        relays = {}
        for role, method in (("primary_inventory", "primary"), ("independent_inventory", "independent")):
            complete = primary_success and role == "primary_inventory"
            output = work / ".game-l10n/data-plane" / role
            prepared = prepare_data_plane_task(
                work, output, worker_role=role, config=config, sdk_activation={}
            )
            relay_path = work / prepared["relay_path"]
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            task = json.loads((output / "task.json").read_text(encoding="utf-8"))
            rows = []
            for index in range(2):
                source = f"SYNTHETIC_SOURCE_{index}"
                prefix = method if namespace_mismatch else "canonical"
                rows.append({
                    "id": f"{prefix}-{index}", "source": source,
                    "metadata": {
                        "display_binding_id": f"display-{index}",
                        "storage_role": "display_value",
                        "display_scope_evidence": {
                            "kind": "positive_display_binding",
                            "binding_basis": "format_field",
                            "evidence_fingerprint": "a" * 64,
                            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                        },
                    },
                })
            artifact = work / task["output_artifact"]
            artifact.write_text(json.dumps({
                "schema": "parr0t.source-inventory.v1", "method": method,
                "implementation_fingerprint": f"{method}-implementation",
                "coverage": {
                    "declared_complete": complete, "exclusions": [],
                    "text_source": {
                        "route": "display_extraction", "locale_probe": "unavailable",
                        "evidence_fingerprint": "b" * 64,
                        "screen_review": "user_skipped" if request_skip else "static",
                        "pending_display_count": 0 if complete else 1,
                    },
                    "display_scope": {
                        "schema": "parr0t.display-scope.v1",
                        "raw_occurrence_count": 2, "display_candidate_count": 2,
                        "control_only_count": 0, "unclassified_count": 0,
                        "evidence_fingerprint": "synthetic-evidence",
                    },
                },
                "segments": rows,
            }), encoding="utf-8")
            (work / relay["expected_envelope"]).write_text(json.dumps({
                "schema": "parr0t.control-envelope.v2", "worker_role": role,
                "stage": "inventory", "status": "success" if complete else "partial", "target_ids": [],
                "counts": {"total": 2, "success": 2 if complete else 1, "pending": 0 if complete else 1},
                "issue_codes": [] if complete else ["inventory_not_declared_complete"],
                "artifact_paths": [task["output_artifact"]],
                "queue_transitions": [], "route_evidence_path": "",
            }), encoding="utf-8")
            sealed = seal_data_plane_task(work, relay_path, worker_ended=True)
            if request_skip and not approved_skip:
                self.assertEqual(sealed["issue_codes"], ["inventory_display_scope_required"])
            else:
                self.assertEqual(sealed["status"], "success" if complete else "partial")
            relays[role] = relay_path
        # Historical inventories predate the extractor-preparation barrier.
        # Selecting a deficient SDK must preserve their evidence, not authorize a new scan.
        record_sdk_selection(work, "private-new", target_engine="fixture-engine", target_version="1")
        config = load_config(work)
        return work, config, relays

    def test_inventory_seal_requires_explicit_approval_for_skipping_screen_review(self) -> None:
        for approved in (False, True):
            with self.subTest(approved=approved), tempfile.TemporaryDirectory() as directory:
                self._sealed_inventory_pair(
                    Path(directory), primary_success=True, request_skip=True, approved_skip=approved,
                )

    def _inventory_status(self, work: Path):
        output = io.StringIO()
        with patch("game_l10n.cli.require_sdk_activation", return_value={}), redirect_stdout(output):
            code = main(["status", "--workspace", str(work)])
        return code, json.loads(output.getvalue())

    def test_status_compares_sealed_partial_pair_before_inventory_expansion(self) -> None:
        for namespace_mismatch, primary_success in ((True, False), (False, False), (True, True)):
            with self.subTest(namespace_mismatch=namespace_mismatch, primary_success=primary_success), tempfile.TemporaryDirectory() as directory:
                work, _, _ = self._sealed_inventory_pair(
                    Path(directory), namespace_mismatch=namespace_mismatch, primary_success=primary_success
                )
                code, status = self._inventory_status(work)
                self.assertEqual(code, 0)
                self.assertEqual(status["next_actions"], ["verify_independent_source_inventory"])
                self.assertIsNone(status["data_plane_task"])
                attestation = work / ".game-l10n/inventory-attestation.json"
                self.assertFalse(attestation.exists())
                output = io.StringIO()
                with patch("game_l10n.cli.require_sdk_activation", return_value={}), redirect_stdout(output):
                    self.assertEqual(main([
                        "verify-inventory", "--workspace", str(work),
                        "--primary", "inputs/primary-inventory.json",
                        "--independent", "inputs/independent-inventory.json",
                    ]), 1)
                self.assertFalse(json.loads(attestation.read_text(encoding="utf-8"))["static_complete"])
                _, status = self._inventory_status(work)
                self.assertEqual(status["next_actions"], [
                    "reconcile_inventory_identity_namespace" if namespace_mismatch
                    else "resolve_source_inventory_gaps"
                ])
                self.assertEqual(status["data_plane_task"]["worker_role"], "inventory_reconciler")
                self.assertFalse(status["inventory_gate"]["static_complete"])

    def test_status_keeps_unsafe_partial_pairs_on_inventory_recovery(self) -> None:
        cases = (
            "malformed", "wrong_schema", "empty", "missing_artifact", "missing_receipt",
            "old_generation", "wrong_task", "changed_task", "unbound_artifact",
            "incomplete_execution", "operational_error", "mixed_provider_error",
            "missing_scope", "invalid_evidence", "same_method", "scope_repair",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                work, config, relays = self._sealed_inventory_pair(Path(directory))
                relay = relays["primary_inventory"]
                receipt_path = relay.with_name("receipt.json")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                artifact = work / "inputs/primary-inventory.json"
                payload = json.loads(artifact.read_text(encoding="utf-8"))
                if case == "malformed":
                    artifact.write_text("{", encoding="utf-8")
                elif case == "empty":
                    artifact.write_text("", encoding="utf-8")
                elif case == "missing_artifact":
                    artifact.unlink()
                elif case == "missing_receipt":
                    receipt_path.unlink()
                elif case in {"wrong_schema", "missing_scope", "invalid_evidence", "same_method"}:
                    if case == "wrong_schema":
                        payload["schema"] = "invalid"
                    elif case == "missing_scope":
                        del payload["coverage"]["display_scope"]
                    elif case == "invalid_evidence":
                        payload["segments"][0]["metadata"]["display_scope_evidence"]["source_sha256"] = "0" * 64
                    else:
                        payload["method"] = "independent"
                    artifact.write_text(json.dumps(payload), encoding="utf-8")
                elif case == "changed_task":
                    task_path = relay.with_name("task.json")
                    task = json.loads(task_path.read_text(encoding="utf-8"))
                    task["attempt"] += 1
                    task_path.write_text(json.dumps(task), encoding="utf-8")
                else:
                    if case == "old_generation":
                        receipt["control_generation"] -= 1
                    elif case == "wrong_task":
                        receipt["task_id"] = "f" * 64
                    elif case == "unbound_artifact":
                        receipt["artifact_paths"] = []
                    elif case == "incomplete_execution":
                        receipt["execution"]["complete"] = False
                    elif case == "scope_repair":
                        receipt["status"] = "error"
                        receipt["issue_codes"] = ["inventory_display_scope_required"]
                    elif case == "operational_error":
                        receipt["status"] = "error"
                        receipt["issue_codes"] = ["operational_error", "provider_operational_error"]
                    elif case == "mixed_provider_error":
                        receipt["issue_codes"].append("provider_error")
                    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
                _, status = self._inventory_status(work)
                self.assertEqual(status["next_actions"], ["prepare_and_validate_extractors"])
                self.assertIsNotNone(status["data_plane_task"])
                self.assertFalse((work / ".game-l10n/inventory-attestation.json").exists())

    def test_legacy_inventory_evidence_does_not_authorize_a_new_deficient_sdk_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, config, relays = self._sealed_inventory_pair(Path(directory))
            before = {
                path: path.read_bytes()
                for relay in relays.values()
                for path in (relay, relay.with_name("task.json"), relay.with_name("receipt.json"))
            }
            with patch("game_l10n.workspace.require_sdk_activation", return_value={}):
                with self.assertRaisesRegex(ConfigurationError, "E_EXTRACTOR_PREPARATION_PENDING"):
                    prepare_data_plane_task(
                        work, relays["primary_inventory"].parent,
                        worker_role="primary_inventory", config=config, sdk_activation={},
                    )
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            _, status = self._inventory_status(work)
            self.assertEqual(status["next_actions"], ["verify_independent_source_inventory"])

    def test_status_keeps_invalid_attestation_recovery_ahead_of_partial_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, _, _ = self._sealed_inventory_pair(Path(directory))
            (work / ".game-l10n/inventory-attestation.json").write_text("{", encoding="utf-8")
            code, status = self._inventory_status(work)
            self.assertEqual(code, 1)
            self.assertEqual(status["next_actions"], ["repair_invalid_workspace_state"])
            self.assertIsNone(status["data_plane_task"])

    def test_status_prioritizes_independent_sdk_mismatch_over_primary_partial_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, _, relays = self._sealed_inventory_pair(Path(directory))
            path = relays["independent_inventory"].with_name("receipt.json")
            receipt = json.loads(path.read_text(encoding="utf-8"))
            receipt["issue_codes"] = ["sdk_identity_mismatch"]
            path.write_text(json.dumps(receipt), encoding="utf-8")
            _, status = self._inventory_status(work)
            self.assertEqual(status["next_actions"], ["record_target_identity_evaluate_and_select_sdk"])


    def test_inventory_scope_failure_seals_as_repairable_cached_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game, sdk, work = root / "game", root / "sdk", root / "work"
            game.mkdir()
            sdk.mkdir()
            work.mkdir()
            establish_control_plane_entry(work)
            config = {
                "game_roots": [str(game)],
                "sdk_root": str(sdk),
                "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
            }
            output = work / ".game-l10n" / "data-plane" / "primary_inventory"
            prepared = prepare_data_plane_task(
                work,
                output,
                worker_role="primary_inventory",
                config=config,
                sdk_activation={},
            )
            relay = work / prepared["relay_path"]
            payload = json.loads(relay.read_text(encoding="utf-8"))
            artifact = work / "inputs" / "primary-inventory.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.source-inventory.v1",
                        "coverage": {"declared_complete": True},
                        "segments": [],
                    }
                ),
                encoding="utf-8",
            )
            envelope = work / payload["expected_envelope"]
            envelope.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.control-envelope.v2",
                        "worker_role": "primary_inventory",
                        "stage": "inventory",
                        "status": "success",
                        "target_ids": [],
                        "counts": {"total": 1, "success": 1},
                        "issue_codes": [],
                        "artifact_paths": ["inputs/primary-inventory.json"],
                        "queue_transitions": [],
                        "route_evidence_path": "",
                    }
                ),
                encoding="utf-8",
            )
            sealed = seal_data_plane_task(work, relay, worker_ended=True)
            self.assertEqual(
                sealed["issue_codes"], ["inventory_display_scope_required"]
            )
            repaired = prepare_data_plane_task(
                work,
                output,
                worker_role="primary_inventory",
                config=config,
                sdk_activation={},
            )
            task = json.loads((output / "task.json").read_text(encoding="utf-8"))
            self.assertEqual(repaired["status"], "ready")
            self.assertEqual(
                task["inventory_scope_repair"]["failure_code"],
                "inventory_display_scope_required",
            )
            self.assertIn(
                "reuse_cached_inventory_artifact_without_rescanning_source",
                task["requirements"],
            )
            self.assertTrue(artifact.is_file())

    def test_inventory_reconciler_operational_route_advancement_is_core_bound(self) -> None:
        def make_relay(root: Path, routes: list[str]):
            game, sdk = root / "game", root / "sdk"
            game.mkdir()
            sdk.mkdir()
            establish_control_plane_entry(root)
            configure_native_runtime(root, "codex", {})
            config = {
                "game_roots": [str(game)],
                "sdk_root": str(sdk),
                "routing": {"data_plane_routes": {"inventory_reconciler": routes}},
            }
            attestation = root / ".game-l10n" / "inventory-attestation.json"
            attestation.parent.mkdir(parents=True, exist_ok=True)
            attestation.write_text(
                json.dumps(
                    {
                        "primary_path": "inputs/primary-inventory.json",
                        "independent_path": "inputs/independent-inventory.json",
                        "primary_fingerprint": "p",
                        "independent_fingerprint": "i",
                        "primary_id_fingerprint": "ids",
                        "primary_identity_binding": {},
                        "identity_gate": {},
                        "gap_score": 1,
                        "issue_codes": [],
                        "reconciliation": {"generation": 0},
                    }
                ),
                encoding="utf-8",
            )
            output = root / ".game-l10n" / "data-plane" / "inventory_reconciler"

            def prepare():
                with patch("game_l10n.native_runtime.active_execution_environment_policy", return_value=approved_execution_policy()):
                    prepared = prepare_data_plane_task(
                        root,
                        output,
                        worker_role="inventory_reconciler",
                        config=config,
                        sdk_activation={},
                    )
                if prepared["status"] != "ready":
                    return prepared, None, None
                relay = root / prepared["relay_path"]
                bind_data_plane_launch(root, relay)
                payload = json.loads(relay.read_text(encoding="utf-8"))
                return prepared, relay, payload

            def start(relay: Path, payload: dict, worker_id: str):
                contract = payload["launch_contract"]
                record_native_launch(root, relay, event="capability-available")
                record_native_launch(
                    root,
                    relay,
                    event="worker-started",
                    task=contract["tasks"][0],
                    worker_id=worker_id,
                    model=contract["model"],
                )

            return config, output, prepare, start

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, output, prepare, start = make_relay(
                root, ["native-sol", "native-luna"]
            )
            first, relay, payload = prepare()
            self.assertEqual(payload["worker_route"], "native-sol")
            start(relay, payload, "worker-sol")
            recorded = record_data_plane_failure(
                root, relay, failure_code="execution_guard_denied"
            )
            self.assertEqual(recorded["workers"][0]["model"], "gpt-6.1-sol")
            self.assertTrue(recorded["cancellation_required"])
            still_live, _, _ = prepare()
            self.assertEqual(still_live["task_id"], first["task_id"])

            sealed = seal_data_plane_task(root, relay, worker_ended=True)
            self.assertEqual(
                sealed["issue_codes"],
                ["operational_error", "execution_guard_denied"],
            )
            _, relay, payload = prepare()
            self.assertEqual(payload["worker_route"], "native-luna")
            self.assertEqual(payload["launch_contract"]["model"], "gpt-6-luna")
            self.assertEqual(
                json.loads((root / payload["task_path"]).read_text(encoding="utf-8"))[
                    "data_plane_route_index"
                ],
                1,
            )
            start(relay, payload, "worker-luna-semantic-failure")
            semantic = seal_data_plane_task(root, relay, worker_ended=True)
            self.assertEqual(semantic["issue_codes"], ["task_incomplete"])
            _, relay, payload = prepare()
            self.assertEqual(payload["worker_route"], "native-luna")
            self.assertEqual(
                json.loads((root / payload["task_path"]).read_text(encoding="utf-8"))[
                    "data_plane_route_index"
                ],
                1,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, prepare, start = make_relay(root, ["native-sol"])
            _, relay, payload = prepare()
            start(relay, payload, "worker-no-successor")
            record_data_plane_failure(
                root, relay, failure_code="provider_operational_error"
            )
            seal_data_plane_task(root, relay, worker_ended=True)
            pending, relay, _ = prepare()
            self.assertEqual(pending["status"], "pending")
            self.assertIsNone(relay)

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

        self.assertEqual(relay_payload["schema"], "parr0t.data-plane-relay.v4")
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

    def test_ended_worker_rejects_unknown_issue_codes_without_echoing_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            game, sdk = work / "game", work / "sdk"
            game.mkdir()
            sdk.mkdir()
            (work / "game-l10n.json").write_text("{}", encoding="utf-8")
            establish_control_plane_entry(work)
            prepared = prepare_data_plane_task(
                work, work / ".game-l10n/data-plane/source_probe",
                worker_role="source_probe",
                config={
                    "game_roots": [str(game)], "sdk_root": str(sdk),
                    "sdk_selection": {"inventory_fingerprint": "fixture-inventory"},
                },
            )
            relay = work / prepared["relay_path"]
            envelope = work / json.loads(relay.read_bytes())["expected_envelope"]
            rejected = "synthetic private failure message"
            envelope.write_text(json.dumps({
                "schema": "parr0t.control-envelope.v2", "worker_role": "source_probe",
                "stage": "discovery", "status": "error", "target_ids": [],
                "counts": {"total": 1, "error": 1, "pending": 1},
                "issue_codes": ["task_incomplete", rejected], "queue_transitions": [],
                "artifact_paths": ["game-l10n.json"], "route_evidence_path": "",
            }), encoding="utf-8")
            sealed = seal_data_plane_task(work, relay, worker_ended=True)
            receipt = (work / sealed["receipt_path"]).read_text(encoding="utf-8")
            self.assertEqual(sealed["issue_codes"], ["malformed_response", "invalid_envelope_issue_codes"])
            self.assertEqual(json.loads(receipt)["artifact_paths"], [])
            self.assertNotIn(rejected, receipt)
            self.assertNotIn(rejected, json.dumps(sealed))

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
            payload.pop("execution_plan")
            for field in (
                "worker_route",
                "worker_model_family",
                "attempt",
                "sealed_failure_is_job_terminal",
            ):
                payload.pop(field)
            relay.write_text(json.dumps(payload), encoding="utf-8")

            sealed = seal_data_plane_task(work, relay, worker_ended=True)

        self.assertEqual(sealed["status"], "error")
        self.assertEqual(sealed["issue_codes"], ["task_incomplete"])

    def test_legacy_v2_data_plane_relay_remains_sealable_for_checkpoint_resume(self) -> None:
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
            payload["schema"] = "parr0t.data-plane-relay.v2"
            payload.pop("execution_plan")
            for field in (
                "worker_route",
                "worker_model_family",
                "attempt",
                "sealed_failure_is_job_terminal",
            ):
                payload.pop(field)
            contract = payload["relay_contract"]
            contract["schema"] = "parr0t.data-plane-relay-contract.v1"
            contract.pop("route_policy")
            contract.pop("failure_policy")
            contract.pop("private_artifact_policy")
            contract.pop("execution_policy")
            contract.pop("implicit_timeout_policy")
            contract.pop("completion_evidence_policy")
            relay.write_text(json.dumps(payload), encoding="utf-8")

            sealed = seal_data_plane_task(work, relay, worker_ended=True)

        self.assertEqual(sealed["status"], "error")
        self.assertEqual(sealed["issue_codes"], ["task_incomplete"])

    def test_legacy_v3_data_plane_relay_remains_sealable_for_checkpoint_resume(self) -> None:
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
            payload["schema"] = "parr0t.data-plane-relay.v3"
            payload.pop("execution_plan")
            contract = payload["relay_contract"]
            contract["schema"] = "parr0t.data-plane-relay-contract.v2"
            contract.pop("execution_policy")
            contract.pop("implicit_timeout_policy")
            contract.pop("completion_evidence_policy")
            relay.write_text(json.dumps(payload), encoding="utf-8")

            sealed = seal_data_plane_task(work, relay, worker_ended=True)

        self.assertEqual(sealed["status"], "error")
        self.assertEqual(sealed["schema"], "parr0t.data-plane-receipt.v1")
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
        self.assertEqual(sealed["schema"], "parr0t.data-plane-receipt.v2")
        self.assertEqual(sealed["worker_role"], "source_probe")
        self.assertEqual(sealed["stage"], "discovery")
        self.assertTrue(sealed["receipt_path"].endswith("receipt.json"))

    def test_new_attempt_cannot_reseal_a_previous_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game, work, sdk = (root / name for name in ("game", "work", "sdk"))
            for path in (game, work, sdk):
                path.mkdir()
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
            relay = work / first["relay_path"]
            first_relay = json.loads(relay.read_text(encoding="utf-8"))
            first_envelope = work / first_relay["expected_envelope"]
            envelope_body = json.dumps(
                {
                    "schema": "parr0t.control-envelope.v2",
                    "worker_role": "source_probe",
                    "stage": "discovery",
                    "status": "error",
                    "target_ids": [],
                    "counts": {"total": 1, "success": 0, "error": 1},
                    "issue_codes": ["provider_error"],
                    "artifact_paths": ["game-l10n.json"],
                    "queue_transitions": [],
                    "route_evidence_path": "",
                }
            )
            first_envelope.write_text(envelope_body, encoding="utf-8")
            seal_data_plane_task(work, relay)

            second = prepare_data_plane_task(
                work, output, worker_role="source_probe", config=config
            )
            second_relay = json.loads(relay.read_text(encoding="utf-8"))
            second_envelope = work / second_relay["expected_envelope"]

            self.assertNotEqual(first["task_id"], second["task_id"])
            self.assertNotEqual(first_envelope, second_envelope)
            self.assertEqual(first_envelope.read_text(encoding="utf-8"), envelope_body)
            self.assertFalse(second_envelope.exists())
            missing = seal_data_plane_task(work, relay, worker_ended=True)
            self.assertEqual(missing["issue_codes"], ["task_incomplete"])
            self.assertEqual(first_envelope.read_text(encoding="utf-8"), envelope_body)

            second_envelope.write_text(envelope_body, encoding="utf-8")
            self.assertEqual(
                seal_data_plane_task(work, relay)["issue_codes"],
                ["provider_error"],
            )

    def test_reprepared_open_data_plane_task_reuses_the_bound_attempt(self) -> None:
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

        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(task["attempt"], 1)

    def test_rotation_rebinds_open_data_plane_task_without_losing_attempt(self) -> None:
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
            first_relay = json.loads(
                (work / first["relay_path"]).read_text(encoding="utf-8")
            )
            first_envelope = work / first_relay["expected_envelope"]
            first_envelope.write_text("{prior output", encoding="utf-8")
            record_control_plane_leak(work, "parent_opened_data_plane")
            acknowledge_control_plane_rotation(work, work / CONTROL_HANDOFF_PATH)
            establish_control_plane_entry(work)

            rebound = prepare_data_plane_task(
                work, output, worker_role="source_probe", config=config
            )
            rebound_path = work / rebound["relay_path"]
            rebound_relay = json.loads(
                (work / rebound["relay_path"]).read_text(encoding="utf-8")
            )
            task = json.loads((work / rebound_relay["task_path"]).read_text(encoding="utf-8"))
            self.assertNotEqual(rebound_path, work / first["relay_path"])
            self.assertEqual(
                first_envelope.read_text(encoding="utf-8"), "{prior output"
            )
            sealed = seal_data_plane_task(
                work, work / rebound["relay_path"], worker_ended=True
            )
            self.assertEqual(sealed["issue_codes"], ["task_incomplete"])

        self.assertNotEqual(first["task_id"], rebound["task_id"])
        self.assertEqual(rebound["control_generation"], 2)
        self.assertEqual(task["control_generation"], 2)
        self.assertEqual(task["attempt"], 1)
        self.assertNotEqual(
            first_relay["expected_envelope"], rebound_relay["expected_envelope"]
        )

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
                "routing": {
                    "context_analyzers": ["native-sol"],
                    "max_workers": 8,
                },
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
            private_task = json.loads(
                (work / relay_payload["task_path"]).read_text(encoding="utf-8")
            )
            context_preflight = json.loads(
                (work / private_task["input_context_preflight"]).read_text(
                    encoding="utf-8"
                )
            )
            task_path = work / relay_payload["task_path"]
            claimed = claim_context_chunk(work, task_path, slot=1)
            claim_path = work / claimed["claim_path"]
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            # Native context workers get the same locked project facts as the API analyzer.
            self.assertIn("target_language", claim["localization_inputs"]["project"])
            result_path = work / ".game-l10n" / "context-progress" / "result.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.context-chunk-result.v1",
                        "claim_id": claim["claim_id"],
                        "status": "success",
                        "context_part": {
                            "status": "success",
                            "reason": "",
                            "global_style_rules": ["Use consistent UI terms."],
                            "terminology_rules": [],
                            "reference_cues": [],
                            "functional_summary": "Fixture screen flow.",
                            "screens": [
                                {
                                    "screen": "fixture",
                                    "purpose": "Fixture purpose.",
                                    "actions": [],
                                    "states": [],
                                    "constraints": [],
                                    "evidence_line_ids": ["fixture-1"],
                                }
                            ],
                            "features": [],
                        },
                        "issue_code": "",
                        "content_provenance": context_result_provenance(claim),
                    }
                ),
                encoding="utf-8",
            )
            committed = commit_context_fragment(
                work, task_path, claim_path, result_path
            )

            slots = relay_payload["execution_plan"]["required_worker_slots"]
            record_data_plane_relay_event(
                work, relay, event="worker-started", slot=slots[0]
            )
            with self.assertRaisesRegex(ConfigurationError, "finalizer requires"):
                record_data_plane_relay_event(work, relay, event="finalizer-started")
            with self.assertRaisesRegex(ConfigurationError, "EXECUTION_INCOMPLETE"):
                seal_data_plane_task(work, relay)
            for slot in slots:
                record_data_plane_relay_event(
                    work, relay, event="worker-ended", slot=slot
                )
            record_data_plane_relay_event(work, relay, event="finalizer-started")
            finalized = finalize_context_fragments(work, task_path)
            record_data_plane_relay_event(work, relay, event="finalizer-ended")

            sealed = seal_data_plane_task(work, relay)
            state = json.loads(
                (work / ".game-l10n" / "context-state.json").read_text(
                    encoding="utf-8"
                )
            )
            resumed = prepare_data_plane_task(
                work,
                work / ".game-l10n" / "data-plane" / "context_analyzer",
                worker_role="context_analyzer",
                config=config,
                sdk_activation={"status": "active"},
            )
            resumed_relay_path = work / resumed["relay_path"]
            resumed_relay = json.loads(
                resumed_relay_path.read_text(encoding="utf-8")
            )
            resumed_task_path = work / resumed_relay["task_path"]
            record_data_plane_relay_event(
                work, resumed_relay_path, event="finalizer-started"
            )
            resumed_finalized = finalize_context_fragments(
                work, resumed_task_path
            )
            record_data_plane_relay_event(
                work, resumed_relay_path, event="finalizer-ended"
            )
            resumed_sealed = seal_data_plane_task(work, resumed_relay_path)

        self.assertEqual(sealed["status"], "success")
        self.assertEqual(sealed["schema"], "parr0t.data-plane-receipt.v2")
        self.assertEqual(committed["status"], "committed")
        self.assertEqual(finalized["status"], "success")
        self.assertEqual(relay_payload["max_workers"], 1)
        self.assertFalse(relay_payload["worker_delegation_allowed"])
        self.assertEqual(
            relay_payload["execution_plan"]["required_worker_slots"],
            [1],
        )
        self.assertEqual(
            relay_payload["execution_plan"]["implicit_timeout_policy"],
            "forbidden",
        )
        self.assertTrue(sealed["execution"]["complete"])
        self.assertEqual(sealed["execution"]["terminal_workers"], 1)
        self.assertTrue(sealed["execution"]["finalizer_terminal"])
        self.assertEqual(private_task["context_parallelism"]["max_workers"], 1)
        self.assertEqual(
            private_task["context_protocol"]["result_schema"],
            "parr0t.context-chunk-result.v1",
        )
        self.assertEqual(
            private_task["context_chunk_measurement"],
            "serialized_payload_chars_including_ids_source_and_metadata",
        )
        self.assertEqual(
            context_preflight["schema"], "parr0t.context-preflight.v1"
        )
        self.assertEqual(
            context_preflight["counts"]["semantic_required"], 1
        )
        self.assertTrue(
            private_task["context_preflight_contract"][
                "buttons_are_ui_but_never_prove_deterministic_skip"
            ]
        )
        self.assertEqual(
            private_task["context_parallelism"]["shared_artifact_writer"],
            "single_fresh_finalizer_after_wave",
        )
        self.assertEqual(state["status"], "success")
        self.assertEqual(state["schema"], "parr0t.context-state.v2")
        self.assertEqual(
            state["attestation"]["analyzer_routes"],
            [{"name": "native-sol", "type": "native_collaboration"}],
        )
        self.assertEqual(state["execution"]["parts"][0]["provider"], "native-sol")
        self.assertEqual(
            state["source_inventory_fingerprint"], verified["primary_fingerprint"]
        )
        self.assertTrue(state["attestation"]["functional_coverage"]["complete"])
        self.assertEqual(resumed_relay["max_workers"], 0)
        self.assertEqual(
            resumed_relay["execution_plan"]["strategy"],
            "context_finalizer_only",
        )
        self.assertEqual(
            resumed_relay["execution_plan"]["required_worker_slots"], []
        )
        self.assertEqual(resumed_finalized["status"], "success")
        self.assertEqual(resumed_sealed["execution"]["required_workers"], 0)
        self.assertTrue(resumed_sealed["execution"]["complete"])

    def test_context_recovery_rotates_routes_and_reduces_private_chunk_ceiling(self) -> None:
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
            save_inventory_attestation(work, primary, independent)
            config = {
                "game_roots": [str(game)],
                "sdk_root": str(sdk),
                "sdk_selection": {"inventory_fingerprint": "fixture-sdk"},
                "files": {"context_pack": "context/context-pack.json"},
                "project": {"context_mode": "auto"},
                "routing": {
                    "context_analyzers": ["native-sol", "native-luna"],
                    "context_chunk_chars": 30000,
                    "context_max_workers": 6,
                },
            }
            output = work / ".game-l10n" / "data-plane" / "context_analyzer"
            observed = []
            for _ in range(3):
                prepared = prepare_data_plane_task(
                    work,
                    output,
                    worker_role="context_analyzer",
                    config=config,
                    sdk_activation={"status": "active"},
                )
                relay = json.loads(
                    (work / prepared["relay_path"]).read_text(encoding="utf-8")
                )
                task = json.loads((output / "task.json").read_text(encoding="utf-8"))
                observed.append(
                    (
                        relay["worker_route"],
                        relay["worker_model_family"],
                        relay["sealed_failure_is_job_terminal"],
                        task["context_chunk_chars"],
                    )
                )
                sealed = seal_data_plane_task(
                    work,
                    work / prepared["relay_path"],
                    worker_ended=True,
                )
                self.assertEqual(
                    sealed["issue_codes"], ["relay_execution_incomplete"]
                )
                execution = sealed["execution"]
                self.assertEqual(execution["unstarted_slots"], [])
                self.assertEqual(execution["terminal_unstarted_slots"], [1])
                self.assertEqual(execution["started_workers"], 0)
                self.assertTrue(execution["finalizer_skipped"])
                self.assertTrue(execution["complete"])
            receipt_path = output / "receipt.json"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["counts"] = {
                "total": 10,
                "success": 3,
                "error": 0,
                "pending": 7,
            }
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            prepared = prepare_data_plane_task(
                work,
                output,
                worker_role="context_analyzer",
                config=config,
                sdk_activation={"status": "active"},
            )
            relay = json.loads(
                (work / prepared["relay_path"]).read_text(encoding="utf-8")
            )
            task = json.loads((output / "task.json").read_text(encoding="utf-8"))
            observed.append(
                (
                    relay["worker_route"],
                    relay["worker_model_family"],
                    relay["sealed_failure_is_job_terminal"],
                    task["context_chunk_chars"],
                )
            )

        self.assertEqual(
            observed,
            [
                ("native-sol", "sol", False, 30000),
                ("native-luna", "luna", False, 30000),
                ("native-sol", "sol", False, 15000),
                ("native-luna", "luna", False, 30000),
            ],
        )

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
        self.assertTrue(contract["recoverable_failure_continuation_allowed"])
        self.assertTrue(contract["user_pause_or_stop_resume_requires_explicit_request"])
        self.assertTrue(contract["changed_scope_requires_new_user_approval"])
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
            contract["recoverable_continuation_requirements"],
            [
                "original_scope_authorization",
                "regenerated_validated_manifest",
                "respect_user_pause_or_stop",
                "honest_optional_budget_accounting",
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

    def test_large_envelopes_preserve_every_target_and_still_validate_opaque_unique_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._valid_translation_envelope(root)
            targets = [f"fixture-{index}" for index in range(2001)]
            payload["target_ids"] = targets
            payload["counts"] = {"total": len(targets), "success": 0, "fallback_required": len(targets)}
            payload["queue_transitions"][0]["count"] = len(targets)
            envelope = self._write_envelope(root, payload)
            result = validate_control_envelope(root, envelope)
            self.assertEqual(result["target_ids"], [control_target_id(target) for target in targets])
            self.assertEqual(result["counts"]["total"], len(targets))
            for invalid in ("not-a-list", [*targets, targets[-1]], [*targets, 1], [*targets, "not opaque"], [*targets, ""]):
                with self.subTest(invalid_tail=invalid[-1]):
                    payload["target_ids"] = invalid
                    envelope = self._write_envelope(root, payload)
                    with self.assertRaisesRegex(ConfigurationError, "target_ids"):
                        validate_control_envelope(root, envelope)

    def test_image_stage_envelopes_validate_categorical_results_without_losing_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for role, stage, issues in (
                ("image_ui_inventory", "image_inventory", []),
                ("image_ui_inventory", "image_inventory", ["image_scope_unexamined"]),
                ("image_localizer", "image_localization", []),
                ("image_localizer", "image_localization", ["placeholder_mismatch"]),
            ):
                with self.subTest(role=role, issues=issues):
                    payload = self._valid_translation_envelope(root)
                    payload.update({
                        "worker_role": role, "stage": stage,
                        "status": "partial" if issues else "success",
                        "issue_codes": issues, "queue_transitions": [],
                    })
                    payload["counts"] = {"total": 1, "success": 0 if issues else 1}
                    envelope = self._write_envelope(root, payload)
                    result = validate_control_envelope(root, envelope, expected_role=role, expected_stage=stage)
                    self.assertEqual(result["status"], payload["status"])
                    self.assertEqual(result["issue_codes"], issues)
                    payload["issue_codes"] = ["private_image_error_prose"]
                    envelope = self._write_envelope(root, payload)
                    with self.assertRaisesRegex(ConfigurationError, "issue_codes"):
                        validate_control_envelope(root, envelope)

    def test_envelope_rejects_artifact_names_that_carry_free_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = self._valid_translation_envelope(root)
            leaked = root / ".game-l10n" / "data-plane" / "The hero said hello.json"
            leaked.write_text("{}", encoding="utf-8")
            payload["artifact_paths"] = [leaked.relative_to(root).as_posix()]
            envelope = self._write_envelope(root, payload)
            with self.assertRaisesRegex(ConfigurationError, "unsafe characters"):
                validate_control_envelope(
                    root, envelope, expected_role="translator", expected_stage="translation"
                )

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

    def test_rotation_acknowledgment_is_retryable_when_receipt_removal_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / CONTROL_HANDOFF_PATH
            record_control_plane_leak(root, "source_or_candidate_received")

            with patch.object(
                Path, "unlink", side_effect=PermissionError("synthetic receipt lock")
            ):
                with self.assertRaises(PermissionError):
                    acknowledge_control_plane_rotation(root, receipt)

            self.assertEqual(control_plane_state(root)["status"], "rotation_required")
            self.assertTrue(receipt.exists())
            resumed = acknowledge_control_plane_rotation(root, receipt)

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["generation"], 2)

    def test_rotation_acknowledgment_regenerates_receipt_after_late_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / CONTROL_HANDOFF_PATH
            record_control_plane_leak(root, "source_or_candidate_received")

            with patch(
                "game_l10n.control_plane._reset_native_control_observations",
                side_effect=OSError("synthetic observation reset failure"),
            ):
                with self.assertRaises(OSError):
                    acknowledge_control_plane_rotation(root, receipt)

            self.assertEqual(control_plane_state(root)["status"], "rotation_required")
            self.assertFalse(receipt.exists())
            resumed = acknowledge_control_plane_rotation(root, receipt)

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["generation"], 2)

    def test_interrupted_second_leak_does_not_wedge_rotation(self) -> None:
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / CONTROL_HANDOFF_PATH
            record_control_plane_leak(root, "source_or_candidate_received")
            acknowledge_control_plane_rotation(root, receipt)
            self.assertFalse(receipt.exists())
            # Simulate a stale receipt plus a crash between the state and handoff writes.
            record_control_plane_leak(root, "source_or_candidate_received")
            stale = receipt.read_text(encoding="utf-8")
            acknowledge_control_plane_rotation(root, receipt)
            receipt.write_text(stale, encoding="utf-8")
            with mock.patch("game_l10n.control_plane._write_control_plane_handoff"):
                record_control_plane_leak(root, "source_or_candidate_received")
            record_control_plane_leak(root, "source_or_candidate_received")
            resumed = acknowledge_control_plane_rotation(root, receipt)

        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["generation"], 4)

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
