from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n import data_plane_task
from game_l10n.cli import build_parser, main
from game_l10n.errors import ConfigurationError
from game_l10n.control_plane import (
    CONTROL_HANDOFF_PATH, acknowledge_control_plane_rotation, establish_control_plane_entry, record_control_plane_leak,
)
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
from tests import write_semantic_result_rows


APPROVED_EXECUTION_POLICY = {
    "schema": "parr0t.execution-environment-policy.v1", "host": "omp",
    "authorization": "user_requested",
    "fallback_on": ["provider_operational_error", "execution_guard_denied"],
    "fallback_strategy": "next_configured_native_route",
    "preserve_successes": True, "preserve_isolation": True,
    "silent_model_switch_allowed": False,
}


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        bind_omp_isolation(self, self.root)
        establish_control_plane_entry(self.root)
        self.segments = [Segment("a", "SOURCE A"), Segment("b", "SOURCE B")]
        self.source = self.root / "source.jsonl"
        self.write_rows(self.source, [asdict(row) for row in self.segments])
        self.tasks, self.results = self.root / "tasks", self.root / "results"

    @staticmethod
    def write_rows(path: Path, rows: list[dict], *, model: str | None = None) -> None:
        write_semantic_result_rows(path, rows, model=model)

    def prepare_probe(self, *, approved_recovery: bool = False) -> tuple[dict, Path, Path]:
        game = self.root / "game"
        game.mkdir()
        (self.root / "game-l10n.json").write_text("{}", encoding="utf-8")
        establish_control_plane_entry(self.root)
        configure_native_runtime(self.root, "omp", {})
        config = {"game_roots": [str(game)], "sdk_root": str(self.root / "sdk"),
                  "sdk_selection": {"inventory_fingerprint": "fixture-inventory"}}
        output = self.root / ".game-l10n/data-plane/source_probe"
        with patch("game_l10n.native_runtime.active_execution_environment_policy",
                   return_value=APPROVED_EXECUTION_POLICY if approved_recovery else None):
            prepared = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        relay = self.root / prepared["relay_path"]
        bind_data_plane_launch(self.root, relay)
        return config, output, relay

    def prepare(self, host: str = "omp") -> tuple[dict, Path, dict]:
        previous = native_runtime(self.root)
        model = ((previous or {}).get("models", {}).get("native-luna")
                 if previous and previous["host"] == host else None)
        configure_native_runtime(self.root, host, {
            "native-luna": model or ("openai-codex/gpt-6-luna:high" if host == "omp" else "gpt-6-luna"),
        })
        prepare_native_tasks(
            self.root, self.source, self.segments, self.tasks,
            max_records=1, max_workers=2,
            route_binding={"provider": "native-luna", "attempt": 1},
        )
        step = self.step()
        relay = self.root / step["relay"]["dispatch"]
        return step, relay, json.loads(relay.read_text(encoding="utf-8"))

    def prepare_replacement(self, *, route: str = "native-recovery",
                            model: str = "fixture/approved-successor") -> None:
        configure_native_runtime(self.root, "omp", {route: model})
        self.tasks, self.results = self.root / "replacement-tasks", self.root / "replacement-results"
        prepare_native_tasks(
            self.root, self.source, self.segments, self.tasks,
            max_records=1, max_workers=2,
            route_binding={"provider": route, "model": model, "attempt": 1},
        )

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

    def test_compiled_worker_binding_and_pending_preflight_preserve_missing_start_error(self) -> None:
        _, _, relay = self.prepare_probe()
        payload = json.loads(relay.read_text(encoding="utf-8"))
        dispatch = payload["launch_contract"]["worker_dispatches"]["worker-0"]
        task = (self.root / payload["task_path"]).resolve()
        self.assertEqual(Path(dispatch["task_path"]), task)
        self.assertEqual(dispatch["task_sha256"], hashlib.sha256(task.read_bytes()).hexdigest())
        self.assertEqual(dispatch["slot"], 0)
        for binding in (str(self.root.resolve()), str(task), dispatch["task_sha256"], "PARR0T_OMP_TASK=worker-0"):
            self.assertIn(binding, dispatch["worker_prompt"])
        self.assertIs(native_launch_summary(self.root, relay)["launch_observed"], False)
        preflight = self.invoke_argv(payload["command_argv"]["capability-available"])
        self.assertEqual(preflight["issue_codes"], [])
        self.assertEqual(preflight["launch_state"], "awaiting_worker_start")
        self.assertIs(preflight["launch_observed"], True)
        self.assertEqual(native_launch_summary(self.root, relay)["issue_codes"], ["E_NATIVE_WORKER_NOT_STARTED"])
        sealed = self.invoke_argv(payload["command_argv"]["seal"])
        self.assertIn("E_NATIVE_WORKER_NOT_STARTED", sealed["issue_codes"])

    def test_admitted_worker_cannot_be_sealed_until_its_real_terminal_event(self) -> None:
        _, output, relay = self.prepare_probe()
        payload = json.loads(relay.read_text(encoding="utf-8"))
        fingerprint = hashlib.sha256(relay.read_bytes()).hexdigest()
        key = hashlib.sha256(f"{fingerprint}\0worker-0".encode()).hexdigest()
        admission = self.root / f".game-l10n/native/omp-launch-admissions/{key}.json"
        admission.parent.mkdir(parents=True)
        record = {"schema": "parr0t.omp-native-admission.v1", "relay_sha256": fingerprint,
                  "task": "worker-0", "parent_id": "fixture-relay", "worker_id": "fixture-worker"}
        for state in ("reserved", "started", "ending"):
            with self.subTest(state=state):
                admission.write_text(json.dumps({**record, "state": state}), encoding="utf-8")
                self.assertIs(native_launch_summary(self.root, relay)["launch_observed"], True)
                with self.assertRaisesRegex(ValueError, "E_NATIVE_WORKER_ACTIVE"):
                    seal_data_plane_task(self.root, relay, worker_ended=True)
                self.assertFalse((output / "receipt.json").exists())
        self.invoke_argv(payload["command_argv"]["capability-available"])
        self.invoke_argv(["fixture-worker" if arg == "<worker-id>" else arg
                         for arg in payload["command_argv"]["worker-started"]["worker-0"]])
        admission.write_text(json.dumps({**record, "state": "ended"}), encoding="utf-8")
        sealed = self.invoke_argv(payload["command_argv"]["seal"])
        self.assertIn("task_incomplete", sealed["issue_codes"])
        self.assertNotIn("E_NATIVE_WORKER_NOT_STARTED", sealed["issue_codes"])

    def test_confirmed_interrupted_rewrite_keeps_started_output_and_rejects_unstarted_output(self) -> None:
        from game_l10n.formats import read_segments
        from game_l10n.models import AttemptRecord
        from game_l10n.native_pass import merge_native_pass
        from game_l10n.native_review import (
            _bound_result, _pending_segment, collect_native_rewrite, partition_native_review_routes,
        )
        from tests.test_native_pass import review_segment

        model = "fixture/rewrite"
        configure_native_runtime(self.root, "omp", {"native-rewrite": model})
        manifest = self.tasks / "manifest.json"
        segments = []
        for target in ("a", "b", "c"):
            segment = review_segment(target, source="Fixture source")
            candidate = _bound_result(segment)
            candidate.attempts = [AttemptRecord("review", "native-sol", "rewrite", candidate_generation=1)]
            candidate.metadata["rewrite_issues"] = ["voice"]
            segments.append(_pending_segment(segment, candidate))
        self.write_rows(self.source, [asdict(segment) for segment in segments])
        segments = read_segments(self.source)
        prepare_native_tasks(
            self.root, self.source, segments, self.tasks, pass_kind="rewrite",
            max_records=1, max_workers=3,
            route_binding={"provider": "native-rewrite", "model": model, "attempt": 1},
        )
        relay = self.root / self.step()["relay"]["dispatch"]
        relay_bytes = relay.read_bytes()
        started, unstarted, interrupted = json.loads(relay_bytes)["task_bindings"]
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task=started["task"],
                             worker_id="fixture-worker", model=model)
        record_native_launch(self.root, relay, event="worker-started", task=interrupted["task"],
                             worker_id="fixture-interrupted-worker", model=model)
        digest = hashlib.sha256(relay_bytes).hexdigest()
        key = hashlib.sha256(f"{digest}\0{started['task']}".encode()).hexdigest()
        admission = self.root / f".game-l10n/native/omp-launch-admissions/{key}.json"
        admission.parent.mkdir(parents=True)
        admission.write_text(json.dumps({
            "schema": "parr0t.omp-native-admission.v1", "relay_sha256": digest,
            "task": started["task"], "parent_id": "fixture-relay",
            "worker_id": "fixture-worker", "state": "started",
        }), encoding="utf-8")
        original_admission = admission.read_bytes()
        started_result, unstarted_result = self.root / started["result"], self.root / unstarted["result"]
        self.write_rows(started_result, [self.row("a", provider="native-rewrite")], model=model)
        self.write_rows(unstarted_result, [self.row("b", provider="native-rewrite")], model=model)
        original_unstarted_result = unstarted_result.read_bytes()

        with self.assertRaisesRegex(ValueError, "E_NATIVE_WORKER_ACTIVE"):
            self.step(resume_relay=True)
        recovered = self.step(resume_relay=True, confirm_rewrite_workers_stopped=True)
        self.assertEqual(recovered["action"], "aggregate_completed_pass")
        merged = self.root / "merged-rewrite.jsonl"
        merge_native_pass(self.root, segments, merged, manifest_path=manifest)
        first, second, third = map(json.loads, merged.read_text(encoding="utf-8").splitlines())
        self.assertEqual((first["target_id"], first["status"], first["text"]), ("a", "success", "TARGET"))
        self.assertEqual((second["target_id"], second["status"], second["issues"]),
                         ("b", "error", ["worker_not_started"]))
        self.assertEqual((third["target_id"], third["status"], third["issues"]),
                         ("c", "error", ["worker_interrupted"]))
        archives = list(unstarted_result.parent.glob(unstarted_result.name + ".ended-original-*"))
        self.assertEqual([item.read_bytes() for item in archives], [original_unstarted_result])
        self.assertEqual(admission.read_bytes(), original_admission)
        self.assertEqual(relay.read_bytes(), relay_bytes)
        self.assertEqual(self.step()["action"], "aggregate_completed_pass")
        collected = (self.root / "collected").resolve()
        summary = collect_native_rewrite(self.root, self.source, manifest, collected)
        self.assertEqual((summary["confirmation_pending"], summary["unresolved"]), (1, 2))
        pending = read_segments(collected / "unresolved-input.jsonl")
        groups = partition_native_review_routes(pending, ["native-rewrite"], stage="review-rewrite")
        self.assertEqual([segment.id for segment in groups["native-rewrite"]], ["b", "c"])


    def test_operational_failure_requires_cancellation_and_preserves_valid_success(self) -> None:
        config, output, relay = self.prepare_probe()
        payload = json.loads(relay.read_text(encoding="utf-8"))
        commands = payload["command_argv"]
        self.invoke_argv(commands["capability-available"])
        with self.assertRaisesRegex(ConfigurationError, "E_DATA_PLANE_FAILURE_INVALID"):
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

    def test_metadata_configuration_before_entry_does_not_authorize_execution(self) -> None:
        from game_l10n.native_host import configure_native_host
        from game_l10n.native_runtime import launch_contract
        unentered = self.root / "unentered"
        with patch.dict("os.environ", {"PARR0T_OMP_GUARD_SESSION": ""}):
            configure_native_runtime(unentered, "omp", {"native-custom": "fixture/exact"},
                                     omp_isolation="prompt")
            configure_native_host(unentered, "fixture-host", "http://127.0.0.1:12345")
            self.assertEqual(native_runtime(unentered)["models"], {"native-custom": "fixture/exact"})
            contract = launch_contract(unentered, "native-custom", ["fixture-task"])
            self.assertEqual(contract["model"], "fixture/exact")
            with self.assertRaisesRegex(ConfigurationError, "^E_CONTROL_PLANE_ENTRY_REQUIRED"):
                record_native_launch(unentered, unentered / ".game-l10n/relay.json", event="capability-available")

    def test_a_single_custom_route_does_not_require_unused_legacy_slots(self) -> None:
        configure_native_runtime(self.root, "omp", {"native-rewrite": "\uacf5\uae09\uc790/\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804@\uacbd\ub85c:high"})
        self.assertEqual(native_runtime(self.root)["models"], {
            "native-rewrite": "\uacf5\uae09\uc790/\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804@\uacbd\ub85c:high",
        })
        configure_native_runtime(self.root, "omp", {})
        self.assertEqual(set(native_runtime(self.root)["models"]), {"native-rewrite"})

    def test_host_metadata_resolves_one_exact_id_without_inventing_authorization(self) -> None:
        from game_l10n.native_host import MODEL_SNAPSHOT_PATH, MODEL_SNAPSHOT_SCHEMA, host_model_snapshot
        path = self.root / MODEL_SNAPSHOT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {"schema": MODEL_SNAPSHOT_SCHEMA, "host": "omp", "session_id": "fixture-session",
                    "process_id": 123, "models": [
                        {"provider": "\uc2e4\uc81c\uacf5\uae09\uc790", "id": "\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804:high",
                         "selector": "\uc2e4\uc81c\uacf5\uae09\uc790/\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804:high"},
                    ]}
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        configure_native_runtime(self.root, "omp", {"native-custom": "\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804:high"})
        self.assertEqual(native_runtime(self.root)["models"]["native-custom"], "\uc2e4\uc81c\uacf5\uae09\uc790/\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804:high")
        self.assertNotIn("authorization", host_model_snapshot(self.root))
        snapshot["models"].append({"provider": "\uc2e4\uc81c\uacf5\uae09\uc790", "id": "opaque-base",
                                   "selector": "\uc2e4\uc81c\uacf5\uae09\uc790/opaque-base"})
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        configure_native_runtime(self.root, "omp", {"native-qualified": "opaque-base@explicit-route:high"})
        self.assertEqual(native_runtime(self.root)["models"]["native-qualified"],
                         "\uc2e4\uc81c\uacf5\uae09\uc790/opaque-base@explicit-route:high")
        snapshot["models"].extend([
            {"provider": "exact-provider", "id": "model:variant",
             "selector": "exact-provider/model:variant"},
            {"provider": "wrong-provider", "id": "model",
             "selector": "wrong-provider/model"},
        ])
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        configure_native_runtime(self.root, "omp", {"native-routed": "model:variant@upstream:high"})
        self.assertEqual(native_runtime(self.root)["models"]["native-routed"],
                         "exact-provider/model:variant@upstream:high")
        snapshot["models"].append({"provider": "\ub2e4\ub978\uacf5\uae09\uc790", "id": "\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804:high",
                                   "selector": "\ub2e4\ub978\uacf5\uae09\uc790/\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804:high"})
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        before = (self.root / ".game-l10n/native/runtime.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "E_NATIVE_MODEL_AMBIGUOUS"):
            configure_native_runtime(self.root, "omp", {"native-custom": "\ubaa8\ub378(\ud310)+\uc0c8\ubc84\uc804:high"})
        self.assertEqual((self.root / ".game-l10n/native/runtime.json").read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "E_NATIVE_MODEL_METADATA_REQUIRED"):
            configure_native_runtime(self.root, "omp", {"native-custom": "\ubaa8\ub378(\ud310)"})
        configure_native_runtime(self.root, "omp", {"native-custom": "\uba85\uc2dc\uacf5\uae09\uc790/\ubaa8\ub378@\uacbd\ub85c:high"})
        self.assertEqual(native_runtime(self.root)["models"]["native-custom"], "\uba85\uc2dc\uacf5\uae09\uc790/\ubaa8\ub378@\uacbd\ub85c:high")

    def test_nested_opaque_catalog_id_resolves_before_qualified_fallback(self) -> None:
        from game_l10n.native_host import MODEL_SNAPSHOT_PATH, MODEL_SNAPSHOT_SCHEMA, resolve_native_model
        path = self.root / MODEL_SNAPSHOT_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "schema": MODEL_SNAPSHOT_SCHEMA, "host": "omp",
            "session_id": "fixture-session", "process_id": 123,
            "models": [
                {"provider": "fixture", "id": "org/model", "selector": "fixture/org/model"},
                {"provider": "org", "id": "unrelated", "selector": "org/unrelated"},
            ],
        }
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        for requested, expected in (
            ("org/model", "fixture/org/model"),
            ("org/model:high", "fixture/org/model:high"),
            ("org/model@upstream:high", "fixture/org/model@upstream:high"),
            ("fixture/org/model@upstream:high", "fixture/org/model@upstream:high"),
            ("explicit/org/model@upstream:high", "explicit/org/model@upstream:high"),
            ("org/model-extra@upstream:high", "org/model-extra@upstream:high"),
        ):
            with self.subTest(requested=requested):
                self.assertEqual(resolve_native_model(self.root, "omp", requested), expected)
        configure_native_runtime(self.root, "omp", {"native-nested": "org/model@upstream:high"})
        self.assertEqual(native_runtime(self.root)["models"]["native-nested"], "fixture/org/model@upstream:high")
        original = (self.root / ".game-l10n/native/runtime.json").read_bytes()
        snapshot["models"].append({"provider": "other", "id": "org/model", "selector": "other/org/model"})
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "E_NATIVE_MODEL_AMBIGUOUS"):
            configure_native_runtime(self.root, "omp", {"native-nested": "org/model@upstream:high"})
        self.assertEqual((self.root / ".game-l10n/native/runtime.json").read_bytes(), original)
        self.assertEqual(resolve_native_model(self.root, "omp", "fixture/org/model@upstream:high"),
                         "fixture/org/model@upstream:high")

    def test_missing_host_metadata_never_guesses_a_provider_prefix(self) -> None:
        with self.assertRaisesRegex(ValueError, "E_NATIVE_MODEL_METADATA_REQUIRED"):
            configure_native_runtime(self.root, "omp", {"native-custom": "gpt-6.1-sol"})
        configure_native_runtime(self.root, "codex", {"native-custom": "gpt-6.1-sol"})
        self.assertEqual(native_runtime(self.root)["models"]["native-custom"], "gpt-6.1-sol")

    def test_execution_environment_policy_is_validated_without_affecting_bindings(self) -> None:
        from game_l10n.native_runtime import active_execution_environment_policy
        configure_native_runtime(self.root, "omp", {})
        path = self.root / "execution-environment.json"
        policy = {"schema": "parr0t.execution-environment-policy.v1", "host": "omp",
                  "authorization": "user_requested", "fallback_on": ["provider_operational_error", "execution_guard_denied"],
                  "fallback_strategy": "next_configured_native_route", "preserve_successes": True,
                  "preserve_isolation": True, "silent_model_switch_allowed": False}
        path.write_text(json.dumps(policy), encoding="utf-8")
        with patch.dict("os.environ", {"PARR0T_EXECUTION_ENVIRONMENT_POLICY": str(path)}):
            self.assertEqual(active_execution_environment_policy(self.root), policy)
            policy["silent_model_switch_allowed"] = True
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "E_EXECUTION_ENVIRONMENT_POLICY_INVALID"):
                active_execution_environment_policy(self.root)

    def test_host_specific_exact_models_and_explicit_override(self) -> None:
        configure_native_runtime(self.root, "codex", {})
        self.assertEqual(native_runtime(self.root)["models"], {
            "native-luna": "gpt-6-luna", "native-sol": "gpt-6.1-sol",
        })
        configure_native_runtime(self.root, "omp", {"native-sol": "fixture/selected-sol"})
        self.assertEqual(native_runtime(self.root)["models"], {
            "native-sol": "fixture/selected-sol",
        })
        configure_native_runtime(self.root, "omp", {})
        self.assertEqual(native_runtime(self.root)["models"]["native-sol"], "fixture/selected-sol")
        for value in ("default", "inherit", "@task", "fixture/*", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "E_NATIVE_RUNTIME_INVALID"):
                configure_native_runtime(self.root, "omp", {"native-luna": value})

    def test_fresh_omp_sol_wave_binds_exact_provider_path_and_effort(self) -> None:
        configure_native_runtime(self.root, "omp", {})
        self.assertEqual(native_runtime(self.root)["models"], {
            "native-luna": "openai-codex/gpt-6-luna:high", "native-sol": "openai-codex/gpt-6.1-sol:high",
        })
        prepare_native_tasks(
            self.root, self.source, self.segments, self.tasks,
            max_records=1, max_workers=2,
            route_binding={"provider": "native-sol", "attempt": 1},
        )
        step = self.step()
        relay = self.root / step["relay"]["dispatch"]
        contract = json.loads(relay.read_text(encoding="utf-8"))["launch_contract"]
        self.assertEqual(contract["model"], "openai-codex/gpt-6.1-sol:high")
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
            prepare_native_tasks(self.root, self.source, self.segments, self.tasks,
                                 route_binding={"provider": "native-luna", "attempt": 1})
        before = (self.tasks / "manifest.json").read_bytes()
        configure_native_runtime(self.root, "omp", {})
        self.assertEqual(self.step()["action"], "dispatch_isolated_wave_relay")
        self.assertEqual((self.tasks / "manifest.json").read_bytes(), before)

    def test_unavailable_relay_preserves_evidence_and_uses_approved_fresh_route(self) -> None:
        _, relay, payload = self.prepare()
        self.assertEqual(payload["launch_contract"]["model"], "openai-codex/gpt-6-luna:high")
        record_native_launch(self.root, relay, event="capability-unavailable")
        sealed = seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(sealed["issue_codes"], ["E_NATIVE_DISPATCH_UNAVAILABLE"])
        self.assertEqual(sealed["ended_tasks_recorded"], 0)
        self.assertEqual(list(self.results.glob("*.jsonl")), [])
        original = relay.read_bytes()
        blocked = self.step()
        self.assertEqual(blocked["action"], "repair_native_runtime")
        self.assertFalse(blocked["runtime_repair"]["isolation_relaxation_allowed"])
        self.prepare_replacement()
        resumed = self.step()
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        replacement = self.root / resumed["relay"]["dispatch"]
        self.assertNotEqual(replacement, relay)
        self.assertEqual(json.loads(replacement.read_text())["launch_contract"]["model"], "fixture/approved-successor")
        self.assertEqual(relay.read_bytes(), original)
        self.assertEqual(list(self.results.glob("*.jsonl")), [])


    def _resume_legacy_unavailable_wave(self, event: str, missing_fields: tuple[str, ...]) -> None:
        from game_l10n.native_runtime import launch_contract

        def legacy_contract(*args, **kwargs):
            contract = launch_contract(*args, **kwargs)
            for field in missing_fields:
                contract.pop(field, None)
            if "model_authorization" in missing_fields:
                contract["preflight"] = "inspect_available_dispatch_tool_and_exact_model_before_starting_any_worker"
            return contract

        with patch("game_l10n.native_runtime.launch_contract", side_effect=legacy_contract), \
             patch("game_l10n.native_pass.launch_contract", side_effect=legacy_contract):
            _, relay, payload = self.prepare()
            record_native_launch(self.root, relay, event=event)
            seal_native_wave_relay(self.root, relay, relay_ended=True)
        paths = [relay, self.tasks / "manifest.json",
                 *(self.root / binding["task"] for binding in payload["task_bindings"])]
        originals = {path: path.read_bytes() for path in paths}
        self.assertEqual(self.step()["action"], "repair_native_runtime")
        self.prepare_replacement()
        resumed = self.step()
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        self.assertNotEqual(self.root / resumed["relay"]["dispatch"], relay)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)
        for field in missing_fields:
            self.assertNotIn(field, json.loads(relay.read_text())["launch_contract"])


    def test_old_promptless_denial_preserves_contract_and_uses_fresh_successor(self) -> None:
        self._resume_legacy_unavailable_wave(
            'capability-unavailable', ('worker_prompt', 'relay_prompt'),
        )

    def test_old_policyless_denial_preserves_contract_and_uses_fresh_successor(self) -> None:
        self._resume_legacy_unavailable_wave('model-unavailable', (
            'model_authorization', 'unavailability_evidence', 'worker_prompt', 'relay_prompt',
            'lifecycle_observation', 'cancellation_priority', 'operational_failure_policy',
        ))

    def test_live_relay_wait_has_no_implicit_deadline(self) -> None:
        _, relay, _ = self.prepare()
        record_native_launch(self.root, relay, event="capability-available")
        self.assertIs(native_launch_summary(self.root, relay)["launch_observed"], True)
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
        self.write_rows(self.root / first["result"], [self.row("a")], model="gpt-6-luna")
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
                             max_records=1, max_workers=2,
                             route_binding={"provider": "native-luna", "model": "gpt-6-luna", "attempt": 1})
        initial = native_control_step(self.root, self.tasks / "manifest.json", self.results,
                                      require_relay_receipt=True)
        relay = self.root / initial["relay"]["dispatch"]
        first = json.loads(relay.read_text())["task_bindings"][0]
        self.write_rows(self.root / first["result"], [self.row("a")], model="gpt-6-luna")
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
        self.write_rows(self.root / binding["result"], [self.row("a")], model="gpt-6-luna")
        configure_native_runtime(self.root, "codex", {})
        self.assertEqual(self.step(resume_relay=True)["action"], "dispatch_isolated_wave_relay")
        launch = native_launch_summary(self.root, relay)
        self.assertEqual(launch["expected_workers"], 0)
        self.assertEqual(launch["issue_codes"], [])
        sealed = seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(sealed["counts"], {"expected_tasks": 1, "ready_tasks": 1, "pending_tasks": 0})
        self.assertEqual(sealed["issue_codes"], [])
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

    def test_changed_model_uses_new_manifest_without_importing_unverified_old_output(self) -> None:
        _, relay, payload = self.prepare()
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        binding = payload["task_bindings"][0]
        result = self.root / binding["result"]
        self.write_rows(result, [self.row("a")])
        original_result, original_relay = result.read_bytes(), relay.read_bytes()
        self.prepare_replacement(route="native-luna", model="fixture/approved-new-model")
        step = self.step()
        self.assertEqual(step["action"], "dispatch_isolated_wave_relay")
        replacement = self.root / step["relay"]["dispatch"]
        self.assertNotEqual(replacement, relay)
        self.assertEqual(json.loads(replacement.read_text())["launch_contract"]["model"], "fixture/approved-new-model")
        self.assertEqual(step["counts"]["imported_tasks"], 0)
        self.assertEqual(list(self.results.glob("*.jsonl")), [])
        self.assertEqual(result.read_bytes(), original_result)
        self.assertEqual(relay.read_bytes(), original_relay)


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

    def test_interrupted_fresh_replacement_dispatch_is_resumable(self) -> None:
        _, relay, _ = self.prepare()
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        original = relay.read_bytes()
        self.prepare_replacement(route="native-luna", model="fixture/approved-new-model")
        with patch("game_l10n.native_pass._write_wave_relay_dispatch", side_effect=OSError("fixture interrupted write")):
            with self.assertRaises(OSError):
                self.step()
        resumed = self.step()
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        replacement = self.root / resumed["relay"]["dispatch"]
        self.assertEqual(json.loads(replacement.read_text())["launch_contract"]["model"], "fixture/approved-new-model")
        self.assertEqual(relay.read_bytes(), original)


    def test_partial_launch_preserves_success_and_only_starts_remaining_task(self) -> None:
        _, relay, payload = self.prepare()
        model = payload["launch_contract"]["model"]
        first, second = payload["task_bindings"]
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task=first["task"], worker_id="worker-a", model=model)
        self.write_rows(self.root / first["result"], [self.row("a")])
        self.write_rows(self.root / second["result"], [self.row("b")])
        unverified = (self.root / second["result"]).read_bytes()
        original = (self.root / first["result"]).read_bytes()
        sealed = seal_native_wave_relay(self.root, relay, relay_ended=True)
        self.assertEqual(sealed["issue_codes"], ["E_NATIVE_WORKER_NOT_STARTED"])
        receipt = self.root / sealed["receipt"]
        original_receipt = receipt.read_bytes()
        self.assertEqual(self.step()["action"], "repair_native_runtime")
        original_dispatch = relay.read_bytes()
        resumed = self.step(resume_relay=True)
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(resumed["relay"]["dispatch"], relay.relative_to(self.root).as_posix())
        self.assertEqual((resumed["relay"]["tasks"], resumed["relay"]["records"]), (1, 1))
        self.assertEqual(relay.read_bytes(), original_dispatch)
        self.assertFalse((self.root / second["result"]).exists())
        self.assertEqual(receipt.with_name(
            receipt.name + ".legacy-" + hashlib.sha256(original_receipt).hexdigest()
        ).read_bytes(), original_receipt)
        result = self.root / second["result"]
        self.assertEqual(result.with_name(
            result.name + ".unstarted-original-" + hashlib.sha256(unverified).hexdigest()
        ).read_bytes(), unverified)
        self.assertEqual(self.step()["action"], "wait_for_active_wave")
        pending = record_native_launch(self.root, relay, event="capability-available")
        self.assertEqual(pending["unstarted_tasks"], [second["task"]])
        with self.assertRaisesRegex(ValueError, "worker already started"):
            record_native_launch(self.root, relay, event="worker-started", task=first["task"],
                                 worker_id="duplicate-worker", model=model)
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

    def test_unattempted_transport_error_requires_repair_without_consuming_semantic_budget(self) -> None:
        configure_native_runtime(self.root, "omp", {"native-luna": "fixture/luna"})
        plan = [{"provider": "native-luna", "model": "fixture/luna", "max_retries": 2}]
        first = self.root / "first.jsonl"
        self.write_rows(first, [self.row("a"), self.row("b", status="refused")])
        state_dir = self.root / "retry"
        initial = start_native_translation_retries(self.root, self.source, self.segments, first, state_dir,
                                                   route_plan=plan)
        locked = (self.root / initial["successful_results"]).read_bytes()
        failed = self.root / "failed.jsonl"
        self.write_rows(failed, [self.row("b", status="error", provider="native-control")])
        repaired = advance_native_translation_retries(self.root, state_dir, failed)
        self.assertEqual(repaired["next_action"], "repair_native_runtime")
        self.assertEqual(repaired["candidate_count"], 1)
        self.assertFalse(repaired["provider_attempt_consumed"])
        self.assertEqual(repaired["repair_needed_count"], 1)
        self.assertEqual((self.root / repaired["successful_results"]).read_bytes(), locked)
        with self.assertRaisesRegex(ValueError, "No native translation retry is pending"):
            advance_native_translation_retries(self.root, state_dir, failed)


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

    def test_changed_route_requires_new_namespace_without_reusing_prior_receipts(self) -> None:
        options = {"max_records": 1, "max_workers": 2,
                   "route_binding": {"provider": "native-luna", "attempt": 1}}
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks, **options)
        first = native_control_step(self.root, self.tasks / "manifest.json", self.results, require_relay_receipt=True)
        seal_native_wave_relay(self.root, self.root / first["relay"]["dispatch"], relay_ended=True)
        native_control_step(self.root, self.tasks / "manifest.json", self.results, require_relay_receipt=True)
        original = {p.name: p.read_bytes() for p in self.results.glob("*.jsonl")}
        options["route_binding"] = {"provider": "native-sol", "attempt": 1}
        with self.assertRaisesRegex(ValueError, "E_NATIVE_TASK_MANIFEST_IMMUTABLE"):
            prepare_native_tasks(self.root, self.source, self.segments, self.tasks, **options)
        new_tasks = self.root / "sol-tasks"
        prepare_native_tasks(self.root, self.source, self.segments, new_tasks, **options)
        step = native_control_step(self.root, new_tasks / "manifest.json", self.root / "sol-results", require_relay_receipt=True)
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

    def test_repair_pending_targets_do_not_starve_independent_semantic_retry(self) -> None:
        configure_native_runtime(self.root, "omp", {"native-luna": "fixture/luna"})
        plan = [{"provider": "native-luna", "model": "fixture/luna", "max_retries": 2}]
        segments = [*self.segments, Segment("c", "SOURCE C")]
        self.write_rows(self.source, [asdict(row) for row in segments])
        first = self.root / "first.jsonl"
        self.write_rows(first, [self.row("a"), self.row("b", status="refused"),
                                self.row("c", status="error", provider="native-control")])
        state_dir = self.root / "retry"
        state = start_native_translation_retries(self.root, self.source, segments, first, state_dir,
                                                 route_plan=plan)
        self.assertEqual(state["next_attempt"], 2)
        self.assertEqual(state["pending_count"], 1)
        self.assertEqual(state["deferred_failure_count"], 1)
        self.assertEqual(state["unresolved_count"], 2)
        pending = [json.loads(line)["id"] for line in (self.root / state["pending_input"]).read_text().splitlines()]
        self.assertEqual(pending, ["b"])
        retry = self.root / "recovered.jsonl"
        self.write_rows(retry, [self.row("b")])
        resumed = advance_native_translation_retries(self.root, state_dir, retry)
        self.assertEqual(resumed["next_action"], "repair_native_runtime")
        self.assertEqual(resumed["candidate_count"], 2)
        self.assertEqual(resumed["deferred_failure_count"], 0)
        pending = [json.loads(line)["id"] for line in (self.root / resumed["pending_input"]).read_text().splitlines()]
        self.assertEqual(pending, ["c"])


    def test_discovery_denial_does_not_repeat_unchanged_task_without_runtime_repair(self) -> None:
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
        self.assertEqual(second["next_action"], "repair_native_runtime")
        self.assertFalse(second["dispatch_allowed"])
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

    def test_ended_unstarted_probe_uses_fresh_task_after_explicit_binding_change(self) -> None:
        config, output, relay = self.prepare_probe(approved_recovery=True)
        original = (output / "task.json").read_bytes()
        payload = json.loads(relay.read_text())
        envelope = self.root / payload["expected_envelope"]
        envelope.write_text('{"stale":true}', encoding="utf-8")
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_model_unavailable")
        seal_data_plane_task(self.root, relay, worker_ended=True)
        configure_native_runtime(self.root, "omp", {"native-sol": "fixture/approved-new-model"})
        prepared = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        self.assertNotEqual(prepared["task_id"], payload["task_id"])
        bind_data_plane_launch(self.root, relay)
        replacement = json.loads(relay.read_text())
        self.assertNotEqual(replacement["expected_envelope"], payload["expected_envelope"])
        self.assertFalse((self.root / replacement["expected_envelope"]).exists())
        self.assertNotEqual((output / "task.json").read_bytes(), original)
        current_task = json.loads((output / "task.json").read_text())
        self.assertEqual(current_task["attempt"], json.loads(original)["attempt"])
        self.assertGreater(current_task["task_revision"], json.loads(original).get("task_revision", 0))
        history = output / "history" / payload["task_id"]
        self.assertEqual((history / "task.json").read_bytes(), original)
        self.assertEqual(json.loads((history / "receipt.json").read_text())["task_id"], payload["task_id"])
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertNotEqual(receipt["task_id"], prepared["task_id"])
        launch = native_launch_summary(self.root, relay)
        self.assertEqual(launch["model"], "fixture/approved-new-model")
        self.assertEqual(launch["started_workers"], 0)
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task="worker-0",
                             worker_id="new-probe", model="fixture/approved-new-model")
        self.assertEqual(native_launch_summary(self.root, relay)["started_workers"], 1)

    def test_changed_host_policy_reopens_only_ended_unstarted_admission_with_same_model(self) -> None:
        config, output, relay = self.prepare_probe()
        original_task = (output / "task.json").read_bytes()
        original = json.loads(relay.read_text())
        original_models = native_runtime(self.root)["models"]
        record_native_launch(self.root, relay, event="model-unavailable",
                             failure_code="host_dispatch_denied")
        seal_data_plane_task(self.root, relay, worker_ended=True)
        original_receipt = (output / "receipt.json").read_bytes()
        held = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        self.assertFalse(held["dispatch_allowed"])
        self.assertEqual(held["issue_codes"], ["native_route_repair_required"])

        configure_native_runtime(self.root, "omp", {},
                                 host_policy_fingerprint=stable_hash({"operator_role": original["launch_contract"]["model"]}))
        prepared = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        self.assertEqual(prepared["status"], "ready")
        self.assertNotEqual(prepared["task_id"], original["task_id"])
        bind_data_plane_launch(self.root, relay)
        replacement = json.loads(relay.read_text())
        self.assertEqual(replacement["launch_contract"]["model"], original["launch_contract"]["model"])
        self.assertEqual(native_runtime(self.root)["models"], original_models)
        history = output / "history" / original["task_id"]
        self.assertEqual((history / "task.json").read_bytes(), original_task)
        self.assertEqual((history / "receipt.json").read_bytes(), original_receipt)
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task="worker-0",
                             worker_id="approved-same-model-worker", model=original["launch_contract"]["model"])
        frozen = relay.read_bytes()
        configure_native_runtime(self.root, "omp", {},
                                 host_policy_fingerprint=stable_hash({"operator_role": "another-policy"}))
        with self.assertRaisesRegex(ValueError, "E_NATIVE_LAUNCH_INVALID"):
            bind_data_plane_launch(self.root, relay)
        self.assertEqual(relay.read_bytes(), frozen)

    def first_reconciliation(self) -> tuple[dict, Path, Path]:
        game = self.root / "game"
        game.mkdir()
        config = {"game_roots": [str(game)], "sdk_root": str(self.root / "sdk")}
        output = self.root / ".game-l10n/data-plane/inventory_reconciler"
        output.mkdir(parents=True)
        (output.parent.parent / "inventory-attestation.json").write_text(
            json.dumps({"reconciliation": {"generation": 2}}), encoding="utf-8",
        )
        configure_native_runtime(self.root, "omp", {}, host_policy_fingerprint="1" * 64)
        prior = prepare_data_plane_task(
            self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={},
        )
        relay = self.root / prior["relay_path"]
        bind_data_plane_launch(self.root, relay)
        return config, output, relay

    def prepare_stale_reconciliation(self, *, prior_partial: bool = False) -> tuple[dict, Path, Path]:
        config, output, relay = self.first_reconciliation()
        payload = json.loads(relay.read_bytes())
        if prior_partial:
            model = payload["launch_contract"]["model"]
            record_native_launch(self.root, relay, event="capability-available")
            record_native_launch(self.root, relay, event="worker-started", task="worker-0",
                                 worker_id="synthetic-prior", model=model)
            (output.parent.parent / "inventory-attestation.json").write_text(json.dumps({
                "reconciliation": {"generation": 3},
                "primary_id_fingerprint": "synthetic-ids",
                "primary_identity_binding": {
                    "policy": "append_only_preserve_existing_id_order",
                    "id_fingerprint": "synthetic-ids", "records": 2,
                },
            }), encoding="utf-8")
            (self.root / payload["expected_envelope"]).write_text(json.dumps({
                "schema": "parr0t.control-envelope.v2", "worker_role": "inventory_reconciler",
                "stage": "inventory", "status": "partial", "target_ids": [],
                "counts": {"total": 2, "success": 1, "pending": 1},
                "issue_codes": ["task_incomplete"], "queue_transitions": [],
                "artifact_paths": [".game-l10n/inventory-attestation.json"], "route_evidence_path": "",
            }), encoding="utf-8")
        else:
            record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_dispatch_denied")
        prior_receipt = seal_data_plane_task(self.root, relay, worker_ended=True)
        self.assertEqual(prior_receipt["status"], "partial" if prior_partial else "error")
        configure_native_runtime(self.root, "omp", {}, host_policy_fingerprint="2" * 64)
        replacement = prepare_data_plane_task(
            self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={},
        )
        self.assertNotEqual(replacement["task_id"], payload["task_id"])
        relay = self.root / replacement["relay_path"]
        bind_data_plane_launch(self.root, relay)
        return config, output, relay

    def test_stale_unsubmitted_reconciliation_gets_new_address_without_relabeling(self) -> None:
        config, output, old_relay = self.prepare_stale_reconciliation()
        receipt = output / "receipt.json"
        preserved = {path: path.read_bytes() for path in (old_relay, old_relay.with_name("task.json"), receipt)}
        old = json.loads(preserved[old_relay])
        configure_native_runtime(self.root, "omp", {}, host_policy_fingerprint="3" * 64)
        fresh = prepare_data_plane_task(
            self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={"current": True},
        )
        relay_path = self.root / fresh["relay_path"]
        self.assertNotEqual(relay_path, old_relay)
        self.assertNotEqual(fresh["task_id"], old["task_id"])
        bind_data_plane_launch(self.root, relay_path)
        current = json.loads(relay_path.read_bytes())
        task = json.loads((self.root / current["task_path"]).read_bytes())
        self.assertEqual(task["sdk_activation"], {"current": True})
        self.assertEqual(current["attempt"], old["attempt"])
        self.assertEqual(current["launch_contract"]["model"], old["launch_contract"]["model"])
        self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)

        repeated = prepare_data_plane_task(
            self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={"current": True},
        )
        self.assertEqual(repeated, fresh)
        with self.assertRaisesRegex((ValueError, ConfigurationError), "relay was superseded"):
            record_native_launch(self.root, old_relay, event="capability-available")
        with self.assertRaisesRegex(ConfigurationError, "relay was superseded"):
            seal_data_plane_task(self.root, old_relay, worker_ended=True)
        self.assertEqual(receipt.read_bytes(), preserved[receipt])
        record_native_launch(self.root, relay_path, event="model-unavailable", failure_code="host_dispatch_denied")
        seal_data_plane_task(self.root, relay_path, worker_ended=True)
        self.assertEqual(json.loads(receipt.read_bytes())["task_id"], fresh["task_id"])
        self.assertEqual(old_relay.read_bytes(), preserved[old_relay])

    def test_prior_partial_receipt_does_not_block_superseding_unadmitted_reconciler(self) -> None:
        config, output, old_relay = self.prepare_stale_reconciliation(prior_partial=True)
        receipt = output / "receipt.json"
        original = receipt.read_bytes()
        old = json.loads(old_relay.read_bytes())
        configure_native_runtime(self.root, "omp", {}, host_policy_fingerprint="3" * 64)
        fresh = prepare_data_plane_task(
            self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={},
        )
        relay = self.root / fresh["relay_path"]
        self.assertNotEqual(relay, old_relay)
        bind_data_plane_launch(self.root, relay)
        self.assertEqual(json.loads(relay.read_bytes())["attempt"], old["attempt"])
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(native_launch_summary(self.root, relay)["unstarted_tasks"], ["worker-0"])

    def test_missing_receipt_does_not_block_superseding_unadmitted_reconciler(self) -> None:
        config, output, old_relay = self.prepare_stale_reconciliation()
        receipt = output / "receipt.json"
        receipt.unlink()
        configure_native_runtime(self.root, "omp", {}, host_policy_fingerprint="3" * 64)
        fresh = prepare_data_plane_task(
            self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={},
        )
        relay = self.root / fresh["relay_path"]
        self.assertNotEqual(relay, old_relay)
        bind_data_plane_launch(self.root, relay)
        self.assertFalse(receipt.exists())

    def write_incomplete_reconciliation_receipt(self, output: Path, relay: Path) -> tuple[str, bytes]:
        task_id = json.loads(relay.read_bytes())["task_id"]
        receipt = output / "receipt.json"
        payload = json.loads(receipt.read_bytes())
        payload["task_id"] = task_id
        payload["execution"]["complete"] = False
        original = json.dumps(payload).encode("utf-8")
        receipt.write_bytes(original)
        return task_id, original

    def test_explicit_discard_retires_only_the_exact_incomplete_receipt(self) -> None:
        config, output, old_relay = self.prepare_stale_reconciliation()
        task_id, original = self.write_incomplete_reconciliation_receipt(output, old_relay)
        receipt = output / "receipt.json"
        configure_native_runtime(self.root, "omp", {}, host_policy_fingerprint="3" * 64)
        options = {"worker_role": "inventory_reconciler", "config": config, "sdk_activation": {}}
        pending = prepare_data_plane_task(self.root, output, **options)
        self.assertFalse(pending["dispatch_allowed"])
        self.assertEqual(pending["task_id"], task_id)
        with self.assertRaisesRegex(ConfigurationError, "E_NATIVE_RECEIPT_DISCARD_INVALID"):
            prepare_data_plane_task(self.root, output, **options, discard_unverified_receipt="0" * 64)
        self.assertEqual(receipt.read_bytes(), original)

        fresh = prepare_data_plane_task(self.root, output, **options, discard_unverified_receipt=task_id)
        relay = self.root / fresh["relay_path"]
        self.assertNotEqual(relay, old_relay)
        bind_data_plane_launch(self.root, relay)
        self.assertFalse(receipt.exists())
        discarded = old_relay.with_name(f"discarded-receipt-{hashlib.sha256(original).hexdigest()}.json")
        self.assertEqual(discarded.read_bytes(), original)
        with self.assertRaisesRegex(ConfigurationError, "relay was superseded"):
            seal_data_plane_task(self.root, old_relay, worker_ended=True)
        record_native_launch(self.root, relay, event="model-unavailable", failure_code="host_dispatch_denied")
        seal_data_plane_task(self.root, relay, worker_ended=True)
        self.assertEqual(json.loads(receipt.read_bytes())["task_id"], fresh["task_id"])

    def test_stale_reconciliation_with_host_reservation_cannot_be_replaced(self) -> None:
        config, output, relay = self.prepare_stale_reconciliation()
        original = relay.read_bytes()
        fingerprint = hashlib.sha256(original).hexdigest()
        key = hashlib.sha256(f"{fingerprint}\0worker-0".encode()).hexdigest()
        admission = self.root / ".game-l10n/native/omp-launch-admissions" / f"{key}.json"
        admission.parent.mkdir(parents=True, exist_ok=True)
        admission.write_text(json.dumps({
            "schema": "parr0t.omp-native-admission.v1", "relay_sha256": fingerprint,
            "task": "worker-0", "parent_id": "fixture-relay", "state": "reserved", "worker_id": None,
        }), encoding="utf-8")
        configure_native_runtime(self.root, "omp", {}, host_policy_fingerprint="3" * 64)
        held = prepare_data_plane_task(
            self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={},
        )
        self.assertEqual(self.root / held["relay_path"], relay)
        with self.assertRaisesRegex(ValueError, "cannot rebind a started or unverified worker"):
            bind_data_plane_launch(self.root, relay)
        self.assertEqual(relay.read_bytes(), original)
        self.assertEqual(json.loads(admission.read_bytes())["state"], "reserved")

        task_id, receipt_bytes = self.write_incomplete_reconciliation_receipt(output, relay)
        with self.assertRaisesRegex(ConfigurationError, "E_NATIVE_RECEIPT_DISCARD_INVALID"):
            prepare_data_plane_task(
                self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={},
                discard_unverified_receipt=task_id,
            )
        self.assertEqual((output / "receipt.json").read_bytes(), receipt_bytes)

    def test_rotation_waits_for_bound_host_end_before_reissuing_reconciler(self) -> None:
        config, output, old_relay = self.first_reconciliation()
        original = old_relay.read_bytes()
        old = json.loads(original)
        model = old["launch_contract"]["model"]
        record_native_launch(self.root, old_relay, event="capability-available")
        record_native_launch(self.root, old_relay, event="worker-started", task="worker-0",
                             worker_id="synthetic-worker", model=model)
        record_control_plane_leak(self.root, "parent_opened_data_plane")
        acknowledge_control_plane_rotation(self.root, self.root / CONTROL_HANDOFF_PATH)
        establish_control_plane_entry(self.root)
        bind_omp_isolation(self, self.root)
        configure_native_runtime(self.root, "omp", {})

        options = {"worker_role": "inventory_reconciler", "config": config, "sdk_activation": {}}
        with self.assertRaisesRegex(ValueError, "E_NATIVE_WORKER_TERMINATION_UNVERIFIED"):
            prepare_data_plane_task(self.root, output, **options)

        relay_digest = hashlib.sha256(original).hexdigest()
        admission_key = hashlib.sha256(f"{relay_digest}\0worker-0".encode()).hexdigest()
        admission = self.root / ".game-l10n/native/omp-launch-admissions" / f"{admission_key}.json"
        admission.parent.mkdir(parents=True)
        record = {"schema": "parr0t.omp-native-admission.v1", "relay_sha256": relay_digest,
                  "task": "worker-0", "parent_id": "synthetic-relay", "worker_id": "synthetic-worker",
                  "state": "started"}
        admission.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "E_NATIVE_WORKER_ACTIVE"):
            prepare_data_plane_task(self.root, output, **options)
        self.assertEqual(old_relay.read_bytes(), original)
        self.assertFalse((output / "receipt.json").exists())

        admission.write_text(json.dumps({**record, "state": "ended"}), encoding="utf-8")
        rebound = prepare_data_plane_task(self.root, output, **options)
        relay = self.root / rebound["relay_path"]
        self.assertNotEqual(relay, old_relay)
        bind_data_plane_launch(self.root, relay)
        self.assertEqual(json.loads(relay.read_bytes())["attempt"], old["attempt"])
        self.assertEqual(old_relay.read_bytes(), original)

    def test_inflight_reconciler_seal_cannot_overwrite_successor_receipt(self) -> None:
        config, output, old_relay = self.first_reconciliation()
        receipt_path = output / "receipt.json"
        old = json.loads(old_relay.read_bytes())
        record_native_launch(self.root, old_relay, event="capability-available")
        record_native_launch(self.root, old_relay, event="worker-started", task="worker-0",
                             worker_id="synthetic-worker", model=old["launch_contract"]["model"])
        publish = data_plane_task._write_data_plane_receipt

        def publish_after_successor(work_dir, relay, path, receipt):
            # Pause the first publication at its lock boundary while a duplicate
            # seals the old task and the successor completes.
            publish(work_dir, relay, path, receipt)
            original = receipt_path.read_bytes()
            fresh = prepare_data_plane_task(
                self.root, output, worker_role="inventory_reconciler", config=config, sdk_activation={},
            )
            fresh_relay = self.root / fresh["relay_path"]
            self.assertNotEqual(fresh_relay, old_relay)
            bind_data_plane_launch(self.root, fresh_relay)
            record_native_launch(self.root, fresh_relay, event="model-unavailable", failure_code="host_dispatch_denied")
            with patch.object(data_plane_task, "_write_data_plane_receipt", publish):
                seal_data_plane_task(self.root, fresh_relay, worker_ended=True)
            self.assertEqual((old_relay.parent / "sealed-receipt.json").read_bytes(), original)
            self.assertEqual(json.loads(receipt_path.read_bytes())["task_id"], fresh["task_id"])
            return publish(work_dir, relay, path, receipt)

        with patch.object(data_plane_task, "_write_data_plane_receipt", publish_after_successor):
            with self.assertRaisesRegex(ConfigurationError, "relay was superseded"):
                seal_data_plane_task(self.root, old_relay, worker_ended=True)
        with self.assertRaisesRegex(ConfigurationError, "relay was superseded"):
            seal_data_plane_task(self.root, old_relay, worker_ended=True)
        self.assertNotEqual(json.loads(receipt_path.read_bytes())["task_id"], old["task_id"])

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
        _, output, relay = self.prepare_probe(approved_recovery=True)
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
