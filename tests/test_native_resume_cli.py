from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from game_l10n.cli import main
from game_l10n.control_plane import establish_control_plane_entry
from game_l10n.formats import read_segments
from game_l10n.native_pass import native_control_step, prepare_native_tasks
from game_l10n.native_runtime import configure_native_runtime
from game_l10n.workspace import initialize_workspace, record_sdk_selection
from tests.test_workspace import activate_workspace_adapter, write_verified_inventory


class NativeResumeCliTests(unittest.TestCase):
    def _workspace(self, root: Path) -> tuple[Path, Path]:
        sdk, game, work = root / "sdk", root / "game", root / "work"
        sdk.mkdir()
        game.mkdir()
        initialize_workspace(work, sdk, game_roots=[game], git_choice="disabled")
        establish_control_plane_entry(work)
        record_sdk_selection(work, "private-new", target_engine="fixture", target_version="1.0")
        activate_workspace_adapter(work)
        primary, inventory = write_verified_inventory(work)
        context = work / "context/context-pack.json"
        body = b"{}\n"
        context.write_bytes(body)
        (work / ".game-l10n/context-state.json").write_text(json.dumps({
            "status": "success",
            "source_inventory_fingerprint": inventory["primary_fingerprint"],
            "output_sha256": hashlib.sha256(body).hexdigest(),
            "output_size": len(body),
            "execution": {"status": "success", "total_parts": 1, "completed_parts": 1,
                          "parts": [{"status": "success"}]},
        }), encoding="utf-8")
        configure_native_runtime(work, "codex", {})
        return work, primary

    def _checkpoint(self, work: Path, source: Path, name: str) -> tuple[Path, Path, dict]:
        tasks = work / "private" / name
        results = work / "private" / (name + "-actual-results")
        prepare_native_tasks(work, source, read_segments(source), tasks,
                             route_binding={"provider": "native-luna", "attempt": 1})
        manifest = tasks / "manifest.json"
        step = native_control_step(work, manifest, results,
                                   usage_event_id=name, input_tokens=123)
        return manifest, results, step

    def _command(self, *args: str) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(list(args))
        return code, json.loads(output.getvalue())

    def test_status_returns_executable_existing_paths_without_modifying_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, source = self._workspace(Path(directory))
            manifest, results, first = self._checkpoint(work, source, "existing")
            before = {p.relative_to(work): p.read_bytes() for p in work.rglob("*") if p.is_file()}
            code, status = self._command("status", "--workspace", str(work))
            self.assertEqual(code, 0)
            self.assertEqual(status["next_actions"], ["resume_native_checkpoint"])
            selected = status["native_resume"]["selected"]
            expected = ["game-l10n", "native-pass-step", "--workspace", str(work.resolve()),
                        "--manifest", manifest.relative_to(work).as_posix(),
                        "--results-dir", results.relative_to(work).as_posix()]
            self.assertEqual(selected["step_command"], expected)
            query_code, query = self._command("native-resume-status", "--workspace", str(work))
            self.assertEqual(query_code, 0)
            self.assertEqual(query["selected"]["step_command"], expected)
            after = {p.relative_to(work): p.read_bytes() for p in work.rglob("*") if p.is_file()}
            self.assertEqual(after, before)
            state_path = work / first["state"]
            old_state = json.loads(state_path.read_text(encoding="utf-8"))
            step_code, resumed = self._command(*selected["step_command"][1:])
            self.assertEqual(step_code, 0)
            self.assertEqual(resumed["action"], "wait_for_active_wave")
            self.assertEqual(resumed["state"], first["state"])
            new_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(new_state["input_tokens"], old_state["input_tokens"])
            self.assertEqual(new_state["usage_event_ids"], old_state["usage_event_ids"])

    def test_ambiguous_pending_checkpoints_require_explicit_manifest_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, source = self._workspace(Path(directory))
            manifest, _, _ = self._checkpoint(work, source, "first")
            self._checkpoint(work, source, "second")
            code, status = self._command("status", "--workspace", str(work))
            self.assertEqual(code, 0)
            self.assertEqual(status["next_actions"], ["select_native_checkpoint"])
            self.assertIsNone(status["native_resume"]["selected"])
            code, selected = self._command("status", "--workspace", str(work),
                                           "--native-manifest", manifest.relative_to(work).as_posix())
            self.assertEqual(code, 0)
            self.assertEqual(selected["next_actions"], ["resume_native_checkpoint"])
            self.assertEqual(selected["native_resume"]["selected"]["manifest"],
                             manifest.relative_to(work).as_posix())


if __name__ == "__main__":
    unittest.main()
