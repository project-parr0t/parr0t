from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from game_l10n.formats import read_segments
from game_l10n.models import Segment
from game_l10n.native_pass import (
    _imported_native_waves,
    _validate_manifest_rows,
    import_native_shard,
    import_native_wave,
    merge_native_pass,
    native_control_step,
    prepare_native_tasks,
    seal_native_wave_relay,
)
from tests.test_validators import optional_slot_fixture


class NativePassTests(unittest.TestCase):
    def test_review_pass_uses_large_batch_and_bounded_failure_split_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            summary = prepare_native_tasks(
                root,
                source,
                [Segment(f"fixture-{index}", "S") for index in range(4096)],
                tasks,
                pass_kind="review",
                max_material_tokens=1_000_000,
            )
            manifest = json.loads((tasks / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(summary["pass_kind"], "review")
        self.assertEqual(summary["max_records"], 2048)
        self.assertEqual([item["records"] for item in manifest["tasks"]], [2048, 2048])
        split = manifest["worker_contract"]["failure_split"]
        self.assertTrue(split["enabled"])
        self.assertEqual(split["strategy"], "failed_subset_binary")
        self.assertTrue(split["successful_rows_locked"])
        self.assertEqual(split["max_depth"], 11)
        self.assertEqual(split["max_semantic_calls"], 16)
        self.assertEqual(
            manifest["worker_contract"]["max_semantic_model_calls_per_task"], 16
        )
        self.assertEqual(manifest["worker_contract"]["max_model_calls_per_task"], 24)
        self.assertFalse(manifest["worker_contract"]["per_record_semantic_call_loop_allowed"])

    def test_review_worker_proves_bounded_failed_subset_split_and_merges_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            results = root / "results"
            segments = [Segment(f"fixture-{index}", "S") for index in range(4)]
            prepare_native_tasks(
                root,
                source,
                segments,
                tasks,
                pass_kind="review",
                max_records=4,
                max_workers=1,
            )
            first = native_control_step(root, tasks / "manifest.json", results)
            self.assertEqual(first["action"], "dispatch_isolated_wave_relay")
            task_rows = [
                json.loads(line)
                for line in (tasks / "task-0001.jsonl").read_text().splitlines()
            ]
            self._write_rows(
                results / "task-0001.jsonl",
                [
                    {
                        "target_id": row["target_id"],
                        "status": "success" if index < 2 else "refused",
                        "text": "T" if index < 2 else "",
                        **(
                            {"split_depth": 1, "semantic_attempts": 2}
                            if index >= 2
                            else {}
                        ),
                    }
                    for index, row in enumerate(task_rows)
                ],
            )
            completed = native_control_step(root, tasks / "manifest.json", results)

            merged = merge_native_pass(
                root,
                segments,
                root / "merged.jsonl",
                manifest_path=tasks / "manifest.json",
            )

        self.assertEqual(completed["action"], "aggregate_completed_pass")
        self.assertEqual(completed["imported_wave"]["success"], 2)
        self.assertEqual(completed["imported_wave"]["failed"], 2)
        self.assertEqual(merged["success"], 2)
        self.assertEqual(merged["failed"], 2)
        self.assertEqual(merged["imported_shards"], 1)

    def test_native_step_hashes_only_the_selected_wave_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            prepare_native_tasks(
                root,
                source,
                [Segment(f"fixture-{index}", "S") for index in range(20)],
                tasks,
                max_records=1,
                max_workers=1,
            )
            from game_l10n import native_pass as native_module

            with patch.object(
                native_module,
                "_file_sha256",
                wraps=native_module._file_sha256,
            ) as digest:
                step = native_control_step(root, tasks / "manifest.json", root / "results")

        self.assertEqual(step["action"], "dispatch_isolated_wave_relay")
        self.assertEqual(digest.call_count, 1)


    def _prepare_optional_wave(self, root: Path, *, slots: int = 1):
        source = root / "source.jsonl"
        self._write_rows(source, [
            asdict(Segment("fixture-label", "SYNTHETIC_LABEL")),
            *(asdict(optional_slot_fixture(target_id=f"fixture-slot-{index}"))
              for index in range(slots)),
        ])
        segments = read_segments(source)
        tasks = root / "tasks"
        prepare_native_tasks(
            root, source, segments, tasks, max_records=1, max_workers=slots + 1
        )
        results = root / "results"
        results.mkdir()
        for task in tasks.glob("task-*.jsonl"):
            rows = [json.loads(line) for line in task.read_text().splitlines()]
            self._write_rows(results / task.name, [{
                "target_id": row["target_id"], "status": "success",
                "text": "TARGET_LABEL" if row["target_id"] == "fixture-label" else "",
            } for row in rows])
        return source, segments, tasks, results

    def test_unbound_manual_shard_merge_rejects_empty_even_with_slot_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shard = root / ".game-l10n/native/shards/manual.jsonl"
            shard.parent.mkdir(parents=True)
            segment = optional_slot_fixture()
            self._write_rows(shard, [{
                "target_id": segment.id, "status": "success", "text": "",
            }])
            output = root / "merged.jsonl"
            with self.assertRaisesRegex(ValueError, "without text"):
                merge_native_pass(root, [segment], output)
            self.assertFalse(output.exists())

    def test_optional_wave_rejects_malformed_source_paths_with_fixed_error(self) -> None:
        missing = object()
        for value in (missing, None, 1, [], {}, "", ".", "missing.jsonl", "../outside.jsonl", "/outside.jsonl", "bad\0path"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, _, tasks, results = self._prepare_optional_wave(root)
                manifest_path = tasks / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                if value is missing:
                    del manifest["source_artifact"]
                else:
                    manifest["source_artifact"] = value
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(ValueError, "^E_NATIVE_WAVE_RESULT_INVALID:"):
                    import_native_wave(root, manifest_path, results, 1)
                self.assertFalse((root / ".game-l10n/native/shards").exists())

    def test_multiple_empty_tasks_read_bound_source_once_per_wave_and_legacy_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _, tasks, results = self._prepare_optional_wave(root, slots=2)
            manifest_path = tasks / "manifest.json"
            with patch("game_l10n.formats.read_segments", wraps=read_segments) as reader:
                summary = import_native_wave(root, manifest_path, results, 1)
                reader.assert_called_once_with(source.resolve())
            self.assertEqual(summary["tasks"], 3)
            self.assertEqual(summary["success"], 3)
            # Simulate only the missing legacy receipt; retain imported shards.
            (root / summary["receipt"]).unlink()
            manifest = json.loads(manifest_path.read_text())
            with patch("game_l10n.formats.read_segments", wraps=read_segments) as reader:
                imported, count = _imported_native_waves(root, manifest_path, manifest)
                reader.assert_called_once_with(source.resolve())
            self.assertEqual(imported, [1])
            self.assertEqual(count, 3)
            self.assertTrue((root / summary["receipt"]).exists())

    def test_optional_wave_rejects_related_ids_absent_from_bound_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, segments, _, results = self._prepare_optional_wave(root)
            slot = segments[1]
            source.write_text(json.dumps(asdict(slot)) + "\n")
            tasks = root / "unbound-related-tasks"
            prepare_native_tasks(root, source, [slot], tasks)
            self._write_rows(results / "task-0001.jsonl", [{
                "target_id": slot.id, "status": "success", "text": "",
            }])
            manifest = json.loads((tasks / "manifest.json").read_text())
            with self.assertRaisesRegex(ValueError, "references an unbound target"):
                _validate_manifest_rows(root, manifest, [{
                    "target_id": slot.id, "status": "success", "text": "",
                }], bound_segments={})
            with self.assertRaisesRegex(ValueError, "^E_NATIVE_WAVE_RESULT_INVALID:"):
                import_native_wave(root, tasks / "manifest.json", results, 1)
            self.assertFalse((root / ".game-l10n/native/shards").exists())

    def test_result_owned_empty_authority_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = root / "result.jsonl"
            self._write_rows(result, [{
                "target_id": "fixture-slot", "status": "success", "text": "",
                "allow_empty": True,
                "ui_empty_target": optional_slot_fixture().metadata["ui_empty_target"],
            }])
            with self.assertRaisesRegex(ValueError, "without text"):
                import_native_shard(root, result, "fixture")

    def test_optional_empty_wave_joins_bound_source_and_merges_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, segments, tasks, results = self._prepare_optional_wave(root)
            summary = import_native_wave(root, tasks / "manifest.json", results, 1)
            self.assertEqual(summary["success"], 2)
            output = root / "merged.jsonl"
            merge_native_pass(root, segments, output, manifest_path=tasks / "manifest.json")
            row = json.loads(output.read_text().splitlines()[1])
            self.assertEqual(row["text"], "")
            self.assertEqual(row["candidate_generation"], 1)
            self.assertTrue(row["candidate_digest"])
            # Import and merge retain candidate state; neither claims review.
            self.assertNotIn("reviewer", row)
            self.assertNotEqual(row["status"], "reviewed")

    def test_optional_empty_wave_rejects_changed_inventory_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, segments, tasks, results = self._prepare_optional_wave(root)
            changed = asdict(segments[1])
            changed["source"] = "SOURCE_CHANGED"
            self._write_rows(source, [asdict(segments[0]), changed])
            with self.assertRaisesRegex(ValueError, "invalid"):
                import_native_wave(root, tasks / "manifest.json", results, 1)
            self.assertFalse((root / ".game-l10n/native/shards").exists())

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
        self.assertEqual(first["task_payload_schema"], "parr0t.native-task-payload.v1")
        self.assertEqual([row["target_id"] for row in task_rows], [f"fixture-{index}" for index in range(5)])
        self.assertEqual([row["ordinal"] for row in task_rows], list(range(5)))
        self.assertEqual(
            [row["segment"]["source"] for row in task_rows],
            [f"SOURCE_{index}" for index in range(5)],
        )
        self.assertEqual(
            [row["segment"]["id"] for row in task_rows],
            [row["target_id"] for row in task_rows],
        )

    def test_legacy_v5_id_only_manifest_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture-source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            segments = [Segment("fixture-1", "SOURCE")]
            tasks = root / "tasks"
            prepare_native_tasks(root, source, segments, tasks)
            manifest_path = tasks / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = 5
            task_path = tasks / manifest["tasks"][0]["file"]
            rows = [
                {
                    "target_id": row["target_id"],
                    "ordinal": row["ordinal"],
                }
                for row in (
                    json.loads(line)
                    for line in task_path.read_text(encoding="utf-8").splitlines()
                )
            ]
            self._write_rows(task_path, rows)
            manifest["tasks"][0]["sha256"] = hashlib.sha256(
                task_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            summary = merge_native_pass(
                root,
                segments,
                root / "merged.jsonl",
                manifest_path=manifest_path,
            )

        self.assertFalse(summary["complete"])

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
        self.assertEqual(manifest["version"], 6)
        self.assertEqual(
            manifest["task_payload_schema"],
            "parr0t.native-task-payload.v1",
        )
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
        self.assertTrue(
            manifest["worker_contract"]["task_payload_contains_bound_segments"]
        )
        self.assertEqual(
            manifest["worker_contract"]["full_source_artifact_access"],
            "validation_only",
        )
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
        self.assertEqual(manifest["execution_contract"]["max_agent_depth"], 2)
        self.assertFalse(
            manifest["execution_contract"]["root_direct_semantic_worker_allowed"]
        )
        self.assertEqual(
            manifest["execution_contract"]["root_dispatch_role"],
            "isolated_wave_relay",
        )
        self.assertTrue(
            manifest["execution_contract"]["relay_delegation_allowed"]
        )
        self.assertFalse(
            manifest["execution_contract"]["relay_model_controller_allowed"]
        )
        self.assertEqual(
            manifest["execution_contract"]["orchestration_owner"],
            "deterministic_core_via_isolated_wave_relay",
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

    def test_changed_v6_detail_contract_is_rejected_before_import(self) -> None:
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
            self.assertEqual(first["action"], "dispatch_isolated_wave_relay")
            self.assertEqual(first["wave"], 1)
            self.assertNotIn("tasks", first)
            self.assertEqual(first["relay"]["role"], "isolated_wave_relay")
            self.assertEqual(first["relay"]["tasks"], 1)
            relay_path = root / first["relay"]["dispatch"]
            relay = json.loads(relay_path.read_text(encoding="utf-8"))
            self.assertEqual(relay["schema"], "parr0t.native-wave-relay.v1")
            self.assertEqual(len(relay["task_bindings"]), 1)
            self.assertFalse(
                relay["relay_contract"]["raw_child_response_forwarding_allowed"]
            )

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
                    self.assertEqual(step["action"], "dispatch_isolated_wave_relay")
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

    def test_native_wave_relay_receipt_is_categorical_and_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            results = root / "results"
            prepare_native_tasks(
                root, source, [Segment("fixture-1", "SOURCE")], tasks
            )
            step = native_control_step(root, tasks / "manifest.json", results)
            dispatch = root / step["relay"]["dispatch"]
            incomplete = seal_native_wave_relay(root, dispatch)
            task_row = json.loads(
                (tasks / "task-0001.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            self._write_rows(
                results / "task-0001.jsonl",
                [
                    {
                        "target_id": task_row["target_id"],
                        "status": "success",
                        "text": "TARGET",
                    }
                ],
            )
            complete = seal_native_wave_relay(root, dispatch)
            receipt = json.loads(
                (root / complete["receipt"]).read_text(encoding="utf-8")
            )

        self.assertEqual(incomplete["issue_codes"], ["E_NATIVE_WAVE_INCOMPLETE"])
        self.assertEqual(complete["issue_codes"], [])
        self.assertEqual(complete["counts"]["ready_tasks"], 1)
        self.assertEqual(
            receipt["schema"], "parr0t.native-wave-relay-receipt.v1"
        )
        self.assertNotIn("SOURCE", json.dumps(receipt))
        self.assertNotIn("TARGET", json.dumps(receipt))

    def test_cli_mode_requires_relay_receipt_before_wave_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            source.write_text("fixture", encoding="utf-8")
            tasks = root / "tasks"
            results = root / "results"
            prepare_native_tasks(
                root, source, [Segment("fixture-1", "SOURCE")], tasks
            )
            first = native_control_step(
                root,
                tasks / "manifest.json",
                results,
                require_relay_receipt=True,
            )
            task_row = json.loads(
                (tasks / "task-0001.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            self._write_rows(
                results / "task-0001.jsonl",
                [
                    {
                        "target_id": task_row["target_id"],
                        "status": "success",
                        "text": "TARGET",
                    }
                ],
            )
            blocked = native_control_step(
                root,
                tasks / "manifest.json",
                results,
                require_relay_receipt=True,
            )
            seal_native_wave_relay(root, root / first["relay"]["dispatch"])
            resumed = native_control_step(
                root,
                tasks / "manifest.json",
                results,
                require_relay_receipt=True,
            )

        self.assertEqual(
            blocked["issue_codes"], ["E_NATIVE_RELAY_RECEIPT_REQUIRED"]
        )
        self.assertEqual(resumed["action"], "aggregate_completed_pass")

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

        self.assertEqual(first["action"], "dispatch_isolated_wave_relay")
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
        self.assertEqual(resumed["action"], "dispatch_isolated_wave_relay")
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
