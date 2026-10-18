from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.models import Segment
from game_l10n.native_pass import (
    import_native_shard,
    import_native_wave,
    merge_native_pass,
    native_control_step,
    prepare_native_tasks,
)


class NativePassTests(unittest.TestCase):
    def _write_rows(self, path: Path, rows: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_exclusive_shards_merge_in_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            second = root / "second.jsonl"
            first = root / "first.jsonl"
            self._write_rows(
                second,
                [{"target_id": "fixture-2", "status": "success", "text": "TARGET_2"}],
            )
            self._write_rows(
                first,
                [{"target_id": "fixture-1", "status": "refused", "text": ""}],
            )
            import_native_shard(root, second, "worker-2")
            import_native_shard(root, first, "worker-1")
            output = root / "merged.jsonl"
            summary = merge_native_pass(
                root,
                [Segment("fixture-1", "SOURCE_1"), Segment("fixture-2", "SOURCE_2")],
                output,
            )
            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(summary["complete"])
        self.assertEqual([row["target_id"] for row in rows], ["fixture-1", "fixture-2"])
        self.assertEqual(rows[0]["status"], "refused")
        self.assertEqual(rows[1]["candidate_generation"], 1)
        self.assertTrue(rows[1]["candidate_digest"])

    def test_overlapping_shards_fail_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in (("one", "TARGET_A"), ("two", "TARGET_B")):
                source = root / f"{name}.jsonl"
                self._write_rows(
                    source,
                    [{"target_id": "fixture-1", "status": "success", "text": text}],
                )
                import_native_shard(root, source, name)
            with self.assertRaisesRegex(ValueError, "overlap"):
                merge_native_pass(
                    root,
                    [Segment("fixture-1", "SOURCE")],
                    root / "merged.jsonl",
                )

    def test_wave_import_validates_all_results_then_manifest_merge_ignores_stale_shards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            segments = [
                Segment(f"fixture-{index}", f"SOURCE_{index}") for index in range(4)
            ]
            tasks = root / "tasks"
            prepare_native_tasks(
                root,
                source,
                segments,
                tasks,
                max_records=2,
                max_workers=2,
            )
            results = root / "results"
            results.mkdir()
            for number in (1, 2):
                task_path = tasks / f"task-{number:04d}.jsonl"
                task_rows = [
                    json.loads(line)
                    for line in task_path.read_text(encoding="utf-8").splitlines()
                ]
                self._write_rows(
                    results / task_path.name,
                    [
                        {
                            "target_id": row["target_id"],
                            "status": "success",
                            "text": f"TARGET_{row['ordinal']}",
                        }
                        for row in task_rows
                    ],
                )

            summary = import_native_wave(root, tasks / "manifest.json", results, 1)
            receipt = root / summary["receipt"]
            receipt_exists = receipt.is_file()
            stale = root / ".game-l10n" / "native" / "shards" / "stale.jsonl"
            self._write_rows(
                stale,
                [{"target_id": "not-in-source", "status": "success", "text": "STALE"}],
            )
            output = root / "merged.jsonl"
            merged = merge_native_pass(
                root,
                segments,
                output,
                manifest_path=tasks / "manifest.json",
            )
            rows = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(summary["tasks"], 2)
        self.assertEqual(summary["records"], 4)
        self.assertEqual(summary["success"], 4)
        self.assertEqual(summary["failed"], 0)
        self.assertTrue(receipt_exists)
        self.assertNotIn("fixture-", json.dumps(summary))
        self.assertNotIn("TARGET_", json.dumps(summary))
        self.assertTrue(merged["manifest_bound"])
        self.assertEqual(merged["expected_shards"], 2)
        self.assertEqual(merged["imported_shards"], 2)
        self.assertTrue(merged["complete"])
        self.assertEqual(
            [row["target_id"] for row in rows],
            [f"fixture-{index}" for index in range(4)],
        )

    def test_wave_import_is_atomic_when_any_result_breaks_its_task_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            segments = [Segment(f"fixture-{index}", "SOURCE") for index in range(4)]
            tasks = root / "tasks"
            prepare_native_tasks(
                root,
                source,
                segments,
                tasks,
                max_records=2,
                max_workers=2,
            )
            results = root / "results"
            results.mkdir()
            first_rows = [
                json.loads(line)
                for line in (tasks / "task-0001.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self._write_rows(
                results / "task-0001.jsonl",
                [
                    {
                        "target_id": row["target_id"],
                        "status": "success",
                        "text": "TARGET",
                    }
                    for row in first_rows
                ],
            )
            self._write_rows(
                results / "task-0002.jsonl",
                [
                    {
                        "target_id": "wrong-binding",
                        "status": "success",
                        "text": "TARGET",
                    }
                ],
            )

            with self.assertRaisesRegex(ValueError, "exclusive task binding"):
                import_native_wave(root, tasks / "manifest.json", results, 1)
            shard_root = root / ".game-l10n" / "native" / "shards"
            imported = list(shard_root.glob("*.jsonl")) if shard_root.exists() else []

        self.assertEqual(imported, [])

    def test_native_tasks_are_exclusive_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            segments = [Segment(f"fixture-{index}", f"SOURCE_{index}") for index in range(5)]
            output = root / "fixture-tasks"
            first = prepare_native_tasks(root, source, segments, output, max_records=2)
            second = prepare_native_tasks(root, source, segments, output, max_records=2)
            task_rows = [
                json.loads(line)
                for task in sorted(output.glob("task-*.jsonl"))
                for line in task.read_text(encoding="utf-8").splitlines()
            ]

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["tasks"], 3)
        self.assertEqual([row["target_id"] for row in task_rows], [f"fixture-{index}" for index in range(5)])
        self.assertEqual([row["ordinal"] for row in task_rows], list(range(5)))

    def test_native_tasks_bound_fresh_workers_by_records_and_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            segments = [
                Segment(f"fixture-{index}", "S" * 700)
                for index in range(3)
            ]
            output = root / "fixture-tasks"
            summary = prepare_native_tasks(
                root,
                source,
                segments,
                output,
                max_records=32,
                max_material_tokens=300,
            )
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["tasks"], 3)
        self.assertTrue(summary["fresh_worker_per_task"])
        self.assertEqual(summary["history_mode"], "none")
        self.assertEqual(manifest["version"], 5)
        self.assertEqual(manifest["min_records"], 8)
        self.assertEqual(manifest["max_material_tokens"], 300)
        self.assertEqual(manifest["max_workers"], 8)
        self.assertEqual(
            [(task["wave"], task["slot"]) for task in manifest["tasks"]],
            [(1, 1), (1, 2), (1, 3)],
        )
        self.assertEqual(manifest["worker_contract"]["max_followup_tasks"], 0)
        self.assertEqual(manifest["worker_contract"]["max_agent_depth"], 1)
        self.assertFalse(manifest["worker_contract"]["delegation_allowed"])
        self.assertFalse(
            manifest["worker_contract"]["model_controller_role_allowed"]
        )
        self.assertEqual(
            manifest["worker_contract"]["max_model_calls_per_task"], 16
        )
        self.assertEqual(
            manifest["worker_contract"]["max_semantic_model_calls_per_task"], 1
        )
        self.assertFalse(
            manifest["worker_contract"]["per_record_semantic_call_loop_allowed"]
        )
        self.assertTrue(
            manifest["worker_contract"]["single_batched_result_required"]
        )
        self.assertEqual(
            manifest["worker_contract"]["max_parent_progress_messages"], 0
        )
        self.assertEqual(manifest["worker_contract"]["parent_result"], "metadata_only")
        self.assertEqual(
            manifest["worker_contract"]["control_envelope_schema"],
            "parr0t.control-envelope.v2",
        )
        self.assertEqual(
            manifest["worker_contract"]["worker_final_response"],
            "workspace_relative_envelope_path_only",
        )
        self.assertFalse(manifest["worker_contract"]["free_form_parent_response_allowed"])
        self.assertEqual(
            manifest["detail_contract"]["schema"],
            "parr0t.detail-contract.v1",
        )
        self.assertEqual(
            manifest["detail_contract"]["authority"], "supplemental_only"
        )
        self.assertFalse(
            manifest["detail_contract"]["free_form_execution_contract_allowed"]
        )
        self.assertTrue(manifest["contract_fingerprint"])
        self.assertEqual(
            manifest["execution_contract"]["strategy"],
            "bounded_slot_queue",
        )
        self.assertFalse(
            manifest["execution_contract"]["job_local_controller_allowed"]
        )
        self.assertFalse(
            manifest["execution_contract"]["model_controller_allowed"]
        )
        self.assertFalse(
            manifest["execution_contract"]["worker_delegation_allowed"]
        )
        self.assertEqual(manifest["execution_contract"]["max_agent_depth"], 1)
        self.assertEqual(
            manifest["execution_contract"]["orchestration_owner"],
            "root_control_plane",
        )
        self.assertEqual(
            manifest["execution_contract"]["max_root_model_calls_per_wave"],
            12,
        )
        self.assertEqual(
            manifest["execution_contract"]["max_root_model_calls_per_turn"],
            64,
        )
        self.assertEqual(
            manifest["execution_contract"]["max_root_input_tokens_per_turn"],
            5_000_000,
        )
        self.assertEqual(
            manifest["execution_contract"]["budget_exhaustion_action"],
            "checkpoint_and_return_control",
        )
        self.assertFalse(
            manifest["execution_contract"]["automatic_root_turn_restart_allowed"]
        )
        self.assertEqual(
            manifest["execution_contract"]["max_unchanged_progress_observations"],
            1,
        )
        self.assertEqual(
            manifest["execution_contract"]["max_control_actions_without_progress"],
            2,
        )
        self.assertEqual(
            manifest["execution_contract"]["progress_reporting"],
            "wave_boundary_or_blocker_only",
        )
        self.assertTrue(
            all(task["records"] == 1 for task in manifest["tasks"])
        )

    def test_bulk_pass_stops_after_pilot_until_user_budget_is_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            segments = [Segment(f"fixture-{index}", "SOURCE") for index in range(4)]
            tasks = root / "tasks"
            prepare_native_tasks(
                root,
                source,
                segments,
                tasks,
                max_records=2,
                max_workers=1,
                bulk_budget_gate_records=4,
            )
            results = root / "results"
            results.mkdir()
            second_rows = [
                json.loads(line)
                for line in (tasks / "task-0002.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self._write_rows(
                results / "task-0002.jsonl",
                [
                    {
                        "target_id": row["target_id"],
                        "status": "success",
                        "text": "TARGET",
                    }
                    for row in second_rows
                ],
            )

            with self.assertRaisesRegex(ValueError, "E_BULK_EXECUTION_BUDGET_REQUIRED"):
                import_native_wave(root, tasks / "manifest.json", results, 2)

            prepare_native_tasks(
                root,
                source,
                segments,
                tasks,
                max_records=2,
                max_workers=1,
                bulk_budget_gate_records=4,
                max_elapsed_minutes=60,
                max_input_tokens=1_000_000,
                provider_cost_unmetered=True,
                user_approved_budget=True,
            )
            summary = import_native_wave(root, tasks / "manifest.json", results, 2)

        self.assertEqual(summary["wave"], 2)
        self.assertEqual(summary["records"], 2)

    def test_changed_v5_detail_contract_is_rejected_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            prepare_native_tasks(
                root,
                source,
                [Segment("fixture-1", "SOURCE")],
                tasks,
            )
            manifest_path = tasks / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["detail_contract"]["per_record_semantic_call_loop_allowed"] = True
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            results = root / "results"
            results.mkdir()

            with self.assertRaisesRegex(ValueError, "E_DETAIL_CONTRACT_CONFLICT"):
                import_native_wave(root, manifest_path, results, 1)

    def test_detail_policy_may_tighten_but_not_disable_bulk_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source.jsonl"
            source.write_text("fixture", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cannot exceed the core default"):
                prepare_native_tasks(
                    root,
                    source,
                    [Segment("fixture-1", "SOURCE")],
                    root / "tasks",
                    bulk_budget_gate_records=4097,
                )

    def test_small_residual_pass_uses_one_direct_task_without_controller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            output = root / "fixture-tasks"
            summary = prepare_native_tasks(
                root,
                source,
                [Segment(f"fixture-{index}", f"SOURCE_{index}") for index in range(4)],
                output,
                max_records=32,
            )

        contract = summary["execution_contract"]
        self.assertEqual(summary["tasks"], 1)
        self.assertEqual(contract["strategy"], "direct_single_task")
        self.assertFalse(contract["job_local_controller_allowed"])
        self.assertFalse(contract["task_replanning_allowed"])
        self.assertFalse(contract["new_proof_layers_allowed"])
        self.assertEqual(
            contract["capacity_failure_action"],
            "defer_task_and_end_worker_attempt",
        )

    def test_small_tail_is_rebalanced_instead_of_creating_one_record_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            output = root / "fixture-tasks"
            summary = prepare_native_tasks(
                root,
                source,
                [Segment(f"fixture-{index}", "S") for index in range(65)],
                output,
                max_records=64,
                min_records=8,
                max_material_tokens=1_000_000,
            )
            manifest = json.loads(
                (output / "manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(summary["tasks"], 2)
        self.assertEqual(
            [task["records"] for task in manifest["tasks"]],
            [32, 33],
        )

    def test_native_control_step_dispatches_imports_and_advances_one_wave(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            segments = [Segment(f"fixture-{index}", "SOURCE") for index in range(4)]
            tasks = root / "tasks"
            results = root / "results"
            prepare_native_tasks(
                root,
                source,
                segments,
                tasks,
                max_records=2,
                max_workers=1,
            )

            first = native_control_step(root, tasks / "manifest.json", results)
            self.assertEqual(first["action"], "dispatch_authorized_wave")
            self.assertEqual(first["wave"], 1)
            self.assertEqual(len(first["tasks"]), 1)

            for number in (1, 2):
                task_path = tasks / f"task-{number:04d}.jsonl"
                rows = [
                    json.loads(line)
                    for line in task_path.read_text(encoding="utf-8").splitlines()
                ]
                self._write_rows(
                    results / task_path.name,
                    [
                        {
                            "target_id": row["target_id"],
                            "status": "success",
                            "text": "TARGET",
                        }
                        for row in rows
                    ],
                )
                step = native_control_step(
                    root,
                    tasks / "manifest.json",
                    results,
                    usage_event_id=f"wave-{number}",
                    input_tokens=100,
                )
                if number == 1:
                    self.assertEqual(step["action"], "dispatch_authorized_wave")
                    self.assertEqual(step["wave"], 2)
                    self.assertEqual(step["imported_wave"]["wave"], 1)
                else:
                    self.assertEqual(step["action"], "aggregate_completed_pass")
                    self.assertEqual(step["wave"], 0)
                    self.assertEqual(step["imported_wave"]["wave"], 2)

        self.assertEqual(step["counts"]["imported_waves"], 2)
        self.assertEqual(step["usage"]["events"], 2)
        self.assertEqual(step["usage"]["input_tokens"], 200)
        self.assertNotIn("SOURCE", json.dumps(step))
        self.assertNotIn("TARGET", json.dumps(step))

    def test_native_control_step_stops_repeated_unchanged_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            prepare_native_tasks(
                root,
                source,
                [Segment("fixture-1", "SOURCE")],
                tasks,
            )
            results = root / "results"

            first = native_control_step(root, tasks / "manifest.json", results)
            second = native_control_step(root, tasks / "manifest.json", results)

        self.assertEqual(first["action"], "dispatch_authorized_wave")
        self.assertEqual(second["action"], "checkpoint_and_stop")
        self.assertEqual(second["issue_codes"], ["E_NATIVE_NO_MEASURABLE_PROGRESS"])

    def test_native_control_step_normalizes_invalid_wave_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            results = root / "results"
            prepare_native_tasks(
                root,
                source,
                [Segment("fixture-1", "SOURCE")],
                tasks,
            )
            native_control_step(root, tasks / "manifest.json", results)
            self._write_rows(
                results / "task-0001.jsonl",
                [{"target_id": "wrong-target", "status": "success", "text": "TARGET"}],
            )
            stopped = native_control_step(root, tasks / "manifest.json", results)
            self._write_rows(
                results / "task-0001.jsonl",
                [{"target_id": "fixture-1", "status": "success", "text": "TARGET"}],
            )
            recovered = native_control_step(root, tasks / "manifest.json", results)

        self.assertEqual(stopped["action"], "checkpoint_and_stop")
        self.assertEqual(stopped["issue_codes"], ["E_NATIVE_WAVE_RESULT_INVALID"])
        self.assertNotIn("wrong-target", json.dumps(stopped))
        self.assertEqual(recovered["action"], "aggregate_completed_pass")

    def test_native_control_step_preserves_pilot_across_budget_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            segments = [Segment(f"fixture-{index}", "SOURCE") for index in range(4)]
            tasks = root / "tasks"
            results = root / "results"
            prepare_native_tasks(
                root,
                source,
                segments,
                tasks,
                max_records=2,
                max_workers=1,
                bulk_budget_gate_records=4,
            )
            native_control_step(root, tasks / "manifest.json", results)
            task_path = tasks / "task-0001.jsonl"
            rows = [
                json.loads(line)
                for line in task_path.read_text(encoding="utf-8").splitlines()
            ]
            self._write_rows(
                results / task_path.name,
                [
                    {
                        "target_id": row["target_id"],
                        "status": "success",
                        "text": "TARGET",
                    }
                    for row in rows
                ],
            )
            stopped = native_control_step(
                root,
                tasks / "manifest.json",
                results,
                usage_event_id="pilot",
                input_tokens=100,
            )
            prepare_native_tasks(
                root,
                source,
                segments,
                tasks,
                max_records=2,
                max_workers=1,
                bulk_budget_gate_records=4,
                max_elapsed_minutes=60,
                max_input_tokens=1_000,
                provider_cost_unmetered=True,
                user_approved_budget=True,
            )
            resumed = native_control_step(root, tasks / "manifest.json", results)

        self.assertEqual(stopped["action"], "checkpoint_and_stop")
        self.assertEqual(
            stopped["issue_codes"], ["E_BULK_EXECUTION_BUDGET_REQUIRED"]
        )
        self.assertEqual(resumed["action"], "dispatch_authorized_wave")
        self.assertEqual(resumed["wave"], 2)
        self.assertEqual(resumed["counts"]["imported_waves"], 1)
        self.assertEqual(resumed["usage"]["input_tokens"], 100)

    def test_native_control_step_imports_completed_wave_before_budget_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            results = root / "results"
            prepare_native_tasks(
                root,
                source,
                [Segment(f"fixture-{index}", "SOURCE") for index in range(4)],
                tasks,
                max_records=2,
                max_workers=1,
                bulk_budget_gate_records=4,
                max_elapsed_minutes=60,
                max_input_tokens=100,
                provider_cost_unmetered=True,
                user_approved_budget=True,
            )
            native_control_step(root, tasks / "manifest.json", results)
            task_path = tasks / "task-0001.jsonl"
            rows = [
                json.loads(line)
                for line in task_path.read_text(encoding="utf-8").splitlines()
            ]
            self._write_rows(
                results / task_path.name,
                [
                    {
                        "target_id": row["target_id"],
                        "status": "success",
                        "text": "TARGET",
                    }
                    for row in rows
                ],
            )
            stopped = native_control_step(
                root,
                tasks / "manifest.json",
                results,
                usage_event_id="wave-1",
                input_tokens=100,
            )

        self.assertEqual(stopped["action"], "checkpoint_and_stop")
        self.assertEqual(
            stopped["issue_codes"], ["E_NATIVE_INPUT_BUDGET_EXHAUSTED"]
        )
        self.assertEqual(stopped["counts"]["imported_waves"], 1)
        self.assertEqual(stopped["imported_wave"]["wave"], 1)


if __name__ == "__main__":
    unittest.main()
