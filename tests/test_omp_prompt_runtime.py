from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from game_l10n.cli import main
from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.data_plane_task import prepare_data_plane_task
from game_l10n.models import Segment
from game_l10n.native_pass import native_control_step, prepare_native_tasks
from game_l10n.native_runtime import (
    bind_data_plane_launch, configure_native_runtime, launch_contract,
    native_runtime, record_native_launch,
)
from game_l10n.omp_isolation import ISOLATION_ENV, ISOLATION_ISSUE
from game_l10n.store import stable_hash
from game_l10n.workspace import (
    initialize_workspace, load_config, record_sdk_activation, record_sdk_evaluation, record_sdk_selection, refresh_sdk_inventory,
)
from tests.omp_isolation_fixture import install_omp_isolation


class OmpPromptRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="parr0t synthetic ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "work"
        environment = patch.dict(os.environ, {ISOLATION_ENV: ""})
        environment.start()
        self.addCleanup(environment.stop)
        sdk = self.root.parent / "sdk"
        sdk.mkdir()
        initialize_workspace(self.root, sdk, git_choice="disabled")
        establish_control_plane_entry(self.root)

    def run_cli(self, argv):
        completed = subprocess.run(argv, cwd=self.root, capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def bind_cli(self):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["native-runtime", "--workspace", str(self.root), "--host", "omp",
                         "--model", "native-sol=fixture/exact-sol"])
        self.assertEqual(code, 0, stderr.getvalue())
        return json.loads(stdout.getvalue())

    def prepare_wave(self):
        segments = [Segment("synthetic-a", "SYNTHETIC A"), Segment("synthetic-b", "SYNTHETIC B")]
        source = self.root / "source.jsonl"
        source.write_text("".join(json.dumps(asdict(segment)) + "\n" for segment in segments), encoding="utf-8")
        tasks, results = self.root / "tasks", self.root / "results"
        prepare_native_tasks(self.root, source, segments, tasks, max_records=1, max_workers=2,
                             route_binding={"provider": "native-luna", "attempt": 1})
        step = native_control_step(self.root, tasks / "manifest.json", results,
                                   require_runtime=True, require_relay_receipt=True)
        relay = self.root / step["relay"]["dispatch"]
        return tasks, results, step, relay, json.loads(relay.read_text(encoding="utf-8"))

    def test_prompt_cli_runs_complete_bound_wave_with_normal_python(self):
        sdk = self.root.parent / "sdk"
        for relative, content in {
            "pyproject.toml": "[project]\nname='synthetic-sdk'\n",
            "docs/fixture-sdk.md": "Synthetic format contract.\n",
            "src/game_l10n/__init__.py": "",
            "src/game_l10n/adapter.py": "SYNTHETIC_FORMAT = 1\n",
        }.items():
            path = sdk / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        refresh_sdk_inventory(self.root, runtime_root=sdk,
                              portable_branch="sdk-fixture-1.0", portable_commit="a" * 40)
        record_sdk_evaluation(self.root, "sdk-fixture-1.0", "compatible", "public_contract_match",
                              ["docs/fixture-sdk.md"], target_engine="fixture", target_version="1.0")
        record_sdk_selection(self.root, "reuse", "sdk-fixture-1.0",
                             target_engine="fixture", target_version="1.0")
        record_sdk_activation(self.root, runtime_root=sdk)
        runtime = self.bind_cli()
        self.assertEqual(runtime["omp_isolation"], "prompt")
        tasks, results, step, relay, payload = self.prepare_wave()
        launch = payload["launch_contract"]
        self.assertFalse(launch["message_isolation"]["host_enforced"])
        self.assertEqual(
            launch["lifecycle_observation"],
            "finite_interruptible_per_call_only_not_a_job_timeout",
        )
        self.assertEqual(step["relay"]["tool_transport"], launch["tool_transport"])
        self.assertFalse(launch["tool_transport"]["required_extension"])
        self.assertEqual(launch["model"], "openai-codex/gpt-6-luna")
        commands = payload["command_argv"]
        self.run_cli(commands["capability-available"])
        for index, binding in enumerate(payload["task_bindings"]):
            start = [f"synthetic-worker-{index}" if value == "<worker-id>" else value
                     for value in commands["worker-started"][binding["task"]]]
            recorded = self.run_cli(start)
            self.assertEqual(recorded["started_workers"], index + 1)
            result = self.root / binding["result"]
            result.parent.mkdir(parents=True, exist_ok=True)
            result.write_text(json.dumps({"target_id": f"synthetic-{'ab'[index]}",
                "status": "success", "text": "\ud569\uc131 \ub300\uc0c1", "provider": "native-luna", "issues": []}) + "\n",
                encoding="utf-8")
        self.run_cli(commands["seal"])
        completed = native_control_step(self.root, tasks / "manifest.json", results,
                                        require_runtime=True, require_relay_receipt=True)
        self.assertEqual(completed["action"], "aggregate_completed_pass")
        self.assertEqual(completed["counts"]["imported_tasks"], 2)
        self.assertEqual(completed["route_binding"]["attempt"], 1)
        self.assertEqual(native_runtime(self.root)["models"]["native-sol"], "fixture/exact-sol")

    def test_prompt_discovery_argv_seals_missing_output_without_fake_success(self):
        self.bind_cli()
        source = self.root / "synthetic-source"
        source.mkdir()
        config = load_config(self.root)
        config["game_roots"] = [str(source)]
        output = self.root / ".game-l10n/data-plane/source_probe"
        prepared = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        relay = self.root / prepared["relay_path"]
        bind_data_plane_launch(self.root, relay)
        payload = json.loads(relay.read_text(encoding="utf-8"))
        commands = payload["command_argv"]
        self.run_cli(commands["capability-available"])
        self.run_cli(["synthetic-probe" if value == "<worker-id>" else value
                      for value in commands["worker-started"]["worker-0"]])
        sealed = self.run_cli(commands["seal"])
        self.assertEqual(sealed["status"], "error")
        self.assertIn("task_incomplete", sealed["issue_codes"])
        self.assertNotIn(ISOLATION_ISSUE, sealed["issue_codes"])

    def test_existing_guard_binding_is_not_silently_downgraded(self):
        proof = install_omp_isolation(self.root)
        with patch.dict(os.environ, {ISOLATION_ENV: proof["session_id"]}):
            configure_native_runtime(self.root, "omp", {"native-sol": "fixture/locked-sol"})
        path = self.root / ".game-l10n/native/runtime.json"
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, ISOLATION_ISSUE):
            configure_native_runtime(self.root, "omp", {})
        self.assertEqual(path.read_bytes(), original)
        changed = configure_native_runtime(self.root, "omp", {}, omp_isolation="prompt")
        self.assertEqual(changed["models"]["native-sol"], "fixture/locked-sol")
        self.assertFalse(launch_contract(self.root, "native-sol", [])["message_isolation"]["host_enforced"])

    def test_legacy_runtime_without_mode_remains_guarded(self):
        proof = install_omp_isolation(self.root)
        with patch.dict(os.environ, {ISOLATION_ENV: proof["session_id"]}):
            configure_native_runtime(self.root, "omp", {})
        path = self.root / ".game-l10n/native/runtime.json"
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("omp_isolation")
        legacy.pop("fingerprint")
        legacy["fingerprint"] = stable_hash(legacy)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, ISOLATION_ISSUE):
            configure_native_runtime(self.root, "omp", {})

    def test_active_guard_cannot_be_misrepresented_as_prompt_only(self):
        proof = install_omp_isolation(self.root)
        with patch.dict(os.environ, {ISOLATION_ENV: proof["session_id"]}):
            with self.assertRaisesRegex(ValueError, ISOLATION_ISSUE):
                configure_native_runtime(self.root, "omp", {}, omp_isolation="prompt")
        self.assertFalse((self.root / ".game-l10n/native/runtime.json").exists())

    def test_mode_change_cannot_relabel_started_worker(self):
        proof = install_omp_isolation(self.root)
        with patch.dict(os.environ, {ISOLATION_ENV: proof["session_id"]}):
            configure_native_runtime(self.root, "omp", {})
            tasks, results, _, relay, payload = self.prepare_wave()
            record_native_launch(self.root, relay, event="capability-available")
            record_native_launch(self.root, relay, event="worker-started", task=payload["launch_contract"]["tasks"][0],
                                 worker_id="synthetic-started-worker", model=payload["launch_contract"]["model"])
        ledger = relay.with_name(relay.stem + "-launch.json")
        original = relay.read_bytes(), ledger.read_bytes()
        configure_native_runtime(self.root, "omp", {}, omp_isolation="prompt")
        with self.assertRaisesRegex(ValueError, "E_NATIVE_LAUNCH_INVALID"):
            native_control_step(self.root, tasks / "manifest.json", results, resume_relay=True,
                                require_runtime=True, require_relay_receipt=True)
        self.assertEqual((relay.read_bytes(), ledger.read_bytes()), original)

    def test_non_omp_hosts_reject_omp_mode_without_changing_runtime(self):
        self.bind_cli()
        path = self.root / ".game-l10n/native/runtime.json"
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "E_NATIVE_RUNTIME_INVALID"):
            configure_native_runtime(self.root, "codex", {}, omp_isolation="prompt")
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
