from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from game_l10n.control_plane import validate_control_envelope
from game_l10n.content_source_policy import create_content_source_grant
from game_l10n.data_plane_task import (
    _expected_context_pack_from_fragments,
    prepare_data_plane_task,
    record_data_plane_failure,
    record_data_plane_relay_event,
    seal_data_plane_task,
)
from game_l10n.errors import ConfigurationError
from game_l10n.native_runtime import bind_data_plane_launch, configure_native_runtime, record_native_launch
from game_l10n.omp_isolation import ISOLATION_ENV
from game_l10n.store import stable_hash
from tests import test_context_claim_binding as context_fixture
from tests.omp_isolation_fixture import bind_omp_isolation
from tests.execution_policy_fixture import approved_execution_policy


class ContextWorkerExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {ISOLATION_ENV: ""})
        environment.start()
        self.addCleanup(environment.stop)
        context_fixture.ContextClaimBindingTests.setUp(self)
        self.reload_task()
        self.empty_path = self.work / "synthetic empty PATH"
        self.empty_path.mkdir()
        self.process_environment = {**os.environ, "PATH": str(self.empty_path)}
        self.assertIsNone(shutil.which("game-l10n", path=str(self.empty_path)))

    def reload_task(self) -> None:
        self.task = json.loads(self.task_path.read_text(encoding="utf-8"))
        self.execution = self.task["worker_execution"]

    def prepare_snapshot(self, name: str) -> dict:
        config = {
            "game_roots": [str(self.work.parent / "game")],
            "sdk_root": str(self.work.parent / "sdk"),
            "sdk_selection": {"inventory_fingerprint": "synthetic-sdk"},
            "files": {"context_pack": "context/context-pack.json"},
            "project": {"context_mode": "functional"},
            "routing": {
                "context_analyzers": ["native-sol"],
                "max_workers": 2,
                "context_chunk_chars": 2048,
            },
        }
        self.output = self.work / ".game-l10n/data-plane" / name
        self.config = config
        prepared = prepare_data_plane_task(
            self.work, self.output, worker_role="context_analyzer",
            config=config, sdk_activation={"status": "active"},
        )
        self.task_path = self.output / "task.json"
        self.reload_task()
        return prepared

    def argv(self, command: str, **bindings: object) -> list[str]:
        replacements = {f"<{name.replace('_', '-')}>": str(value)
                        for name, value in bindings.items()}
        self.assertLessEqual(set(replacements), set(self.execution["placeholders"]))
        argv = [replacements.get(argument, argument)
                for argument in self.execution["command_argv"][command]]
        self.assertFalse(set(argv) & set(self.execution["placeholders"]))
        return argv

    def run_argv(self, argv: list[str], *, success: bool = True) -> dict:
        completed = subprocess.run(
            argv, cwd=self.empty_path, env=self.process_environment,
            capture_output=True, encoding="utf-8", check=False,
        )
        if not success:
            self.assertNotEqual(completed.returncode, 0, completed.stdout)
            self.assertIn("E_CONTEXT_", completed.stderr)
            return {}
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def run_command(self, command: str, *, success: bool = True, **bindings: object) -> dict:
        return self.run_argv(self.argv(command, **bindings), success=success)

    def claim(self, slot: int) -> tuple[Path, dict]:
        claimed = self.run_command("claim-context-chunk", slot=slot)
        self.assertEqual(claimed["status"], "claimed")
        path = self.work / claimed["claim_path"]
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((payload["slot"], payload["task_id"]), (slot, self.task["task_id"]))
        return path, payload

    def result(self, claim: dict) -> Path:
        original = context_fixture.ContextClaimBindingTests.result(self, claim)
        result = original.with_name(f"synthetic result slot {claim['slot']}.json")
        original.replace(result)
        return result

    def assert_round_trip(self) -> None:
        self.assertEqual(self.execution["argv_convention"], "complete_executable_argv")
        self.assertEqual(self.execution["workspace_root"], str(self.work.resolve()))
        self.assertEqual(self.execution["task_path"], str(self.task_path.resolve()))
        for command in self.execution["command_argv"].values():
            self.assertEqual(command[:5], [sys.executable, "-X", "utf8", "-I", "-B"])
            self.assertTrue(Path(command[5]).is_absolute())
            self.assertEqual(Path(command[5]).name, "native-python.py")
            self.assertNotIn("--relay", command)
        unsigned = {key: value for key, value in self.task.items() if key != "task_id"}
        self.assertEqual(stable_hash(unsigned), self.task["task_id"])
        self.assertEqual(self.execution["roles"], {
            "slot_worker": ["claim-context-chunk", "commit-context-fragment"],
            "finalizer_only": ["finalize-context-fragments"],
        })
        first_path, first = self.claim(1)
        self.assertEqual(self.claim(1), (first_path, first))
        second_path, second = self.claim(2)
        self.assertNotEqual(first["binding_id"], second["binding_id"])
        for path, claim in ((second_path, second), (first_path, first)):
            committed = self.run_command(
                "commit-context-fragment", claim_path=path, result_path=self.result(claim),
            )
            self.assertEqual((committed["status"], committed["slot"]), ("committed", claim["slot"]))
        exhausted = self.run_command("claim-context-chunk", slot=1)
        self.assertEqual((exhausted["status"], exhausted["success"], exhausted["pending"]),
                         ("exhausted", 2, 0))
        finalized = self.run_command("finalize-context-fragments")
        self.assertEqual((finalized["status"], finalized["success"], finalized["pending"]),
                         ("success", 2, 0))
        expected, complete, _ = _expected_context_pack_from_fragments(self.work, self.task)
        self.assertEqual([chunk["source_index"] for chunk, _, _ in complete], [0, 1])
        actual = json.loads((self.work / self.task["output_artifact"]).read_text(encoding="utf-8"))
        self.assertEqual(actual, json.loads(json.dumps(asdict(expected))))
        envelope = validate_control_envelope(
            self.work, self.work / self.task["output_envelope"],
            expected_role="context_analyzer", expected_stage="context",
        )
        self.assertEqual(envelope["status"], "success")
        self.assertEqual(envelope["artifact_paths"], [self.task["output_artifact"]])
        finalized_bytes = (self.work / self.task["output_artifact"]).read_bytes()
        self.run_command("finalize-context-fragments")
        self.assertEqual((self.work / self.task["output_artifact"]).read_bytes(), finalized_bytes)

    def test_completed_slot_does_not_wait_for_sibling_admission(self) -> None:
        relay = self.output / "relay.json"
        slots = json.loads(relay.read_text(encoding="utf-8"))["execution_plan"]["required_worker_slots"]
        first, second = slots
        record_data_plane_relay_event(self.work, relay, event="worker-started", slot=first)
        path, claim = self.claim(first)
        self.run_command("commit-context-fragment", claim_path=path, result_path=self.result(claim))
        ended = record_data_plane_relay_event(self.work, relay, event="worker-ended", slot=first)
        self.assertEqual(ended["execution"]["terminal_workers"], 1)
        self.assertEqual(ended["execution"]["unstarted_slots"], [second])
        with self.assertRaisesRegex(ConfigurationError, "finalizer requires"):
            record_data_plane_relay_event(self.work, relay, event="finalizer-started")
        with self.assertRaisesRegex(ConfigurationError, "EXECUTION_INCOMPLETE"):
            seal_data_plane_task(self.work, relay)

        record_data_plane_relay_event(self.work, relay, event="worker-started", slot=second)
        path, claim = self.claim(second)
        self.run_command("commit-context-fragment", claim_path=path, result_path=self.result(claim))
        record_data_plane_relay_event(self.work, relay, event="worker-ended", slot=second)
        record_data_plane_relay_event(self.work, relay, event="finalizer-started")
        self.run_command("finalize-context-fragments")
        record_data_plane_relay_event(self.work, relay, event="finalizer-ended")
        sealed = seal_data_plane_task(self.work, relay)
        self.assertEqual(sealed["status"], "success")
        self.assertTrue(sealed["execution"]["complete"])
        self.assertEqual(sealed["counts"]["success"], 2)

    def test_partial_admission_failure_ends_without_starting_missing_slots_or_finalizer(self) -> None:
        bind_omp_isolation(self, self.work)
        configure_native_runtime(self.work, "omp", {"native-sol": "fixture/sol"}, omp_isolation="guarded")
        self.prepare_snapshot("partial admission")
        relay = self.output / "relay.json"
        bind_data_plane_launch(self.work, relay)
        contract = json.loads(relay.read_text(encoding="utf-8"))["launch_contract"]
        record_native_launch(self.work, relay, event="capability-available")
        record_native_launch(
            self.work, relay, event="worker-started", task=contract["tasks"][0],
            worker_id="synthetic-started-child", model=contract["model"],
        )
        record_data_plane_relay_event(self.work, relay, event="worker-started", slot=1)
        record_data_plane_failure(self.work, relay, failure_code="execution_guard_denied")
        with self.assertRaisesRegex(ConfigurationError, "actual started workers must end"):
            seal_data_plane_task(self.work, relay, worker_ended=True)
        record_data_plane_relay_event(self.work, relay, event="worker-ended", slot=1)
        fingerprint = hashlib.sha256(relay.read_bytes()).hexdigest()
        key = hashlib.sha256(f"{fingerprint}\0{contract['tasks'][0]}".encode()).hexdigest()
        admission = self.work / ".game-l10n/native/omp-launch-admissions" / f"{key}.json"
        admission.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": "parr0t.omp-native-admission.v1", "relay_sha256": fingerprint,
            "task": contract["tasks"][0], "state": "started",
        }
        admission.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "WORKER_ACTIVE"):
            seal_data_plane_task(self.work, relay, worker_ended=True)
        self.assertEqual(json.loads(admission.read_text(encoding="utf-8"))["state"], "started")
        self.assertFalse((self.output / "receipt.json").exists())
        admission.write_text(json.dumps({**record, "state": "ended"}), encoding="utf-8")
        sealed = seal_data_plane_task(self.work, relay, worker_ended=True)
        self.assertEqual(sealed["issue_codes"], ["operational_error", "execution_guard_denied"])
        self.assertEqual(sealed["execution"]["started_workers"], 1)
        self.assertEqual(sealed["execution"]["terminal_workers"], 1)
        self.assertEqual(sealed["execution"]["terminal_unstarted_slots"], [2])
        self.assertFalse(sealed["execution"]["finalizer_started"])
        self.assertTrue(sealed["execution"]["finalizer_skipped"])
        self.assertTrue(sealed["execution"]["complete"])

    def test_denied_before_any_worker_start_seals_without_fabricated_children(self) -> None:
        configure_native_runtime(self.work, "codex", {"native-sol": "gpt-6.1-sol"})
        self.prepare_snapshot("denied admission")
        relay = self.output / "relay.json"
        bind_data_plane_launch(self.work, relay)
        recorded = record_data_plane_failure(self.work, relay, failure_code="execution_guard_denied")
        self.assertEqual(recorded["workers"], [])
        self.assertFalse(recorded["cancellation_required"])
        sealed = seal_data_plane_task(self.work, relay, worker_ended=True)
        self.assertEqual(sealed["execution"]["started_workers"], 0)
        self.assertEqual(sealed["execution"]["terminal_workers"], 0)
        self.assertEqual(sealed["execution"]["terminal_unstarted_slots"], [1, 2])
        self.assertTrue(sealed["execution"]["complete"])

    def test_operational_successor_requires_the_original_bound_failure_category(self) -> None:
        bind_omp_isolation(self, self.work)
        configure_native_runtime(self.work, "omp", {
            "native-sol": "fixture/sol", "native-approved-successor": "fixture/successor",
        }, omp_isolation="guarded")
        narrow = approved_execution_policy("provider_operational_error")
        for failure, allowed in (("execution_guard_denied", False), ("provider_operational_error", True)):
            with self.subTest(failure=failure):
                with patch("game_l10n.native_runtime.active_execution_environment_policy", return_value=narrow):
                    self.prepare_snapshot(f"bound-policy-{failure}")
                original_task = self.task_path.read_bytes()
                relay = self.output / "relay.json"
                bind_data_plane_launch(self.work, relay)
                if allowed:
                    contract = json.loads(relay.read_text(encoding="utf-8"))["launch_contract"]
                    record_native_launch(self.work, relay, event="capability-available")
                    record_native_launch(
                        self.work, relay, event="worker-started", task=contract["tasks"][0],
                        worker_id="synthetic-provider-failure-child", model=contract["model"],
                    )
                    record_data_plane_relay_event(self.work, relay, event="worker-started", slot=1)
                record_data_plane_failure(self.work, relay, failure_code=failure)
                if allowed:
                    with self.assertRaisesRegex(ConfigurationError, "actual started workers must end"):
                        seal_data_plane_task(self.work, relay, worker_ended=True)
                    record_data_plane_relay_event(self.work, relay, event="worker-ended", slot=1)
                sealed = seal_data_plane_task(self.work, relay, worker_ended=True)
                self.assertEqual(sealed["execution"]["started_workers"], int(allowed))
                self.assertEqual(sealed["execution"]["terminal_workers"], int(allowed))
                self.assertTrue(sealed["execution"]["complete"])
                self.config["routing"]["context_analyzers"] = ["native-sol", "native-approved-successor"]
                with patch("game_l10n.native_runtime.active_execution_environment_policy", return_value=approved_execution_policy()):
                    prepared = prepare_data_plane_task(
                        self.work, self.output, worker_role="context_analyzer",
                        config=self.config, sdk_activation={"status": "active"},
                    )
                if allowed:
                    self.assertEqual(prepared["status"], "ready")
                    successor = json.loads(self.task_path.read_text(encoding="utf-8"))
                    self.assertEqual(successor["context_route"], "native-approved-successor")
                    self.assertNotEqual(successor["task_id"], self.task["task_id"])
                    self.assertEqual(successor["attempt"], self.task["attempt"])
                    archive = self.output / "history" / self.task["task_id"] / "task.json"
                    self.assertEqual(archive.read_bytes(), original_task)
                else:
                    self.assertEqual(prepared["status"], "pending")
                    self.assertEqual(prepared["next_action"], "repair_native_runtime")
                    self.assertFalse(prepared["dispatch_allowed"])
                    self.assertEqual(self.task_path.read_bytes(), original_task)

    def test_minimum_context_failure_requires_repair_but_changed_capacity_can_retry(self) -> None:
        relay = self.output / "relay.json"
        first_task = self.task_path.read_bytes()
        sealed = seal_data_plane_task(self.work, relay, worker_ended=True)
        self.assertTrue(sealed["execution"]["complete"])
        prepared = self.prepare_snapshot("context_analyzer")
        self.assertEqual(prepared["status"], "pending")
        self.assertEqual(prepared["next_action"], "repair_context_inputs")
        self.assertFalse(prepared["dispatch_allowed"])
        self.assertEqual(self.task_path.read_bytes(), first_task)
        self.config["routing"]["context_chunk_chars"] = 4096
        retried = prepare_data_plane_task(
            self.work, self.output, worker_role="context_analyzer",
            config=self.config, sdk_activation={"status": "active"},
        )
        self.assertEqual(retried["status"], "ready")
        self.assertNotEqual(self.task_path.read_bytes(), first_task)
        archive = self.output / "history" / self.task["task_id"] / "task.json"
        self.assertEqual(archive.read_bytes(), first_task)

    def test_minimum_context_failure_uses_untried_approved_route(self) -> None:
        seal_data_plane_task(self.work, self.output / "relay.json", worker_ended=True)
        self.prepare_snapshot("context_analyzer")
        self.config["routing"]["context_analyzers"] = ["native-sol", "native-approved-successor"]
        retried = prepare_data_plane_task(
            self.work, self.output, worker_role="context_analyzer",
            config=self.config, sdk_activation={"status": "active"},
        )
        self.assertEqual(retried["status"], "ready")
        task = json.loads(self.task_path.read_text(encoding="utf-8"))
        self.assertEqual(task["context_route"], "native-approved-successor")

    def test_route_and_grant_revision_reuses_successful_context_fragments(self) -> None:
        self.prepare_snapshot("scope-change")
        claim_path, claim = self.claim(1)
        self.run_command("commit-context-fragment", claim_path=claim_path, result_path=self.result(claim))
        fragments = {
            path: path.read_bytes()
            for path in (self.work / ".game-l10n/context-progress/fragments").rglob("*.json")
        }
        sealed = seal_data_plane_task(self.work, self.output / "relay.json", worker_ended=True)
        self.assertEqual(sealed["counts"]["success"], 1)
        grant, _ = create_content_source_grant(
            self.work, authority="explicit_user", scope_id="context-retry-v2",
            task_kinds=["localization_context"], routes=["data-plane:context_analyzer"],
            external_sources=["https://example.invalid/exact-context-reference"], user_confirmed=True,
        )
        self.config["routing"]["context_analyzers"] = ["native-sol", "native-approved-successor"]
        prepared = prepare_data_plane_task(
            self.work, self.output, worker_role="context_analyzer", config=self.config,
            sdk_activation={"status": "active"}, content_source_grant=grant,
            content_source_scope="context-retry-v2",
        )
        self.assertEqual(prepared["status"], "ready")
        task = json.loads(self.task_path.read_text(encoding="utf-8"))
        self.assertEqual(task["context_route"], "native-approved-successor")
        self.assertEqual(task["context_parallelism"]["completed_chunks"], 1)
        self.assertEqual(task["context_parallelism"]["pending_chunks"], 1)
        self.assertEqual(task["context_parallelism"]["max_workers"], 1)
        manifest = json.loads((self.work / task["input_context_manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["chunks"][0]["binding"]["semantic_route"], "native-sol")
        self.assertEqual(manifest["chunks"][1]["binding"]["semantic_route"], "native-approved-successor")
        self.assertEqual(len(task["context_source_policies"]), 2)
        for path, original in fragments.items():
            self.assertEqual(path.read_bytes(), original)

    def test_late_admission_keeps_original_failure_evidence_and_truthful_start_counts(self) -> None:
        configure_native_runtime(self.work, "codex", {"native-sol": "gpt-6.1-sol"})
        self.prepare_snapshot("late-admission")
        relay = self.output / "relay.json"
        bind_data_plane_launch(self.work, relay)
        record_data_plane_failure(self.work, relay, failure_code="execution_guard_denied")
        failure_path = next(self.output.glob("operational-failure-*.json"))
        self.assertEqual(json.loads(failure_path.read_text(encoding="utf-8"))["workers"], [])
        contract = json.loads(relay.read_text(encoding="utf-8"))["launch_contract"]
        name = contract["tasks"][0]
        record_native_launch(self.work, relay, event="capability-available")
        record_native_launch(
            self.work, relay, event="worker-started", task=name,
            worker_id="synthetic-late-admitted-child", model=contract["model"],
        )
        fingerprint = hashlib.sha256(relay.read_bytes()).hexdigest()
        key = hashlib.sha256(f"{fingerprint}\0{name}".encode()).hexdigest()
        admission = self.work / ".game-l10n/native/omp-launch-admissions" / f"{key}.json"
        admission.parent.mkdir(parents=True, exist_ok=True)
        admission.write_text(json.dumps({
            "schema": "parr0t.omp-native-admission.v1", "relay_sha256": fingerprint,
            "task": name, "state": "ended",
        }), encoding="utf-8")
        sealed = seal_data_plane_task(self.work, relay, worker_ended=True)
        self.assertEqual(sealed["execution"]["started_workers"], 1)
        self.assertEqual(sealed["execution"]["terminal_workers"], 1)
        self.assertEqual(sealed["execution"]["terminal_unstarted_slots"], [2])
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        self.assertEqual(failure["workers"], [])
        self.assertEqual(failure["terminal_workers"][0]["worker_id"], "synthetic-late-admitted-child")
        self.assertTrue(failure["terminal"])

    def test_ordinary_worker_argv_runs_without_cli_on_path(self) -> None:
        self.assert_round_trip()

    def test_prompt_worker_argv_runs_without_guard_or_relay_launch(self) -> None:
        configured = configure_native_runtime(
            self.work, "omp", {"native-sol": "fixture/exact-sol"}, omp_isolation="prompt",
        )
        self.prepare_snapshot("synthetic prompt context")
        self.assertEqual(configured["models"]["native-sol"], "fixture/exact-sol")
        self.assertEqual(self.task["context_route"], "native-sol")
        self.assertEqual(self.task["context_parallelism"]["max_workers"], 2)
        self.assert_round_trip()
        relay = json.loads((self.output / "relay.json").read_text(encoding="utf-8"))
        self.assertNotIn("launch_contract", relay)

    def test_fresh_completed_snapshot_runs_only_finalizer_and_reuses_fragments(self) -> None:
        self.assert_round_trip()
        fragments = {
            path: path.read_bytes()
            for path in (self.work / ".game-l10n/context-progress/fragments").rglob("*.json")
        }
        old_task = self.task_path.read_bytes()
        old_task_path = self.task_path
        self.prepare_snapshot("synthetic completed context")
        self.assertEqual(self.task["context_parallelism"]["max_workers"], 0)
        self.assertEqual(self.task["context_parallelism"]["pending_chunks"], 0)
        self.run_command("claim-context-chunk", slot=1, success=False)
        finalized = self.run_command("finalize-context-fragments")
        self.assertEqual((finalized["status"], finalized["success"], finalized["pending"]),
                         ("success", 2, 0))
        self.assertEqual(old_task_path.read_bytes(), old_task)
        for path, original in fragments.items():
            self.assertEqual(path.read_bytes(), original)

    def test_guarded_contract_retains_the_guard_executor_convention(self) -> None:
        bind_omp_isolation(self, self.work)
        configure_native_runtime(self.work, "omp", {}, omp_isolation="guarded")
        self.prepare_snapshot("synthetic guarded context")
        self.assertEqual(self.execution["tool"], "parr0t_exec")
        self.assertEqual(self.execution["tool_device"], "xd://parr0t_exec")
        self.assertEqual(self.execution["argv_convention"], "python_arguments_without_executable")
        for argv in self.execution["command_argv"].values():
            self.assertEqual(argv[:2], ["-m", "game_l10n.cli"])
            self.assertNotIn(sys.executable, argv)
            self.assertNotIn("native-python.py", argv)

    def test_wrong_slot_and_cross_claim_result_cannot_consume_another_slot(self) -> None:
        first_path, first = self.claim(1)
        for invalid_slot in (0, 3):
            self.run_command("claim-context-chunk", slot=invalid_slot, success=False)
        second_path, second = self.claim(2)
        self.run_command(
            "commit-context-fragment", claim_path=second_path,
            result_path=self.result(first), success=False,
        )
        self.assertEqual(self.claim(1), (first_path, first))
        self.assertEqual(self.claim(2), (second_path, second))
        forged = {**first, "slot": 2}
        context_fixture.ContextClaimBindingTests.rewrite_claim(self, first_path, forged)
        self.run_command(
            "commit-context-fragment", claim_path=first_path,
            result_path=self.result(forged), success=False,
        )
        self.assertEqual(self.claim(2), (second_path, second))
        self.assertFalse((self.work / ".game-l10n/context-progress/fragments").exists())

    def test_finalizer_does_not_certify_missing_or_misaligned_fragments(self) -> None:
        first_path, first = self.claim(1)
        _, second = self.claim(2)
        result_path = self.result(first)
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["context_part"]["screens"][0]["evidence_line_ids"] = [
            row["id"] for row in second["rows"]
        ]
        result_path.write_text(json.dumps(result), encoding="utf-8")
        self.run_command(
            "commit-context-fragment", claim_path=first_path,
            result_path=result_path, success=False,
        )
        incomplete = self.run_command("finalize-context-fragments")
        self.assertEqual((incomplete["status"], incomplete["success"], incomplete["pending"]),
                         ("partial", 0, 2))
        self.assertFalse((self.work / self.task["output_artifact"]).exists())
        self.run_command(
            "commit-context-fragment", claim_path=first_path, result_path=self.result(first),
        )
        incomplete = self.run_command("finalize-context-fragments")
        self.assertEqual((incomplete["status"], incomplete["success"], incomplete["pending"]),
                         ("partial", 1, 1))
        envelope = validate_control_envelope(
            self.work, self.work / self.task["output_envelope"],
            expected_role="context_analyzer", expected_stage="context",
        )
        self.assertEqual(envelope["status"], "partial")
        self.assertEqual(envelope["artifact_paths"], [])

    def test_task_copy_and_self_hashed_execution_change_are_rejected(self) -> None:
        copied = self.output / "synthetic-unbound-task.json"
        copied.write_bytes(self.task_path.read_bytes())
        argv = self.argv("claim-context-chunk", slot=1)
        argv[argv.index("--task") + 1] = str(copied)
        self.run_argv(argv, success=False)
        changed = json.loads(self.task_path.read_text(encoding="utf-8"))
        changed["worker_execution"]["workspace_root"] = str(self.empty_path)
        changed["task_id"] = stable_hash({key: value for key, value in changed.items()
                                          if key != "task_id"})
        self.task_path.write_text(json.dumps(changed), encoding="utf-8")
        completed = subprocess.run(
            self.argv("claim-context-chunk", slot=1), cwd=self.empty_path,
            env=self.process_environment, capture_output=True, encoding="utf-8", check=False,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("E_DATA_PLANE_RELAY_INVALID", completed.stderr)
        self.assertFalse((self.work / ".game-l10n/context-progress/claims").exists())

    def test_existing_historical_task_is_not_upgraded_or_rewritten(self) -> None:
        historical = dict(self.task)
        historical.pop("worker_execution")
        historical["task_id"] = stable_hash({key: value for key, value in historical.items()
                                             if key != "task_id"})
        self.task_path.write_text(json.dumps(historical), encoding="utf-8")
        relay_path = self.output / "relay.json"
        relay = json.loads(relay_path.read_text(encoding="utf-8"))
        relay.update(task_id=historical["task_id"],
                     task_sha256=hashlib.sha256(self.task_path.read_bytes()).hexdigest())
        relay_path.write_text(json.dumps(relay), encoding="utf-8")
        lifecycle_path = self.output / "relay-lifecycle.json"
        lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        lifecycle.pop("state_fingerprint")
        lifecycle["task_id"] = historical["task_id"]
        lifecycle["state_fingerprint"] = stable_hash(lifecycle)
        lifecycle_path.write_text(json.dumps(lifecycle), encoding="utf-8")
        self.task = historical
        claimed_path, claimed = self.claim(1)
        self.run_command(
            "commit-context-fragment", claim_path=claimed_path, result_path=self.result(claimed),
        )
        protected = [self.task_path, relay_path, self.output / "relay-lifecycle.json", claimed_path]
        protected.extend((self.work / ".game-l10n/context-progress/fragments").rglob("*.json"))
        snapshots = {path: path.read_bytes() for path in protected}
        prepared = prepare_data_plane_task(
            self.work, self.output, worker_role="context_analyzer",
            config={"game_roots": [str(self.work.parent / "game")],
                    "sdk_root": str(self.work.parent / "sdk"),
                    "files": {"context_pack": "context/context-pack.json"}},
            sdk_activation={"status": "active"},
        )
        self.assertEqual(prepared["task_id"], historical["task_id"])
        for path, original in snapshots.items():
            self.assertEqual(path.read_bytes(), original)
        self.assertNotIn("worker_execution", json.loads(self.task_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
