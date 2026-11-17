from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.cli import build_parser, main
from game_l10n.errors import ConfigurationError
from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.data_plane_task import (
    current_data_plane_operational_failure,
    prepare_data_plane_task,
    record_data_plane_failure,
    seal_data_plane_task,
)
from game_l10n.models import Segment
from game_l10n.native_pass import import_native_wave, native_control_step, prepare_native_tasks, seal_native_wave_relay
from game_l10n.native_runtime import (
    bind_data_plane_launch, configure_native_runtime, native_runtime, record_native_launch,
    native_launch_summary,
)
from game_l10n.native_translation_retry import start_native_translation_retries, advance_native_translation_retries
from game_l10n.store import stable_hash
from tests.omp_isolation_fixture import bind_omp_isolation


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        bind_omp_isolation(self, self.root)
        self.segments = [Segment("a", "SOURCE A"), Segment("b", "SOURCE B")]
        self.source = self.root / "source.jsonl"
        self.write_rows(self.source, [asdict(row) for row in self.segments])
        self.tasks, self.results = self.root / "tasks", self.root / "results"

    @staticmethod
    def write_rows(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def prepare_probe(self) -> tuple[dict, Path, Path]:
        game = self.root / "game"
        game.mkdir()
        (self.root / "game-l10n.json").write_text("{}", encoding="utf-8")
        establish_control_plane_entry(self.root)
        configure_native_runtime(self.root, "omp", {})
        config = {"game_roots": [str(game)], "sdk_root": str(self.root / "sdk"),
                  "sdk_selection": {"inventory_fingerprint": "fixture-inventory"}}
        output = self.root / ".game-l10n/data-plane/source_probe"
        prepared = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        relay = self.root / prepared["relay_path"]
        bind_data_plane_launch(self.root, relay)
        return config, output, relay

    def prepare(self, host: str = "omp") -> tuple[dict, Path, dict]:
        configure_native_runtime(self.root, host, {})
        prepare_native_tasks(
            self.root, self.source, self.segments, self.tasks,
            max_records=1, max_workers=2,
            route_binding={"provider": "native-luna", "attempt": 1},
        )
        step = self.step()
        relay = self.root / step["relay"]["dispatch"]
        return step, relay, json.loads(relay.read_text(encoding="utf-8"))

    def step(self, **kwargs) -> dict:
        return native_control_step(
            self.root, self.tasks / "manifest.json", self.results,
            require_relay_receipt=True, require_runtime=True, **kwargs,
        )

    def row(self, target_id: str, *, provider: str = "native-luna", status: str = "success") -> dict:
        return {"target_id": target_id, "status": status, "text": "TARGET" if status == "success" else "",
                "provider": provider, "issues": []}

    def invoke_argv(self, argv: list[str]) -> dict:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv[2:])
        self.assertEqual(code, 0, stderr.getvalue())
        return json.loads(stdout.getvalue())

    def test_bound_data_plane_argv_records_real_launch_and_seals_missing_output(self) -> None:
        _, output, relay = self.prepare_probe()
        payload = json.loads(relay.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["launch_contract"]["lifecycle_observation"],
            "finite_interruptible_per_call_only_not_a_job_timeout",
        )
        self.assertEqual(
            payload["launch_contract"]["cancellation_priority"],
            "cancel_owned_child_promptly_on_observed_operational_error_before_replacement",
        )
        commands = payload["command_argv"]
        self.invoke_argv(commands["capability-available"])
        start = ["synthetic-worker" if value == "<worker-id>" else value
                 for value in commands["worker-started"]["worker-0"]]
        self.invoke_argv(start)
        summary = native_launch_summary(self.root, relay)
        self.assertEqual(summary["started_workers"], 1)
        self.assertEqual(summary["model"], payload["launch_contract"]["model"])
        sealed = self.invoke_argv(commands["seal"])
        self.assertEqual(sealed["status"], "error")
        self.assertIn("task_incomplete", sealed["issue_codes"])
        self.assertTrue((output / "receipt.json").is_file())


    def test_operational_failure_requires_cancellation_and_preserves_valid_success(self) -> None:
        config, output, relay = self.prepare_probe()
        payload = json.loads(relay.read_text(encoding="utf-8"))
        commands = payload["command_argv"]
        self.invoke_argv(commands["capability-available"])
        with self.assertRaisesRegex(ConfigurationError, "no bound worker start"):
            record_data_plane_failure(
                self.root, relay, failure_code="provider_operational_error"
            )
        start = [
            "synthetic-worker" if value == "<worker-id>" else value
            for value in commands["worker-started"]["worker-0"]
        ]
        self.invoke_argv(start)

        recorded = self.invoke_argv(
            commands["operational-failure"]["provider_operational_error"]
        )
        self.assertEqual(recorded["workers"][0]["worker_id"], "synthetic-worker")
        self.assertEqual(
            recorded["owner"],
            {"host": "omp", "relay_role": "isolated_data_plane_relay"},
        )
        self.assertTrue(recorded["cancellation_required"])
        retry = prepare_data_plane_task(
            self.root,
            output,
            worker_role="source_probe",
            config=config,
        )
        self.assertEqual(retry["task_id"], payload["task_id"])
        self.assertTrue(
            current_data_plane_operational_failure(
                self.root, "source_probe"
            )["cancellation_required"]
        )
        failure_path = relay.with_name(f"operational-failure-{payload['task_id']}.json")
        original_failure = failure_path.read_text(encoding="utf-8")
        changed_failure = json.loads(original_failure)
        changed_failure["workers"][0]["worker_id"] = "unrelated-worker"
        failure_path.write_text(json.dumps(changed_failure), encoding="utf-8")
        self.assertIsNone(current_data_plane_operational_failure(self.root, "source_probe"))
        failure_path.write_text(original_failure, encoding="utf-8")
        with self.assertRaisesRegex(ConfigurationError, "CANCELLATION_REQUIRED"):
            seal_data_plane_task(self.root, relay)

        (self.root / "game-l10n.json").write_text(
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
        envelope = self.root / payload["expected_envelope"]
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
        sealed = seal_data_plane_task(self.root, relay, worker_ended=True)
        self.assertEqual(sealed["status"], "success")
        self.assertTrue((self.root / "game-l10n.json").is_file())
        self.assertIsNone(current_data_plane_operational_failure(self.root, "source_probe"))

    def test_bound_native_argv_imports_only_completed_bound_results(self) -> None:
        establish_control_plane_entry(self.root)
        _, relay, payload = self.prepare()
        commands = payload["command_argv"]
        self.invoke_argv(commands["capability-available"])
        for index, (binding, segment) in enumerate(zip(payload["task_bindings"], self.segments, strict=True)):
            start = [f"synthetic-worker-{index}" if value == "<worker-id>" else value
                     for value in commands["worker-started"][binding["task"]]]
            self.invoke_argv(start)
            self.write_rows(self.root / binding["result"], [self.row(segment.id)])
        args = build_parser().parse_args(commands["seal"][2:])
        seal_native_wave_relay(self.root, self.root / args.dispatch, relay_ended=args.relay_ended)
        completed = self.step()
        self.assertEqual(completed["action"], "aggregate_completed_pass")
        self.assertEqual(completed["counts"]["imported_tasks"], 2)

    def test_tampered_lifecycle_argv_cannot_change_a_bound_relay(self) -> None:
        from game_l10n.data_plane_task import _load_bound_data_plane_relay
        _, _, relay = self.prepare_probe()
        payload = json.loads(relay.read_text(encoding="utf-8"))
        payload["command_argv"]["capability-available"][-1] = "worker-started"
        relay.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ConfigurationError):
            _load_bound_data_plane_relay(self.root, relay)
        self.assertFalse((relay.parent / "receipt.json").exists())

    def test_host_specific_exact_models_and_explicit_override(self) -> None:
        configure_native_runtime(self.root, "codex", {})
        self.assertEqual(native_runtime(self.root)["models"], {
            "native-luna": "gpt-6-luna", "native-sol": "gpt-6.1-sol",
        })
        configure_native_runtime(self.root, "omp", {"native-sol": "fixture/selected-sol"})
        self.assertEqual(native_runtime(self.root)["models"], {
            "native-luna": "openai-codex/gpt-6-luna", "native-sol": "fixture/selected-sol",
        })
        configure_native_runtime(self.root, "omp", {})
        self.assertEqual(native_runtime(self.root)["models"]["native-sol"], "fixture/selected-sol")
        for value in ("default", "inherit", "@task", "fixture/*", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "E_NATIVE_RUNTIME_INVALID"):
                configure_native_runtime(self.root, "omp", {"native-luna": value})

    def test_fresh_omp_sol_wave_binds_exact_provider_path_and_effort(self) -> None:
        configure_native_runtime(self.root, "omp", {})
        self.assertEqual(native_runtime(self.root)["models"], {
            "native-luna": "openai-codex/gpt-6-luna", "native-sol": "opencodex/gpt-6.1-sol:high",
        })
        prepare_native_tasks(
            self.root, self.source, self.segments, self.tasks,
            max_records=1, max_workers=2,
            route_binding={"provider": "native-sol", "attempt": 1},
        )
        step = self.step()
        relay = self.root / step["relay"]["dispatch"]
        contract = json.loads(relay.read_text(encoding="utf-8"))["launch_contract"]
        self.assertEqual(contract["model"], "opencodex/gpt-6.1-sol:high")
        self.assertEqual(contract["spawn_model_parameter"], "tasks[].model")

    def test_missing_runtime_does_not_create_a_controller_checkpoint(self) -> None:
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks)
        with self.assertRaisesRegex(ValueError, "E_NATIVE_RUNTIME_REQUIRED"):
            self.step()
        self.assertFalse((self.root / ".game-l10n/native/control").exists())

    def test_runtime_repair_action_does_not_invalidate_older_manifest(self) -> None:
        from game_l10n.control_plane import review_control_contract
        legacy = review_control_contract()
        legacy["allowed_actions"].remove("repair_native_runtime")
        with patch("game_l10n.native_pass.review_control_contract", return_value=legacy):
            prepare_native_tasks(self.root, self.source, self.segments, self.tasks)
        before = (self.tasks / "manifest.json").read_bytes()
        configure_native_runtime(self.root, "omp", {})
        self.assertEqual(self.step()["action"], "dispatch_isolated_wave_relay")
        self.assertEqual((self.tasks / "manifest.json").read_bytes(), before)

    def test_unavailable_relay_creates_no_fake_results_and_resumes_same_wave(self) -> None:
        _, relay, payload = self.prepare()
        self.assertEqual(payload["launch_contract"]["model"], "openai-codex/gpt-6-luna")
        self.assertEqual(payload["launch_contract"]["spawn_model_parameter"], "tasks[].model")
        record_native_launch(self.root, relay, event="capability-unavailable")
        sealed = seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(sealed["issue_codes"], ["E_NATIVE_DISPATCH_UNAVAILABLE"])
        self.assertEqual(sealed["ended_tasks_recorded"], 0)
        self.assertEqual(list(self.results.glob("*.jsonl")), [])
        dispatch_bytes = relay.read_bytes()
        blocked = self.step()
        self.assertEqual(blocked["action"], "repair_native_runtime")
        self.assertEqual(blocked["relay"]["dispatch"], relay.relative_to(self.root).as_posix())
        self.assertEqual(blocked["relay"]["tasks"], 2)
        self.assertEqual(blocked["runtime_repair"]["scope"], "host")
        self.assertFalse(blocked["runtime_repair"]["isolation_relaxation_allowed"])
        self.assertEqual(relay.read_bytes(), dispatch_bytes)
        record_native_launch(self.root, relay, event="capability-available")
        resumed = self.step(resume_relay=True)
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(resumed["wave"], 1)
        self.assertEqual(resumed["route_binding"]["attempt"], 1)

    def _resume_legacy_unavailable_wave(self, event: str, missing_fields: tuple[str, ...]) -> None:
        from game_l10n.native_runtime import launch_contract

        def legacy_contract(*args, **kwargs):
            contract = launch_contract(*args, **kwargs)
            for field in missing_fields:
                contract.pop(field, None)
            if 'model_authorization' in missing_fields:
                contract['preflight'] = 'inspect_available_dispatch_tool_and_exact_model_before_starting_any_worker'
            return contract

        with patch('game_l10n.native_runtime.launch_contract', side_effect=legacy_contract), \
             patch('game_l10n.native_pass.launch_contract', side_effect=legacy_contract):
            _, relay, payload = self.prepare()
            record_native_launch(self.root, relay, event=event)
            seal_native_wave_relay(self.root, relay, relay_ended=True)
        ledger = relay.with_name(relay.stem + '-launch.json')
        paths = [relay, ledger, self.tasks / 'manifest.json',
                 *(self.root / binding['task'] for binding in payload['task_bindings'])]
        originals = {path: path.read_bytes() for path in paths}
        runtime = native_runtime(self.root)
        self.assertEqual(self.step()['action'], 'repair_native_runtime')
        resumed = self.step(resume_relay=True)
        self.assertEqual(resumed['action'], 'dispatch_isolated_wave_relay')
        self.assertEqual(resumed['wave'], 1)
        self.assertEqual(resumed['route_binding']['attempt'], 1)
        self.assertEqual(native_runtime(self.root), runtime)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        for field in missing_fields:
            self.assertNotIn(field, json.loads(relay.read_text())['launch_contract'])
        record_native_launch(self.root, relay, event='capability-available')
        for binding, target in zip(payload['task_bindings'], ('a', 'b')):
            record_native_launch(self.root, relay, event='worker-started', task=binding['task'],
                                 worker_id='synthetic-legacy-' + target, model=payload['launch_contract']['model'])
            self.write_rows(self.root / binding['result'], [self.row(target)])
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(self.step()['action'], 'aggregate_completed_pass')
        self.assertEqual(relay.read_bytes(), originals[relay])

    def test_old_promptless_capability_unavailable_wave_resumes_same_runtime(self) -> None:
        self._resume_legacy_unavailable_wave('capability-unavailable', ('worker_prompt',))

    def test_old_policyless_model_unavailable_wave_resumes_same_runtime(self) -> None:
        self._resume_legacy_unavailable_wave('model-unavailable', (
            'model_authorization', 'unavailability_evidence', 'worker_prompt',
            'lifecycle_observation', 'cancellation_priority', 'operational_failure_policy',
        ))

    def test_live_relay_wait_has_no_implicit_deadline(self) -> None:
        _, relay, _ = self.prepare()
        for _ in range(5):
            waiting = self.step()
            self.assertEqual(waiting["action"], "wait_for_active_wave")
            self.assertEqual(waiting["issue_codes"], [])
            self.assertEqual(waiting["route_binding"]["attempt"], 1)
            self.assertEqual(waiting["counts"]["imported_tasks"], 0)
        record_native_launch(self.root, relay, event="capability-unavailable")
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(self.step()["action"], "repair_native_runtime")

    def test_ended_legacy_relay_resumes_and_preserves_unverified_peer_result(self) -> None:
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks,
                             max_records=1, max_workers=2,
                             route_binding={"provider": "native-luna", "attempt": 1})
        initial = native_control_step(self.root, self.tasks / "manifest.json", self.results,
                                      require_relay_receipt=True)
        relay = self.root / initial["relay"]["dispatch"]
        payload = json.loads(relay.read_text())
        first, second = payload["task_bindings"]
        self.write_rows(self.root / first["result"], [self.row("a")])
        original = (self.root / first["result"]).read_bytes()
        for _ in range(3):
            native_control_step(self.root, self.tasks / "manifest.json", self.results,
                                require_relay_receipt=True)
        configure_native_runtime(self.root, "codex", {})
        resumed = self.step(resume_relay=True)
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(resumed["route_binding"]["attempt"], 1)
        updated = json.loads(relay.read_text())
        self.assertEqual(updated["launch_contract"]["tasks"], [second["task"]])
        self.assertTrue(updated["relay_contract"]["legacy_results_must_not_be_reexecuted"])
        self.assertEqual(native_launch_summary(self.root, relay)["legacy_unverified_results"], 1)
        record_native_launch(self.root, relay, event="capability-unavailable")
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(self.step()["action"], "repair_native_runtime")
        self.assertEqual(self.step(resume_relay=True)["action"], "dispatch_isolated_wave_relay")
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task=second["task"],
                             worker_id="new-peer", model=updated["launch_contract"]["model"])
        self.write_rows(self.root / second["result"], [self.row("b")])
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        finished = self.step()
        self.assertEqual(finished["action"], "aggregate_completed_pass")
        self.assertEqual(finished["imported_wave"]["success"], 2)
        self.assertEqual((self.root / first["result"]).read_bytes(), original)
        self.assertTrue(list(relay.parent.glob(relay.name + ".legacy-*")))

    def test_preserved_legacy_result_cannot_change_after_resume(self) -> None:
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks,
                             max_records=1, max_workers=2)
        initial = native_control_step(self.root, self.tasks / "manifest.json", self.results,
                                      require_relay_receipt=True)
        relay = self.root / initial["relay"]["dispatch"]
        first = json.loads(relay.read_text())["task_bindings"][0]
        self.write_rows(self.root / first["result"], [self.row("a")])
        configure_native_runtime(self.root, "codex", {})
        self.step(resume_relay=True)
        self.write_rows(self.root / first["result"], [self.row("a", status="refused")])
        with self.assertRaisesRegex(ValueError, "preserved legacy result changed"):
            native_launch_summary(self.root, relay)

    def test_partial_legacy_result_keeps_success_and_queues_only_missing_record(self) -> None:
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks,
                             max_records=2, max_workers=1,
                             route_binding={"provider": "native-luna", "attempt": 1})
        initial = native_control_step(self.root, self.tasks / "manifest.json", self.results,
                                      require_relay_receipt=True)
        relay = self.root / initial["relay"]["dispatch"]
        binding = json.loads(relay.read_text())["task_bindings"][0]
        self.write_rows(self.root / binding["result"], [self.row("a")])
        configure_native_runtime(self.root, "codex", {})
        self.assertEqual(self.step(resume_relay=True)["action"], "dispatch_isolated_wave_relay")
        launch = native_launch_summary(self.root, relay)
        self.assertEqual(launch["expected_workers"], 0)
        self.assertEqual(launch["issue_codes"], [])
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        finished = self.step()
        self.assertEqual(finished["imported_wave"]["success"], 1)
        self.assertEqual(finished["imported_wave"]["failed"], 1)
        rows = [json.loads(line) for line in (self.root / binding["result"]).read_text().splitlines()]
        self.assertEqual(rows[1]["provider"], "native-control")
        self.assertTrue(list(self.results.glob("*.ended-original-*")))

    def test_explicit_ended_launch_recovers_missing_terminal_receipt(self) -> None:
        _, relay, payload = self.prepare()
        record_native_launch(self.root, relay, event="capability-available")
        for binding, target in zip(payload["task_bindings"], ("a", "b")):
            record_native_launch(self.root, relay, event="worker-started", task=binding["task"],
                                 worker_id="worker-" + target, model=payload["launch_contract"]["model"])
            self.write_rows(self.root / binding["result"], [self.row(target)])
        self.assertEqual(self.step()["action"], "wait_for_active_wave")
        self.assertEqual(self.step(resume_relay=True)["action"], "aggregate_completed_pass")

    def test_runtime_refresh_keeps_custom_models_and_active_route_evidence(self) -> None:
        configure_native_runtime(self.root, "omp", {"native-sol": "openai-codex/gpt-6.1-sol"})
        _, relay, payload = self.prepare()
        record_native_launch(self.root, relay, event="capability-available")
        first = payload["task_bindings"][0]
        record_native_launch(self.root, relay, event="worker-started", task=first["task"],
                             worker_id="existing-peer", model=payload["launch_contract"]["model"])
        original = relay.read_bytes()
        configure_native_runtime(self.root, "omp", {"native-qwen": "fixture/approved-qwen"})
        configure_native_runtime(self.root, "omp", {})
        self.assertEqual(native_runtime(self.root)["models"]["native-qwen"], "fixture/approved-qwen")
        self.assertEqual(native_runtime(self.root)["models"]["native-sol"], "openai-codex/gpt-6.1-sol")
        self.assertEqual(native_launch_summary(self.root, relay)["started_workers"], 1)
        self.assertEqual(relay.read_bytes(), original)
        next_generation = native_runtime(self.root)["control_generation"] + 1
        with patch("game_l10n.native_runtime.control_plane_state", return_value={"generation": next_generation}):
            with self.assertRaisesRegex(ValueError, "E_NATIVE_RUNTIME_INVALID"):
                native_runtime(self.root)
            configure_native_runtime(self.root, "omp", {})
            self.assertEqual(native_runtime(self.root)["models"]["native-qwen"], "fixture/approved-qwen")
            self.assertEqual(native_runtime(self.root)["models"]["native-sol"], "openai-codex/gpt-6.1-sol")
            self.assertEqual(native_launch_summary(self.root, relay)["started_workers"], 1)

    def test_model_rebind_of_unstarted_wave_quarantines_unverified_output(self) -> None:
        _, relay, payload = self.prepare()
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        binding = payload["task_bindings"][0]
        self.write_rows(self.root / binding["result"], [self.row("a")])
        configure_native_runtime(self.root, "omp", {"native-luna": "fixture/approved-new-model"})
        step = self.step(resume_relay=True)
        self.assertEqual(step["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(step["route_binding"]["attempt"], 1)
        self.assertEqual(json.loads(relay.read_text())["launch_contract"]["model"], "fixture/approved-new-model")
        self.assertFalse((self.root / binding["result"]).exists())
        self.assertTrue(list(self.results.glob("*.unstarted-original-*")))

    def test_model_rebind_cannot_relabel_an_already_started_worker(self) -> None:
        _, relay, payload = self.prepare()
        record_native_launch(self.root, relay, event="capability-available")
        binding = payload["task_bindings"][0]
        record_native_launch(self.root, relay, event="worker-started", task=binding["task"],
                             worker_id="started-peer", model=payload["launch_contract"]["model"])
        configure_native_runtime(self.root, "omp", {"native-luna": "fixture/approved-new-model"})
        original = relay.read_bytes()
        with self.assertRaisesRegex(ValueError, "E_NATIVE_LAUNCH_INVALID"):
            self.step(resume_relay=True)
        self.assertEqual(relay.read_bytes(), original)

    def test_interrupted_unstarted_rebind_is_resumable_from_archived_evidence(self) -> None:
        _, relay, _ = self.prepare()
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        configure_native_runtime(self.root, "omp", {"native-luna": "fixture/approved-new-model"})
        with patch("game_l10n.native_pass._write_wave_relay_dispatch", side_effect=OSError("fixture interrupted write")):
            with self.assertRaises(OSError):
                self.step(resume_relay=True)
        resumed = self.step(resume_relay=True)
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(json.loads(relay.read_text())["launch_contract"]["model"], "fixture/approved-new-model")

    def test_partial_launch_preserves_success_and_only_starts_remaining_task(self) -> None:
        _, relay, payload = self.prepare()
        model = payload["launch_contract"]["model"]
        first, second = payload["task_bindings"]
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task=first["task"], worker_id="worker-a", model=model)
        self.write_rows(self.root / first["result"], [self.row("a")])
        original = (self.root / first["result"]).read_bytes()
        sealed = seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(sealed["issue_codes"], ["E_NATIVE_WORKER_NOT_STARTED"])
        self.assertFalse((self.root / second["result"]).exists())
        self.assertEqual(self.step()["action"], "repair_native_runtime")
        configure_native_runtime(self.root, "omp", {"native-extra": "fixture/approved-extra"})
        self.step(resume_relay=True)
        pending = record_native_launch(self.root, relay, event="capability-available")
        self.assertEqual(pending["unstarted_tasks"], [second["task"]])
        record_native_launch(self.root, relay, event="worker-started", task=second["task"], worker_id="worker-b", model=model, observed_model=model)
        self.write_rows(self.root / second["result"], [self.row("b")])
        self.assertEqual(seal_native_wave_relay(self.root, relay, relay_ended=True)["issue_codes"], [])
        step = self.step()
        self.assertEqual(step["action"], "aggregate_completed_pass")
        self.assertEqual((self.root / first["result"]).read_bytes(), original)

    def test_provider_label_cannot_hide_host_model_mismatch(self) -> None:
        _, relay, payload = self.prepare()
        record_native_launch(self.root, relay, event="capability-available")
        binding = payload["task_bindings"][0]
        self.write_rows(self.root / binding["result"], [self.row("a")])
        record = record_native_launch(
            self.root, relay, event="worker-started", task=binding["task"], worker_id="wrong-model",
            model=payload["launch_contract"]["model"], observed_model="fixture/sol",
        )
        self.assertEqual(record["issue_codes"], ["E_NATIVE_WORKER_MODEL_MISMATCH"])
        self.assertFalse((self.root / binding["result"]).exists())
        self.assertEqual(len(list(self.results.glob("*.model-rejected-*"))), 1)
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(self.step()["action"], "repair_native_runtime")

    def test_direct_wave_import_cannot_bypass_launch_evidence(self) -> None:
        _, relay, payload = self.prepare()
        for binding, target in zip(payload["task_bindings"], ("a", "b")):
            self.write_rows(self.root / binding["result"], [self.row(target)])
        record_native_launch(self.root, relay, event="capability-unavailable")
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        with self.assertRaisesRegex(ValueError, "E_NATIVE_WORKER_NOT_STARTED"):
            import_native_wave(self.root, self.tasks / "manifest.json", self.results, 1)

    def test_legacy_transport_errors_cannot_consume_another_provider_attempt(self) -> None:
        first = self.root / "first.jsonl"
        self.write_rows(first, [self.row("a"), self.row("b", status="refused")])
        state_dir = self.root / "retry"
        start_native_translation_retries(self.root, self.source, self.segments, first, state_dir)
        failed = self.root / "failed.jsonl"
        self.write_rows(failed, [self.row("b", status="error", provider="native-control")])
        before = json.loads((state_dir / "retry-state.json").read_text())
        repaired = advance_native_translation_retries(self.root, state_dir, failed)
        self.assertEqual(repaired["next_attempt"], before["next_attempt"])
        self.assertEqual(repaired["next_provider"], before["next_provider"])
        self.assertEqual(repaired["candidate_count"], 1)
        self.assertFalse(repaired["provider_attempt_consumed"])
        self.assertNotEqual(repaired["native_pass_output"], "retry/native-passes/run-0001")
        self.write_rows(failed, [self.row("b")])
        self.assertEqual(advance_native_translation_retries(self.root, state_dir, failed)["next_action"], "complete")

    def test_new_execution_cannot_import_another_runs_errors(self) -> None:
        options = {"max_records": 1, "max_workers": 2,
                   "route_binding": {"provider": "native-luna", "attempt": 1}}
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks, **options)
        first = native_control_step(self.root, self.tasks / "manifest.json", self.results, require_relay_receipt=True)
        seal_native_wave_relay(self.root, self.root / first["relay"]["dispatch"], relay_ended=True)
        ended = native_control_step(self.root, self.tasks / "manifest.json", self.results, require_relay_receipt=True)
        self.assertEqual(ended["imported_wave"]["failed"], 2)
        configure_native_runtime(self.root, "codex", {})
        for index, route in enumerate((options["route_binding"],
                                       {"provider": "native-luna", "attempt": 2},
                                       {"provider": "native-sol", "attempt": 1})):
            with self.subTest(route=route):
                tasks, results = self.root / f"retry-{index}", self.root / f"retry-results-{index}"
                prepare_native_tasks(self.root, self.source, self.segments, tasks,
                                     **{**options, "route_binding": route})
                step = native_control_step(self.root, tasks / "manifest.json", results,
                                           require_relay_receipt=True, require_runtime=True)
                self.assertEqual(step["action"], "dispatch_isolated_wave_relay")
                self.assertEqual(step["counts"]["imported_tasks"], 0)
                self.assertNotEqual(step["state"], ended["state"])
                self.assertNotEqual(step["relay"]["dispatch"], first["relay"]["dispatch"])
                self.assertEqual(list(results.glob("*.jsonl")), [])
                self.assertTrue(prepare_native_tasks(self.root, self.source, self.segments, tasks,
                                                     **{**options, "route_binding": route})["cached"])

    def test_rebinding_same_directory_does_not_reuse_prior_route_receipts(self) -> None:
        options = {"max_records": 1, "max_workers": 2,
                   "route_binding": {"provider": "native-luna", "attempt": 1}}
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks, **options)
        first = native_control_step(self.root, self.tasks / "manifest.json", self.results, require_relay_receipt=True)
        seal_native_wave_relay(self.root, self.root / first["relay"]["dispatch"], relay_ended=True)
        native_control_step(self.root, self.tasks / "manifest.json", self.results, require_relay_receipt=True)
        original = {p.name: p.read_bytes() for p in self.results.glob("*.jsonl")}
        options["route_binding"] = {"provider": "native-sol", "attempt": 1}
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks, **options)
        step = native_control_step(self.root, self.tasks / "manifest.json", self.root / "sol-results", require_relay_receipt=True)
        self.assertEqual(step["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(step["counts"]["imported_tasks"], 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.results.glob("*.jsonl")}, original)

    def test_execution_namespace_is_immutable(self) -> None:
        self.prepare()
        path = self.tasks / "manifest.json"
        original = path.read_text()
        for namespace in ("another-run", None):
            with self.subTest(namespace=namespace):
                manifest = json.loads(original)
                if namespace is None:
                    manifest.pop("execution_namespace")
                else:
                    manifest["execution_namespace"] = namespace
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "E_DETAIL_CONTRACT_CONFLICT"):
                    self.step()

    def test_legacy_completed_checkpoint_remains_unchanged_and_resumable(self) -> None:
        options = {"max_records": 1, "max_workers": 2,
                   "route_binding": {"provider": "native-luna", "attempt": 1}}
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks, **options)
        path = self.tasks / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest.pop("execution_namespace")
        manifest["contract_fingerprint"] = stable_hash({key: manifest[key] for key in (
            "worker_contract", "detail_contract", "execution_contract", "result_contract",
            "route_binding", "language_binding",
        )})
        path.write_text(json.dumps(manifest), encoding="utf-8")
        first = native_control_step(self.root, path, self.results, require_relay_receipt=True)
        for task, target in zip(manifest["tasks"], ("a", "b")):
            self.write_rows(self.results / task["file"], [self.row(target)])
        seal_native_wave_relay(self.root, self.root / first["relay"]["dispatch"], relay_ended=True)
        ended = native_control_step(self.root, path, self.results, require_relay_receipt=True)
        self.assertEqual(ended["imported_wave"]["success"], 2)
        original = path.read_bytes()
        configure_native_runtime(self.root, "codex", {})
        self.assertTrue(prepare_native_tasks(self.root, self.source, self.segments, self.tasks, **options)["cached"])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.step()["action"], "aggregate_completed_pass")
        self.assertEqual(self.step()["counts"]["imported_tasks"], 2)

    def test_mixed_transport_failure_does_not_replay_already_attempted_failures(self) -> None:
        segments = [*self.segments, Segment("c", "SOURCE C")]
        self.write_rows(self.source, [asdict(row) for row in segments])
        first = self.root / "first.jsonl"
        self.write_rows(first, [self.row("a"), self.row("b", status="refused"), self.row("c", status="error", provider="native-control")])
        state_dir = self.root / "retry"
        state = start_native_translation_retries(self.root, self.source, segments, first, state_dir)
        self.assertEqual(state["next_attempt"], 1)
        self.assertEqual(state["pending_count"], 1)
        self.assertEqual(state["deferred_failure_count"], 1)
        self.assertEqual(state["unresolved_count"], 2)
        pending = [json.loads(line)["id"] for line in (self.root / state["pending_input"]).read_text().splitlines()]
        self.assertEqual(pending, ["c"])
        retry = self.root / "recovered.jsonl"
        self.write_rows(retry, [self.row("c")])
        resumed = advance_native_translation_retries(self.root, state_dir, retry)
        self.assertEqual(resumed["next_attempt"], 2)
        self.assertEqual(resumed["candidate_count"], 2)
        self.assertEqual(resumed["deferred_failure_count"], 0)
        pending = [json.loads(line)["id"] for line in (self.root / resumed["pending_input"]).read_text().splitlines()]
        self.assertEqual(pending, ["b"])

    def test_discovery_transport_failure_reuses_same_task_and_attempt(self) -> None:
        game = self.root / "game"
        game.mkdir()
        (self.root / "game-l10n.json").write_text("{}", encoding="utf-8")
        establish_control_plane_entry(self.root)
        configure_native_runtime(self.root, "omp", {})
        config = {"game_roots": [str(game)], "sdk_root": str(self.root / "sdk"),
                  "sdk_selection": {"inventory_fingerprint": "fixture-inventory"}}
        output = self.root / ".game-l10n/data-plane/source_probe"
        first = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        relay = self.root / first["relay_path"]
        bind_data_plane_launch(self.root, relay)
        original = (output / "task.json").read_bytes()
        record_native_launch(self.root, relay, event="capability-unavailable")
        result = seal_data_plane_task(self.root, relay, worker_ended=True)
        self.assertEqual(result["issue_codes"], ["E_NATIVE_DISPATCH_UNAVAILABLE"])
        second = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        self.assertEqual(second["task_id"], first["task_id"])
        self.assertEqual((output / "task.json").read_bytes(), original)

    def test_completed_data_plane_attempt_keeps_distinct_launch_evidence(self) -> None:
        for legacy_layout in (False, True):
            with self.subTest(legacy_layout=legacy_layout), tempfile.TemporaryDirectory() as temporary:
                previous_root, self.root = self.root, Path(temporary)
                try:
                    bind_omp_isolation(self, self.root)
                    config, output, relay = self.prepare_probe()
                    first_id = json.loads(relay.read_text())["task_id"]
                    model = json.loads(relay.read_text())["launch_contract"]["model"]
                    record_native_launch(self.root, relay, event="capability-available")
                    record_native_launch(self.root, relay, event="worker-started", task="worker-0",
                                         worker_id="fixture-first", model=model)
                    first_ledger = next(output.glob("*-launch.json"))
                    if legacy_layout:
                        first_ledger = first_ledger.rename(output / "relay-launch.json")
                    original_ledger = first_ledger.read_bytes()
                    closed = seal_data_plane_task(self.root, relay, worker_ended=True)
                    self.assertNotIn("E_NATIVE_WORKER_NOT_STARTED", closed["issue_codes"])
                    prepared = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
                    self.assertNotEqual(prepared["task_id"], first_id)
                    bind_data_plane_launch(self.root, relay)
                    fresh = native_launch_summary(self.root, relay)
                    self.assertEqual(fresh["started_workers"], 0)
                    self.assertEqual(fresh["unstarted_tasks"], ["worker-0"])
                    record_native_launch(self.root, relay, event="capability-available")
                    record_native_launch(self.root, relay, event="worker-started", task="worker-0",
                                         worker_id="fixture-second", model=model)
                    self.assertEqual(native_launch_summary(self.root, relay)["started_workers"], 1)
                    self.assertEqual(first_ledger.read_bytes(), original_ledger)
                finally:
                    self.root = previous_root

    def test_discovery_accepts_equivalent_noncanonical_workspace_path(self) -> None:
        game = self.root / "game"
        game.mkdir()
        alias = self.root / "alias"
        alias.mkdir()
        (self.root / "game-l10n.json").write_text("{}", encoding="utf-8")
        establish_control_plane_entry(self.root)
        config = {"game_roots": [str(game)], "sdk_root": str(self.root / "sdk"),
                  "sdk_selection": {"inventory_fingerprint": "fixture-inventory"}}
        output = self.root / ".game-l10n/data-plane/source_probe"
        first = prepare_data_plane_task(alias / "..", output, worker_role="source_probe", config=config)
        second = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        self.assertEqual(first["task_id"], second["task_id"])

    def test_ended_unstarted_probe_rebind_keeps_task_and_invalidates_stale_receipt(self) -> None:
        config, output, relay = self.prepare_probe()
        original = (output / "task.json").read_bytes()
        payload = json.loads(relay.read_text())
        envelope = self.root / payload["expected_envelope"]
        envelope.write_text('{"stale":true}', encoding="utf-8")
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        seal_data_plane_task(self.root, relay, worker_ended=True)
        configure_native_runtime(self.root, "omp", {"native-sol": "fixture/approved-new-model"})
        prepared = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        self.assertEqual(prepared["task_id"], payload["task_id"])
        bind_data_plane_launch(self.root, relay)
        self.assertEqual((output / "task.json").read_bytes(), original)
        self.assertFalse((output / "receipt.json").exists())
        self.assertFalse(envelope.exists())
        self.assertTrue(list(output.glob("receipt.json.legacy-*")))
        self.assertTrue(list(output.glob(envelope.name + ".legacy-*")))
        launch = native_launch_summary(self.root, relay)
        self.assertEqual(launch["model"], "fixture/approved-new-model")
        self.assertEqual(launch["started_workers"], 0)
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task="worker-0",
                             worker_id="new-probe", model="fixture/approved-new-model")
        self.assertEqual(native_launch_summary(self.root, relay)["started_workers"], 1)

    def test_data_plane_rebind_requires_terminal_evidence(self) -> None:
        _, _, relay = self.prepare_probe()
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        configure_native_runtime(self.root, "omp", {"native-sol": "fixture/approved-new-model"})
        original = relay.read_bytes()
        with self.assertRaisesRegex(ValueError, "ended data-plane receipt required"):
            bind_data_plane_launch(self.root, relay)
        self.assertEqual(relay.read_bytes(), original)

    def test_data_plane_rebind_cannot_relabel_a_started_worker(self) -> None:
        _, _, relay = self.prepare_probe()
        model = json.loads(relay.read_text())["launch_contract"]["model"]
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task="worker-0",
                             worker_id="existing-probe", model=model)
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        seal_data_plane_task(self.root, relay, worker_ended=True)
        configure_native_runtime(self.root, "omp", {"native-sol": "fixture/approved-new-model"})
        original = relay.read_bytes()
        with self.assertRaisesRegex(ValueError, "E_NATIVE_LAUNCH_INVALID"):
            bind_data_plane_launch(self.root, relay)
        self.assertEqual(relay.read_bytes(), original)

    def test_interrupted_data_plane_rebind_recovers_archived_evidence(self) -> None:
        _, output, relay = self.prepare_probe()
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        seal_data_plane_task(self.root, relay, worker_ended=True)
        configure_native_runtime(self.root, "omp", {"native-sol": "fixture/approved-new-model"})
        original = (output / "task.json").read_bytes()
        with patch("game_l10n.native_runtime._write", side_effect=OSError("fixture interrupted binding")):
            with self.assertRaises(OSError):
                bind_data_plane_launch(self.root, relay)
        bind_data_plane_launch(self.root, relay)
        self.assertEqual(native_launch_summary(self.root, relay)["model"], "fixture/approved-new-model")
        self.assertEqual((output / "task.json").read_bytes(), original)

    def test_prompt_selected_model_can_launch_without_a_secondary_approval_list(self) -> None:
        selected = "fixture-provider/unlisted-review-model@fixture-route:high"
        (self.root / "game-l10n.json").write_text(json.dumps({
            "model_policy": {"source": "user_prompt", "native_roles": {"translation": selected}},
        }), encoding="utf-8")
        configure_native_runtime(self.root, "omp", {})
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks,
                             max_records=1, max_workers=2,
                             route_binding={"provider": "native-translation", "attempt": 1})
        relay = self.root / self.step()["relay"]["dispatch"]
        payload = json.loads(relay.read_text())
        with self.assertRaisesRegex(ValueError, "E_NATIVE_MODEL_FAILURE_EVIDENCE_REQUIRED"):
            record_native_launch(self.root, relay, event="model-unavailable")
        self.assertEqual(native_launch_summary(self.root, relay)["issue_codes"], ["E_NATIVE_RELAY_PREFLIGHT_REQUIRED"])
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started",
                             task=payload["task_bindings"][0]["task"], worker_id="fixture-exact-worker", model=selected)
        summary = native_launch_summary(self.root, relay)
        self.assertEqual(summary["started_workers"], 1)
        self.assertEqual(summary["model"], selected)
        self.assertEqual(summary["model_authorization"]["source"], "user_prompt")
        self.assertEqual(summary["model_authorization"]["status"], "approved")

    def test_observed_dispatch_denial_is_operational_not_revoked_model_approval(self) -> None:
        _, _, relay = self.prepare_probe()
        before = native_runtime(self.root)
        payload = json.loads(relay.read_text())
        summary = self.invoke_argv(payload["command_argv"]["model-unavailable:host_dispatch_denied"])
        self.assertEqual(summary["model_authorization"]["status"], "approved")
        self.assertEqual(summary["model_failure_code"], "host_dispatch_denied")
        self.assertEqual(summary["started_workers"], 0)
        self.assertEqual(native_runtime(self.root), before)
        sealed = self.invoke_argv(payload["command_argv"]["seal"])
        self.assertEqual(sealed["status"], "error")
        self.assertEqual(sealed["issue_codes"], ["E_NATIVE_WORKER_MODEL_UNAVAILABLE"])

    def test_existing_started_contract_keeps_original_authorization_boundary(self) -> None:
        from game_l10n.native_runtime import launch_contract

        def legacy_contract(*args, **kwargs):
            contract = launch_contract(*args, **kwargs)
            contract.pop("model_authorization", None)
            contract.pop("unavailability_evidence", None)
            contract.pop("worker_prompt", None)
            contract["preflight"] = "inspect_available_dispatch_tool_and_exact_model_before_starting_any_worker"
            return contract

        with patch("game_l10n.native_runtime.launch_contract", side_effect=legacy_contract), \
             patch("game_l10n.native_pass.launch_contract", side_effect=legacy_contract):
            _, relay, payload = self.prepare()
            record_native_launch(self.root, relay, event="capability-available")
            record_native_launch(self.root, relay, event="worker-started",
                                 task=payload["task_bindings"][0]["task"], worker_id="fixture-existing-worker",
                                 model=payload["launch_contract"]["model"])
        original = relay.read_bytes()
        self.assertEqual(native_launch_summary(self.root, relay)["started_workers"], 1)
        self.assertEqual(relay.read_bytes(), original)
