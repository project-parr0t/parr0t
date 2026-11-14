"""Synthetic tests for the guarded, no-history OMP launcher."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from game_l10n import omp_session


class OmpSessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name) / "synthetic \ud55c\uae00 & $ workspace"
        self.workspace.mkdir()
        (self.workspace / "game-l10n.json").write_text("{}", encoding="utf-8")
        self.model = "provider/exact-model@upstream/route:high"
        self.which = patch.object(omp_session.shutil, "which", return_value="C:/synthetic tools/omp.exe")
        self.which.start()
        self.addCleanup(self.which.stop)

    def plan(self, mode="workspace"):
        return omp_session._plan_command(self.workspace, self.model, mode)

    def rotation(self):
        handoff = self.workspace / omp_session.CONTROL_HANDOFF_PATH
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text("{}", encoding="utf-8")
        return patch.object(omp_session, "control_plane_state", return_value={"status": "rotation_required"})

    def test_workspace_pending_and_ready_entry(self):
        self.plan()
        with patch.object(omp_session, "control_plane_entry_state", return_value={"status": "ready"}):
            self.plan()
        with self.assertRaisesRegex(ValueError, "ROTATION_REQUIRED"):
            self.plan("rotation")
        with self.rotation():
            with self.assertRaisesRegex(ValueError, "use --mode rotation"):
                self.plan()
            self.plan("rotation")

    def test_rotation_requires_canonical_handoff(self):
        with patch.object(omp_session, "control_plane_state", return_value={"status": "rotation_required"}):
            with self.assertRaisesRegex(ValueError, "HANDOFF_REQUIRED"):
                self.plan("rotation")

    def test_preview_has_no_side_effects_and_exact_argv(self):
        before = set(self.workspace.rglob("*"))
        output = io.StringIO()
        with patch.object(omp_session.subprocess, "Popen") as spawn, redirect_stdout(output):
            code = omp_session.main(["--workspace", str(self.workspace), "--model", self.model,
                                     "--mode", "workspace", "--print-command"])
        self.assertEqual(code, 0)
        spawn.assert_not_called()
        self.assertEqual(before, set(self.workspace.rglob("*")))
        command = json.loads(output.getvalue())
        self.assertEqual(command[:5], ["C:/synthetic tools/omp.exe", "--cwd", str(self.workspace.resolve()),
                                       "--model", self.model])
        self.assertEqual(command[7:9], ["--no-prewalk", "--no-title"])
        self.assertEqual(command[9], "--trusted-extension")
        self.assertEqual(command[11:13], ["--parr0t-workspace", str(self.workspace.resolve())])
        self.assertTrue(Path(command[10]).is_file())
        self.assertTrue(Path(command[6]).is_relative_to(self.workspace.resolve()))

    def test_model_and_guard_prerequisites(self):
        for model in ("model-only", "provider/model;echo", "provider/model@@route", "provider/model:high:extra"):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "EXACT_MODEL_REQUIRED"):
                omp_session._plan_command(self.workspace, model, "workspace")
        with patch.object(omp_session.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "EXECUTABLE_REQUIRED"):
                self.plan()
        with patch.object(omp_session, "__file__", str(self.workspace / "missing.py")):
            with self.assertRaisesRegex(ValueError, "GUARD_REQUIRED"):
                self.plan()

    def test_foreground_exit_and_environment(self):
        child = Mock(pid=123)
        child.wait.return_value = 23
        with patch.dict(os.environ, {"PARR0T_OMP_GUARD_SESSION": "old", "PARR0T_OMP_GUARD_WORKSPACE": "old"}), \
                patch.object(omp_session.subprocess, "Popen", return_value=child) as spawn, \
                redirect_stdout(io.StringIO()):
            code = omp_session.main(["--workspace", str(self.workspace), "--model", self.model,
                                     "--mode", "workspace", "--foreground"])
        self.assertEqual(code, 23)
        kwargs = spawn.call_args.kwargs
        self.assertEqual(kwargs["creationflags"], 0)
        self.assertNotIn("PARR0T_OMP_GUARD_SESSION", kwargs["env"])
        self.assertNotIn("PARR0T_OMP_GUARD_WORKSPACE", kwargs["env"])
        self.assertIn("PARR0T_OMP_CONTROL_DIRECTORY", kwargs["env"])
        child.wait.assert_called_once_with()

    def test_plans_are_unique_and_rotation_prompt_is_canonical(self):
        self.assertNotEqual(self.plan()[1], self.plan()[1])
        with self.rotation():
            command, _, _ = self.plan("rotation")
        self.assertIn(omp_session.CONTROL_HANDOFF_PATH, command[-1])
        self.assertIn("fresh-session-confirmed", command[-1])


if __name__ == "__main__":
    unittest.main()
