from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from game_l10n import native_pass
from game_l10n.control_plane import _reset_native_control_observations
from game_l10n.models import Segment
from game_l10n.native_resume import native_resume_status
from game_l10n.store import stable_hash
from tests.test_native_pass import review_segment


class NativeResumeTests(unittest.TestCase):
    def _write(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value), encoding="utf-8")

    def _prepare(self, root: Path, name: str = "job", *, version: int = 7,
                 review: bool = False, records: int = 4) -> tuple[Path, Path]:
        source = root / f"{name}-source.jsonl"
        source.write_text("PRIVATE_SOURCE_SENTINEL", encoding="utf-8")
        tasks, results = root / name / "tasks", root / name / "results"
        segments = [review_segment(f"id-{index}", "PRIVATE_SOURCE_SENTINEL", "PRIVATE_TARGET_SENTINEL")
                    if review else Segment(f"id-{index}", "PRIVATE_SOURCE_SENTINEL")
                    for index in range(records)]
        native_pass.prepare_native_tasks(
            root, source, segments, tasks, pass_kind="review" if review else "translation",
            max_records=records if review else 2, max_workers=1,
            route_binding={"provider": "native-sol" if review else "native-luna", "attempt": 1},
        )
        manifest_path = tasks / "manifest.json"
        if version == 6:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = 6
            kind = manifest["pass_kind"]
            manifest["worker_contract"] = native_pass._worker_contract(kind, manifest_version=6)
            manifest["detail_contract"] = native_pass._detail_contract(kind, manifest_version=6)
            manifest.pop("language_binding")
            contract = {key: manifest[key] for key in (
                "worker_contract", "detail_contract", "execution_contract", "result_contract",
                "route_binding", "execution_namespace",
            )}
            manifest["contract_fingerprint"] = stable_hash(contract)
            self._write(manifest_path, manifest)
        native_pass.native_control_step(root, manifest_path, results)
        return manifest_path, results

    def _finish_wave(self, root: Path, manifest_path: Path, results: Path,
                     *, refused: bool = False) -> dict:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        plan = native_pass._native_control_plan_fingerprint(manifest)
        state = native_pass._load_native_control_state(
            native_pass._native_control_state_path(root, manifest, plan), manifest, plan,
        )
        wave = state["active_wave"]
        results.mkdir(parents=True, exist_ok=True)
        for task in manifest["tasks"]:
            if task["wave"] != wave:
                continue
            rows = [json.loads(line) for line in (manifest_path.parent / task["file"]).read_text().splitlines()]
            (results / task["file"]).write_text("".join(json.dumps({
                "target_id": row["target_id"],
                "status": "refused" if refused and index >= 2 else "success",
                "text": "" if refused and index >= 2 else "PRIVATE_TARGET_SENTINEL",
                "provider": manifest["route_binding"]["provider"],
            }) + "\n" for index, row in enumerate(rows)), encoding="utf-8")
        return native_pass.native_control_step(root, manifest_path, results)

    def _read_only(self, root: Path, **kwargs) -> dict:
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        read_text = Path.read_text

        def metadata_only(path: Path, *args, **options):
            self.assertEqual(path.suffix, ".json", f"Private payload opened: {path.name}")
            return read_text(path, *args, **options)

        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "read_text", metadata_only))
            for method in ("write_text", "write_bytes", "mkdir", "replace", "rename", "unlink", "touch"):
                stack.enter_context(patch.object(Path, method, side_effect=AssertionError("Discovery wrote files")))
            stack.enter_context(patch.object(native_pass, "native_control_step", side_effect=AssertionError("Controller invoked")))
            stack.enter_context(patch.object(native_pass, "native_runtime", side_effect=AssertionError("Runtime inspected")))
            stack.enter_context(patch.object(native_pass, "_read_jsonl", side_effect=AssertionError("Payload read")))
            result = native_resume_status(root, **kwargs)
        after = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        serialized = json.dumps(result)
        for private in ("PRIVATE_SOURCE_SENTINEL", "PRIVATE_TARGET_SENTINEL", "task_bindings",
                        "source_fingerprint", "contract_fingerprint", "worker_contract", "launch_contract"):
            self.assertNotIn(private, serialized)
        return result

    def _resign(self, path: Path, mutate) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        payload["dispatch_fingerprint"] = stable_hash({
            key: value for key, value in payload.items() if key != "dispatch_fingerprint"
        })
        self._write(path, payload)

    def test_missing_does_not_create_default_or_scan_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unrelated.jsonl").write_text("PRIVATE_SOURCE_SENTINEL", encoding="utf-8")
            with patch.object(Path, "rglob", side_effect=AssertionError("Workspace scan")):
                result = native_resume_status(root)
            self.assertEqual(result, {
                "schema": "parr0t.native-resume-status.v1", "status": "missing",
                "selected": None, "candidates": [], "issue_codes": [],
            })
            self.assertEqual([path.name for path in root.iterdir()], ["unrelated.jsonl"])

    def test_legacy_partial_counts_survive_rotation_and_deduplicate_waves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, results = self._prepare(root, version=6, records=6)
            self._finish_wave(root, manifest, results)
            before = self._read_only(root)
            self.assertEqual(before["status"], "ready")
            self.assertEqual(len(before["candidates"]), 1)
            selected = before["selected"]
            self.assertTrue(selected["compatible"])
            self.assertEqual(selected["manifest_version"], 6)
            self.assertEqual(selected["counts"], {
                "total_tasks": 3, "imported_tasks": 1, "total_waves": 3, "imported_waves": 1,
            })
            self.assertEqual(selected["results_dir"], "job/results")
            bound = json.loads(manifest.read_text(encoding="utf-8"))
            plan = native_pass._native_control_plan_fingerprint(bound)
            state_path = native_pass._native_control_state_path(root, bound, plan)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema"] = native_pass.LEGACY_NATIVE_CONTROL_STATE_SCHEMA
            for key in ("budget_started_at_epoch", "checkpoint_initialized", "imported_wave_count", "imported_tasks"):
                state.pop(key)
            self._write(state_path, state)
            self.assertEqual(self._read_only(root)["selected"]["counts"], selected["counts"])
            _reset_native_control_observations(root)
            resumed = self._read_only(root)
            self.assertEqual(resumed["selected"]["counts"], selected["counts"])
            self.assertEqual(resumed["selected"]["last_action"], "")
            self.assertEqual(resumed["selected"]["step_command"], [
                "game-l10n", "native-pass-step", "--workspace", str(root.resolve()),
                "--manifest", "job/tasks/manifest.json", "--results-dir", "job/results",
            ])

    def test_ambiguity_filter_and_unfinished_preference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, first_results = self._prepare(root, "first", records=2)
            second, _ = self._prepare(root, "second", records=2)
            ambiguous = self._read_only(root)
            self.assertEqual(ambiguous["status"], "selection_required")
            self.assertIsNone(ambiguous["selected"])
            filtered = self._read_only(root, manifest_path=second)
            self.assertEqual(filtered["selected"]["manifest"], "second/tasks/manifest.json")
            self._finish_wave(root, first, first_results)
            selected = self._read_only(root)
            self.assertEqual(selected["status"], "ready")
            self.assertEqual(selected["selected"]["manifest"], "second/tasks/manifest.json")
            historical = self._read_only(root, manifest_path=Path("first/tasks/manifest.json"))
            self.assertEqual(historical["selected"]["state"], "complete")
            unknown = self._read_only(root, manifest_path=Path("unbound/manifest.json"))
            self.assertEqual(unknown["issue_codes"], ["E_NATIVE_RESUME_MANIFEST_NOT_FOUND"])

    def test_tampered_digest_and_exact_bindings_fail_categorically(self) -> None:
        mutations = {
            "digest": lambda value: value.update(dispatch_fingerprint="tampered"),
            "wave": lambda value: value.update(wave=99),
            "schema": lambda value: value.update(schema="PRIVATE_SOURCE_SENTINEL"),
            "task": lambda value: value["task_bindings"][0].update(task="job/tasks/task-9999.jsonl"),
            "result_name": lambda value: value["task_bindings"][0].update(result="job/results/wrong.jsonl"),
            "escape": lambda value: value["task_bindings"][0].update(result="../PRIVATE_SOURCE_SENTINEL.jsonl"),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._prepare(root)
                relay = next((root / ".game-l10n/native/relay").glob("*-wave-*.json"))
                if name == "digest":
                    payload = json.loads(relay.read_text())
                    mutation(payload)
                    self._write(relay, payload)
                else:
                    self._resign(relay, mutation)
                status = self._read_only(root)
                self.assertEqual(status["status"], "invalid")
                self.assertIsNone(status["selected"])
                self.assertEqual(status["candidates"], [])
                self.assertTrue(all(code.startswith("E_NATIVE_RESUME_") for code in status["issue_codes"]))

    def test_conflicting_results_parents_across_waves_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, results = self._prepare(root, records=6)
            self._finish_wave(root, manifest, results)
            relay = sorted((root / ".game-l10n/native/relay").glob("*-wave-*.json"))[-1]
            self._resign(relay, lambda value: value["task_bindings"][0].update(result="other/task-0002.jsonl"))
            self.assertEqual(self._read_only(root)["issue_codes"], ["E_NATIVE_RESUME_RESULTS_CONFLICT"])

    def test_historical_unbound_split_route_keeps_root_resume_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, results = self._prepare(root, review=True)
            prepare = native_pass.prepare_native_tasks

            def historical_prepare(*args, **kwargs):
                kwargs.pop("route_binding", None)
                return prepare(*args, **kwargs)

            with patch.object(native_pass, "prepare_native_tasks", side_effect=historical_prepare):
                self._finish_wave(root, manifest, results, refused=True)
            status = self._read_only(root)
            self.assertEqual(status["status"], "ready")
            self.assertEqual(status["selected"]["manifest"], "job/tasks/manifest.json")
            self.assertIsNone(status["selected"]["current_child"]["route_binding"])

    def test_split_candidate_uses_root_command_and_separate_child(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, results = self._prepare(root, review=True)
            self._finish_wave(root, manifest, results, refused=True)
            status = self._read_only(root)
            self.assertEqual(status["status"], "ready")
            self.assertEqual(len(status["candidates"]), 1)
            selected = status["selected"]
            self.assertEqual(selected["manifest"], "job/tasks/manifest.json")
            self.assertEqual(selected["results_dir"], "job/results")
            self.assertEqual(selected["state"], "unfinished")
            child = selected["current_child"]
            self.assertIsNotNone(child)
            self.assertNotEqual(child["manifest"], selected["manifest"])
            self.assertIn("review-splits/", child["manifest"])
            self.assertEqual(child["state"], "unfinished")
            self.assertEqual(child["route_binding"], selected["route_binding"])
            argv = selected["step_command"]
            self.assertEqual(argv[argv.index("--manifest") + 1], selected["manifest"])
            self.assertEqual(argv[argv.index("--results-dir") + 1], selected["results_dir"])
            filtered = self._read_only(root, manifest_path=root / child["manifest"])
            self.assertEqual(filtered["selected"]["step_command"], argv)

    def test_split_without_root_results_binding_never_guesses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, results = self._prepare(root, review=True)
            self._finish_wave(root, manifest, results, refused=True)
            for relay in (root / ".game-l10n/native/relay").glob("*-wave-*.json"):
                if json.loads(relay.read_text())["manifest"] == "job/tasks/manifest.json":
                    relay.unlink()
            self.assertEqual(self._read_only(root)["issue_codes"], ["E_NATIVE_RESUME_RESULTS_MISSING"])

    def test_malformed_non_utf8_and_unreadable_metadata_never_leak(self) -> None:
        for contents in (b"{PRIVATE_SOURCE_SENTINEL", b"\xffPRIVATE_SOURCE_SENTINEL"):
            with self.subTest(contents=contents), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._prepare(root)
                relay = next((root / ".game-l10n/native/relay").glob("*-wave-*.json"))
                relay.write_bytes(contents)
                self.assertEqual(self._read_only(root)["status"], "invalid")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(root)
            with patch.object(Path, "read_text", side_effect=PermissionError("PRIVATE_SOURCE_SENTINEL")):
                result = native_resume_status(root)
            self.assertEqual(result["issue_codes"], ["E_NATIVE_RESUME_METADATA_UNREADABLE"])
            self.assertNotIn("PRIVATE_SOURCE_SENTINEL", json.dumps(result))

    def test_receipts_and_launch_ledgers_are_not_discovery_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(root)
            relay = next((root / ".game-l10n/native/relay").glob("*-wave-*.json"))
            for suffix in ("-receipt.json", "-launch.json"):
                relay.with_name(relay.stem + suffix).write_bytes(b"\xffPRIVATE_SOURCE_SENTINEL")
            self.assertEqual(self._read_only(root)["status"], "ready")

    def test_old_relay_optional_fields_remain_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepare(root, version=6)
            relay = next((root / ".game-l10n/native/relay").glob("*-wave-*.json"))
            def historical(value):
                value.pop("dispatch_revision", None)
                value.pop("launch_contract", None)
            self._resign(relay, historical)
            self.assertEqual(self._read_only(root)["status"], "ready")

    def test_changed_manifest_and_source_escape_are_rejected(self) -> None:
        for escape in (False, True):
            with self.subTest(escape=escape), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path, _ = self._prepare(root)
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if escape:
                    manifest["source_artifact"] = "../PRIVATE_SOURCE_SENTINEL.jsonl"
                else:
                    manifest["contract_fingerprint"] = "PRIVATE_SOURCE_SENTINEL"
                self._write(path, manifest)
                self.assertEqual(self._read_only(root)["status"], "invalid")

    def test_orphan_split_child_never_receives_controller_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, results = self._prepare(root, review=True)
            self._finish_wave(root, manifest, results, refused=True)
            for chain in (root / ".game-l10n/native/review-splits").glob("*/chain.json"):
                chain.unlink()
            status = self._read_only(root)
            self.assertEqual(status["issue_codes"], ["E_NATIVE_RESUME_SPLIT_INVALID"])

    def test_symlink_binding_is_rejected_even_inside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, results = self._prepare(root)
            destination = root / "real-results"
            destination.mkdir()
            if results.exists():
                results.rmdir()
            try:
                results.symlink_to(destination, target_is_directory=True)
            except (OSError, NotImplementedError):
                if os.name != "nt":
                    raise
                subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/J", str(results), str(destination)],
                    check=True, capture_output=True,
                )
            status = native_resume_status(root)
            self.assertEqual(status["issue_codes"], ["E_NATIVE_RESUME_PATH_INVALID"])


if __name__ == "__main__":
    unittest.main()
