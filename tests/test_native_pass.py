from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.models import Segment
from game_l10n.native_pass import import_native_shard, merge_native_pass, prepare_native_tasks


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
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["max_material_tokens"], 300)
        self.assertEqual(manifest["worker_contract"]["max_followup_tasks"], 0)
        self.assertEqual(manifest["worker_contract"]["parent_result"], "metadata_only")
        self.assertTrue(
            all(task["records"] == 1 for task in manifest["tasks"])
        )


if __name__ == "__main__":
    unittest.main()
