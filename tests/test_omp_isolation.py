from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from game_l10n.control_plane import control_plane_state, establish_control_plane_entry, record_control_plane_leak
from game_l10n.data_plane_task import prepare_data_plane_task, seal_data_plane_task
from game_l10n.errors import ConfigurationError
from game_l10n.models import Segment
from game_l10n.native_host import configure_native_host
from game_l10n.native_pass import (
    import_native_wave, native_control_step, prepare_native_tasks, seal_native_wave_relay,
)
from game_l10n.native_runtime import (
    bind_data_plane_launch, configure_native_runtime, launch_contract, native_launch_summary, native_runtime,
    record_native_launch,
)
from game_l10n.omp_isolation import (
    ISOLATION_ENV, ISOLATION_ISSUE, ISOLATION_PATH, guard_sha256, require_omp_isolation,
)
from game_l10n.store import stable_hash
from tests.omp_isolation_fixture import bind_omp_isolation, install_omp_isolation


class OmpIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.proof = bind_omp_isolation(self, self.root)
        self.registration = self.root / ISOLATION_PATH
        self.tasks, self.results = self.root / "tasks", self.root / "results"
        self.source = self.root / "source.jsonl"
        self.segments = [Segment("synthetic-a", "SYNTHETIC SOURCE")]
        self.source.write_text(json.dumps(asdict(self.segments[0])) + "\n", encoding="utf-8")

    def write_proof(self, proof):
        self.registration.write_text(json.dumps(proof), encoding="utf-8")

    def step(self, **kwargs):
        return native_control_step(self.root, self.tasks / "manifest.json", self.results,
                                  require_runtime=True, require_relay_receipt=True, **kwargs)

    def prepare(self):
        configure_native_runtime(self.root, "omp", {})
        prepare_native_tasks(self.root, self.source, self.segments, self.tasks,
                             route_binding={"provider": "native-luna", "attempt": 1})
        step = self.step()
        relay = self.root / step["relay"]["dispatch"]
        return step, relay, json.loads(relay.read_text(encoding="utf-8"))

    def assert_isolation_error(self, operation):
        with self.assertRaises(ValueError) as error:
            operation()
        self.assertEqual(str(error.exception), ISOLATION_ISSUE)

    def test_missing_malformed_stale_tampered_wrong_session_are_categorical(self):
        cases = [
            None, "{SYNTHETIC PRIVATE CONTENT", [],
            {**self.proof, "control_generation": self.proof["control_generation"] + 1},
            {**self.proof, "control_generation": True},
            {**self.proof, "guard_sha256": "0" * 64},
            {**self.proof, "session_id": "different-session"},
            {**self.proof, "session_id": ""},
            {**self.proof, "process_id": True},
            {**self.proof, "process_id": 0},
            {**self.proof, "extra": "SYNTHETIC PRIVATE CONTENT"},
        ]
        for index, proof in enumerate(cases):
            with self.subTest(case=index):
                if proof is None:
                    self.registration.unlink(missing_ok=True)
                elif isinstance(proof, str):
                    self.registration.write_text(proof, encoding="utf-8")
                else:
                    self.write_proof(proof)
                self.assert_isolation_error(lambda: require_omp_isolation(self.root))
                self.assert_isolation_error(lambda: configure_native_runtime(self.root, "omp", {}))
                self.assertFalse((self.root / ".game-l10n/native/runtime.json").exists())
        self.write_proof(self.proof)
        with patch.dict(os.environ, {ISOLATION_ENV: ""}):
            self.assert_isolation_error(lambda: require_omp_isolation(self.root))

    def test_current_actual_template_proof_and_parent_transport_contract(self):
        template = files("game_l10n").joinpath("templates", "omp-isolation-guard.mjs").read_bytes()
        self.assertEqual(guard_sha256(), sha256(template).hexdigest())
        self.assertEqual(require_omp_isolation(self.root), self.proof)
        step, relay, payload = self.prepare()
        launch = payload["launch_contract"]
        self.assertEqual(launch["message_isolation"], self.proof)
        marker = "PARR0T_OMP_RELAY=" + relay.relative_to(self.root).as_posix()
        self.assertEqual(step["relay"]["prompt"], marker)
        self.assertEqual(launch["relay_prompt_line"], marker)
        self.assertEqual(launch["worker_prompt_line"], marker)
        transport = step["relay"]["tool_transport"]
        self.assertEqual(transport, launch["tool_transport"])
        self.assertEqual(transport["subagent_python_tool"], "parr0t_exec")
        self.assertFalse(transport["outbound_messaging_allowed"])
        self.assertEqual(transport["relay_python_argv_prefix"], ["-m", "game_l10n.cli"])
        self.assertEqual(transport["role_bindings"]["2"], "semantic_worker")
        self.assertEqual(record_native_launch(self.root, relay, event="capability-available")["started_workers"], 0)
        summary = record_native_launch(self.root, relay, event="worker-started", task=launch["tasks"][0],
                                       worker_id="synthetic-worker", model=launch["model"])
        self.assertEqual(summary["started_workers"], 1)

    def test_template_changes_rejected_before_worker_start_or_import(self):
        _, relay, payload = self.prepare()
        record_native_launch(self.root, relay, event="capability-available")
        ledger = relay.with_name(relay.stem + "-launch.json")
        original = ledger.read_bytes()
        resources = self.root / "synthetic-package"
        template = resources / "templates/omp-isolation-guard.mjs"
        template.parent.mkdir(parents=True)
        template.write_bytes(b"// synthetic changed installed guard\n")
        with patch("game_l10n.omp_isolation.files", return_value=resources):
            self.assert_isolation_error(lambda: launch_contract(self.root, "native-luna", []))
            self.assert_isolation_error(lambda: record_native_launch(self.root, relay, event="worker-started",
                task=payload["launch_contract"]["tasks"][0], worker_id="must-not-start",
                model=payload["launch_contract"]["model"]))
            self.assertEqual(ledger.read_bytes(), original)
            sealed = seal_native_wave_relay(self.root, relay, relay_ended=True)
            self.assertEqual(sealed["issue_codes"], [ISOLATION_ISSUE])
            self.assertEqual(sealed["ended_tasks_recorded"], 0)
            self.assert_isolation_error(lambda: import_native_wave(self.root, self.tasks / "manifest.json", self.results, 1))
            blocked = self.step()
            self.assertEqual(blocked["action"], "repair_native_runtime")
            self.assertFalse(blocked["runtime_repair"]["automatic_rotation_allowed"])
            self.assertEqual(blocked["route_binding"]["attempt"], 1)
            self.assertEqual(list(self.results.glob("*.jsonl")), [])

    def test_safe_host_repair_reuses_same_wave_task_and_attempt(self):
        initial, relay, payload = self.prepare()
        record_native_launch(self.root, relay, event="capability-available")
        repaired = install_omp_isolation(self.root)
        with patch.dict(os.environ, {ISOLATION_ENV: repaired["session_id"]}):
            self.assertEqual(native_launch_summary(self.root, relay)["issue_codes"], [ISOLATION_ISSUE])
            seal_native_wave_relay(self.root, relay, relay_ended=True)
            self.assertEqual(self.step()["action"], "repair_native_runtime")
            resumed = self.step(resume_relay=True)
            updated = json.loads(relay.read_text(encoding="utf-8"))
            self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
            self.assertEqual(resumed["wave"], initial["wave"])
            self.assertEqual(resumed["route_binding"], initial["route_binding"])
            self.assertEqual(updated["task_bindings"], payload["task_bindings"])
            self.assertEqual(updated["launch_contract"]["message_isolation"], repaired)
            self.assertEqual(native_launch_summary(self.root, relay)["started_workers"], 0)
            self.assertTrue(list(relay.parent.glob(relay.name + ".legacy-*")))

    def test_started_legacy_unsafe_launch_is_never_certified_or_relabelled(self):
        _, relay, payload = self.prepare()
        launch = payload["launch_contract"]
        record_native_launch(self.root, relay, event="capability-available")
        record_native_launch(self.root, relay, event="worker-started", task=launch["tasks"][0],
                             worker_id="synthetic-old-worker", model=launch["model"])
        # Recreate a real pre-isolation checkpoint without changing worker evidence.
        for key in ("message_isolation", "tool_transport", "relay_prompt_line", "worker_prompt_line"):
            payload["launch_contract"].pop(key)
        payload["dispatch_fingerprint"] = stable_hash({key: value for key, value in payload.items()
                                                       if key != "dispatch_fingerprint"})
        relay.write_text(json.dumps(payload), encoding="utf-8")
        ledger_path = relay.with_name(relay.stem + "-launch.json")
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["relay_fingerprint"] = stable_hash(payload)
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        before = relay.read_bytes(), ledger_path.read_bytes()
        summary = native_launch_summary(self.root, relay)
        self.assertEqual(summary["started_workers"], 0)
        self.assertEqual(summary["issue_codes"], [ISOLATION_ISSUE])
        self.assertEqual(native_runtime(self.root, allow_stale_generation=True)["host"], "omp")
        self.assert_isolation_error(lambda: record_native_launch(self.root, relay, event="capability-available"))
        seal_native_wave_relay(self.root, relay, relay_ended=True)
        with self.assertRaisesRegex(ValueError, "cannot rebind a started"):
            self.step(resume_relay=True)
        self.assertEqual((relay.read_bytes(), ledger_path.read_bytes()), before)

    def test_data_plane_stale_proof_cannot_bind_and_safe_repair_keeps_task_ordinal(self):
        (self.root / "game-l10n.json").write_text("{}", encoding="utf-8")
        establish_control_plane_entry(self.root)
        configure_native_runtime(self.root, "omp", {})
        source_root = self.root / "synthetic-source-root"
        source_root.mkdir()
        config = {"game_roots": [str(source_root)], "sdk_root": str(self.root / "synthetic-sdk"),
                  "sdk_selection": {"inventory_fingerprint": "synthetic-inventory"}}
        output = self.root / ".game-l10n/data-plane/source_probe"
        prepared = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
        relay = self.root / prepared["relay_path"]
        bind_data_plane_launch(self.root, relay)
        record_native_launch(self.root, relay, event="capability-available")
        original_task = (output / "task.json").read_bytes()
        original_relay = relay.read_bytes()
        self.write_proof({**self.proof, "control_generation": self.proof["control_generation"] + 1})
        self.assert_isolation_error(lambda: bind_data_plane_launch(self.root, relay))
        self.assertEqual(relay.read_bytes(), original_relay)
        sealed = seal_data_plane_task(self.root, relay, worker_ended=True)
        self.assertEqual(sealed["issue_codes"], [ISOLATION_ISSUE])
        repaired = install_omp_isolation(self.root)
        with patch.dict(os.environ, {ISOLATION_ENV: repaired["session_id"]}):
            resumed = prepare_data_plane_task(self.root, output, worker_role="source_probe", config=config)
            self.assertEqual(resumed["task_id"], prepared["task_id"])
            self.assertEqual((output / "task.json").read_bytes(), original_task)
            bind_data_plane_launch(self.root, relay)
            updated = json.loads(relay.read_text(encoding="utf-8"))
            self.assertEqual(updated["task_id"], prepared["task_id"])
            self.assertEqual(updated["attempt"], json.loads(original_relay)["attempt"])
            self.assertEqual(updated["launch_contract"]["message_isolation"], repaired)

    def test_codex_and_registered_host_api_do_not_require_omp_proof(self):
        self.registration.unlink()
        configure_native_runtime(self.root, "codex", {})
        self.assertNotIn("message_isolation", launch_contract(self.root, "native-luna", []))
        configure_native_host(self.root, "synthetic-host", "http://127.0.0.1:12345")
        configure_native_runtime(self.root, "synthetic-host", {"native-luna": "fixture/luna", "native-sol": "fixture/sol"},
                                 transport="native-host-api")
        self.assertEqual(native_runtime(self.root)["transport"], "native-host-api")
        configure_native_host(self.root, "omp", "http://127.0.0.1:12345")
        configure_native_runtime(self.root, "omp", {}, transport="native-host-api")
        self.assertEqual(native_runtime(self.root)["transport"], "native-host-api")

    def test_registration_never_clears_rotation_or_auto_resumes(self):
        self.prepare()
        marked = record_control_plane_leak(self.root, "unvalidated_worker_response")
        original = (self.root / ".game-l10n/control-plane-state.json").read_bytes()
        install_omp_isolation(self.root, session_id=self.proof["session_id"])
        with self.assertRaises(ConfigurationError):
            self.step(resume_relay=True)
        with self.assertRaises(ConfigurationError):
            configure_native_runtime(self.root, "omp", {})
        self.assertEqual(control_plane_state(self.root)["status"], "rotation_required")
        self.assertEqual(control_plane_state(self.root)["generation"], marked["generation"])
        self.assertEqual((self.root / ".game-l10n/control-plane-state.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
