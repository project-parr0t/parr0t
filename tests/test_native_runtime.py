from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.data_plane_task import prepare_data_plane_task, seal_data_plane_task
from game_l10n.models import Segment
from game_l10n.native_pass import import_native_wave, native_control_step, prepare_native_tasks, seal_native_wave_relay
from game_l10n.native_runtime import (
    bind_data_plane_launch, configure_native_runtime, native_runtime, record_native_launch,
)
from game_l10n.native_translation_retry import start_native_translation_retries, advance_native_translation_retries


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.segments = [Segment("a", "SOURCE A"), Segment("b", "SOURCE B")]
        self.source = self.root / "source.jsonl"
        self.write_rows(self.source, [asdict(row) for row in self.segments])
        self.tasks, self.results = self.root / "tasks", self.root / "results"

    @staticmethod
    def write_rows(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

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

    def test_host_specific_exact_models_and_explicit_override(self) -> None:
        configure_native_runtime(self.root, "codex", {})
        self.assertEqual(native_runtime(self.root)["models"]["native-luna"], "gpt-6-luna")
        configure_native_runtime(self.root, "omp", {"native-sol": "fixture/selected-sol"})
        self.assertEqual(native_runtime(self.root)["models"], {
            "native-luna": "openai-codex/gpt-6-luna", "native-sol": "fixture/selected-sol",
        })
        for value in ("default", "inherit", "@task", "fixture/*", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "E_NATIVE_RUNTIME_INVALID"):
                configure_native_runtime(self.root, "omp", {"native-luna": value})

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
        self.assertEqual(self.step()["action"], "repair_native_runtime")
        record_native_launch(self.root, relay, event="capability-available")
        resumed = self.step(resume_relay=True)
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(resumed["wave"], 1)
        self.assertEqual(resumed["route_binding"]["attempt"], 1)

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
