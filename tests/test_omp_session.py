"""Synthetic consumer tests for prompt-based and guarded, no-history OMP launches."""
from contextlib import redirect_stderr, redirect_stdout
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
        self.windows_os = Mock(wraps=os)
        self.windows_os.name = "nt"
        self.windows_os.environ = os.environ

    def plan(self, mode="workspace", *, guarded=False):
        return omp_session._plan_command(self.workspace, self.model, mode, guarded=guarded)

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
        self.assertEqual(len(command), 10)
        self.assertNotIn("--trusted-extension", command)
        self.assertNotIn("--parr0t-workspace", command)
        self.assertNotIn("--continue", command)
        self.assertNotIn("--resume", command)
        self.assertNotIn("parr0t_exec", command[-1])
        self.assertTrue(Path(command[6]).is_relative_to(self.workspace.resolve()))

    def test_guarded_preview_preserves_strict_arguments(self):
        output = io.StringIO()
        with patch.object(omp_session.subprocess, "Popen") as spawn, redirect_stdout(output):
            code = omp_session.main(["--workspace", str(self.workspace), "--model", self.model,
                                     "--mode", "workspace", "--guarded", "--print-command"])
        self.assertEqual(code, 0)
        spawn.assert_not_called()
        command = json.loads(output.getvalue())
        self.assertEqual(command[9], "--trusted-extension")
        self.assertTrue(Path(command[10]).is_file())
        self.assertEqual(command[11:13], ["--parr0t-workspace", str(self.workspace.resolve())])

    def test_model_and_guard_prerequisites(self):
        for model in ("model-only", "provider/model;echo", "provider/model@@route", "provider/model:high:extra"):
            with self.subTest(model=model), self.assertRaisesRegex(ValueError, "EXACT_MODEL_REQUIRED"):
                omp_session._plan_command(self.workspace, model, "workspace")
        with patch.object(omp_session.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "EXECUTABLE_REQUIRED"):
                self.plan()
        with patch.object(omp_session, "__file__", str(self.workspace / "missing.py")):
            with self.assertRaisesRegex(ValueError, "GUARD_REQUIRED"):
                self.plan(guarded=True)

    def test_guarded_cli_rejects_missing_guard_before_spawning(self):
        errors = io.StringIO()
        with patch.object(omp_session, "__file__", str(self.workspace / "missing.py")), \
                patch.object(omp_session.subprocess, "Popen") as spawn, redirect_stderr(errors):
            code = omp_session.main(["--workspace", str(self.workspace), "--model", self.model,
                                     "--mode", "workspace", "--guarded", "--foreground"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(errors.getvalue())["issue_code"], "E_OMP_CONTROL_GUARD_REQUIRED")
        spawn.assert_not_called()

    def test_foreground_exit_and_environment(self):
        child = Mock(pid=123)
        child.wait.return_value = 23
        for guarded in (False, True):
            with self.subTest(guarded=guarded), \
                    patch.dict(os.environ, {"PARR0T_OMP_GUARD_SESSION": "old",
                                            "PARR0T_OMP_GUARD_WORKSPACE": "old",
                                            "PARR0T_OMP_CONTROL_DIRECTORY": "old"}), \
                    patch.object(omp_session.subprocess, "Popen", return_value=child) as spawn, \
                    redirect_stdout(io.StringIO()):
                child.wait.reset_mock()
                code = omp_session.main(["--workspace", str(self.workspace), "--model", self.model,
                                         "--mode", "workspace", "--foreground"]
                                        + (["--guarded"] if guarded else []))
                self.assertEqual(code, 23)
                kwargs = spawn.call_args.kwargs
                self.assertEqual(kwargs["creationflags"], 0)
                self.assertNotIn("PARR0T_OMP_GUARD_SESSION", kwargs["env"])
                self.assertNotIn("PARR0T_OMP_GUARD_WORKSPACE", kwargs["env"])
                self.assertEqual("PARR0T_OMP_CONTROL_DIRECTORY" in kwargs["env"], guarded)
                child.wait.assert_called_once_with()
                child.poll.assert_not_called()

    def test_plans_are_unique_and_rotation_prompt_is_canonical(self):
        self.assertNotEqual(self.plan()[1], self.plan()[1])
        with self.rotation():
            command, _, _ = self.plan("rotation")
        self.assertIn(omp_session.CONTROL_HANDOFF_PATH, command[-1])
        self.assertIn("fresh-session-confirmed", command[-1])

    def test_prompt_launch_without_guard_returns_started_immediately(self):
        child = Mock(pid=123)
        output = io.StringIO()
        with patch.object(omp_session, "__file__", str(self.workspace / "missing.py")), \
                patch.object(omp_session, "os", self.windows_os), \
                patch.object(omp_session.subprocess, "CREATE_NEW_CONSOLE", 16, create=True), \
                patch.object(omp_session.subprocess, "Popen", return_value=child) as spawn, \
                patch.object(omp_session.time, "monotonic") as clock, \
                patch.object(omp_session.time, "sleep") as sleep, \
                patch.object(omp_session, "sha256") as digest, \
                redirect_stdout(output):
            code = omp_session.main(["--workspace", str(self.workspace), "--model", self.model,
                                     "--mode", "workspace"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["omp_isolation"], "prompt")
        self.assertEqual(result["launcher_process_id"], 123)
        self.assertIsNone(result["process_id"])
        self.assertNotIn("session_id", result)
        self.assertEqual(json.loads(Path(result["launch_record"]).read_text(encoding="utf-8"))["status"], "started")
        spawn.assert_called_once()
        command = spawn.call_args.args[0]
        self.assertNotIn("--trusted-extension", command)
        self.assertNotIn("--parr0t-workspace", command)
        self.assertEqual(command[4], self.model)
        self.assertEqual(spawn.call_args.kwargs["creationflags"], 16)
        self.assertNotIn("PARR0T_OMP_CONTROL_DIRECTORY", spawn.call_args.kwargs["env"])
        child.wait.assert_not_called()
        child.poll.assert_not_called()
        clock.assert_not_called()
        sleep.assert_not_called()
        digest.assert_not_called()

    def test_guarded_launch_requires_valid_registration_proof(self):
        child = Mock(pid=123)

        def registered(command, **kwargs):
            directory = Path(kwargs["env"]["PARR0T_OMP_CONTROL_DIRECTORY"])
            guard = Path(command[command.index("--trusted-extension") + 1])
            proof = {"schema": "parr0t.omp-control-ready.v1", "launch_id": directory.name,
                     "process_id": 456, "guard_sha256": omp_session.sha256(guard.read_bytes()).hexdigest(),
                     "session_id": "fresh-session:1"}
            (directory / "ready.json").write_text(json.dumps(proof), encoding="utf-8")
            return child

        output = io.StringIO()
        with patch.object(omp_session, "os", self.windows_os), \
                patch.object(omp_session.subprocess, "CREATE_NEW_CONSOLE", 16, create=True), \
                patch.object(omp_session.subprocess, "Popen", side_effect=registered) as spawn, \
                redirect_stdout(output):
            code = omp_session.main(["--workspace", str(self.workspace), "--model", self.model,
                                     "--mode", "workspace", "--guarded"])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "guard_registered")
        self.assertEqual(result["omp_isolation"], "guarded")
        self.assertEqual(result["process_id"], 456)
        self.assertEqual(result["launcher_process_id"], 123)
        self.assertEqual(result["session_id"], "fresh-session:1")
        spawn.assert_called_once()
        child.wait.assert_not_called()

    def test_guarded_missing_or_invalid_proof_stays_pending_without_restart(self):
        for invalid in (None, {"launch_id": "old"}, {"guard_sha256": "wrong"},
                        {"process_id": True}, {"session_id": "invalid/session"}):
            with self.subTest(invalid=invalid):
                child = Mock(pid=123)
                child.poll.return_value = None

                def unregistered(command, **kwargs):
                    if invalid is not None:
                        directory = Path(kwargs["env"]["PARR0T_OMP_CONTROL_DIRECTORY"])
                        guard = Path(command[command.index("--trusted-extension") + 1])
                        proof = {"schema": "parr0t.omp-control-ready.v1", "launch_id": directory.name,
                                 "process_id": 456, "guard_sha256": omp_session.sha256(guard.read_bytes()).hexdigest(),
                                 "session_id": "fresh-session:1", **invalid}
                        (directory / "ready.json").write_text(json.dumps(proof), encoding="utf-8")
                    return child

                with patch.object(omp_session, "os", self.windows_os), \
                        patch.object(omp_session.subprocess, "CREATE_NEW_CONSOLE", 16, create=True), \
                        patch.object(omp_session.subprocess, "Popen", side_effect=unregistered) as spawn, \
                        patch.object(omp_session.time, "monotonic", side_effect=[0, 0, 2]), \
                        patch.object(omp_session.time, "sleep"):
                    result = omp_session._launch(self.workspace, self.model, 1, mode="workspace", guarded=True)
                self.assertEqual(result["status"], "startup_pending")
                self.assertIsNone(result["process_id"])
                self.assertNotIn("session_id", result)
                spawn.assert_called_once()
                child.wait.assert_not_called()

    def test_guarded_process_failure_does_not_restart(self):
        child = Mock(pid=123)
        child.poll.return_value = 7
        with patch.object(omp_session, "os", self.windows_os), \
                patch.object(omp_session.subprocess, "CREATE_NEW_CONSOLE", 16, create=True), \
                patch.object(omp_session.subprocess, "Popen", return_value=child) as spawn:
            result = omp_session._launch(self.workspace, self.model, mode="workspace", guarded=True)
        self.assertEqual(result["status"], "startup_failed")
        self.assertEqual(result["launcher_process_id"], 123)
        spawn.assert_called_once()
        child.wait.assert_not_called()

    def test_guarded_pending_cli_is_not_reported_as_success(self):
        output = io.StringIO()
        with patch.object(omp_session, "_launch", return_value={"status": "startup_pending"}) as launch, \
                redirect_stdout(output):
            code = omp_session.main(["--workspace", str(self.workspace), "--model", self.model,
                                     "--mode", "workspace", "--guarded"])
        self.assertEqual(code, 1)
        self.assertTrue(launch.call_args.kwargs["guarded"])


if __name__ == "__main__":
    unittest.main()
