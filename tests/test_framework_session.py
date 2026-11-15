from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from game_l10n import control_plane, framework_session
from game_l10n.cli import main
from game_l10n.errors import ConfigurationError
from game_l10n.models import ProjectContext, Segment
from game_l10n.native_pass import prepare_native_tasks
from game_l10n.orchestrator import LocalizationOrchestrator, OrchestratorConfig
from game_l10n.providers import ProviderRegistry, ScriptedProvider
from game_l10n.workspace import initialize_workspace


class FrameworkSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.workspace = root / "workspace"
        sdk = root / "sdk"
        sdk.mkdir()
        initialize_workspace(self.workspace, sdk, git_choice="disabled")
        self.external_log = root / "host.log"
        self.log_data = b"SYNTHETIC_DIAGNOSTIC_EVENT\n\xff\x00\n"
        self.external_log.write_bytes(self.log_data)

    def change(self, mode: str = "analysis", logs: list[Path] | None = None) -> dict:
        return framework_session.change_framework_mode(
            self.workspace, mode, user_requested=True, logs=logs,
        )

    def call(self, *arguments: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main([*arguments, "--workspace", str(self.workspace)])
        return result, output.getvalue(), error.getvalue()

    def test_mode_change_requires_actual_user_assertion(self) -> None:
        for confirmed in (False, 1):
            with self.subTest(confirmed=confirmed):
                with self.assertRaisesRegex(ConfigurationError, "USER_REQUEST_REQUIRED"):
                    framework_session.change_framework_mode(
                        self.workspace, "analysis", user_requested=confirmed,
                    )
        self.assertFalse((self.workspace / framework_session.FRAMEWORK_SESSION_PATH).exists())
        self.assertTrue(framework_session.framework_session_state(self.workspace)["instructions_active"])

    def test_clean_shutdown_is_not_mislabeled_a_leak_and_blocks_native_work(self) -> None:
        result = self.change("shutdown")
        self.assertFalse(result["framework_session"]["instructions_active"])
        state = control_plane.control_plane_state(self.workspace)
        self.assertEqual(state["issue_code"], "framework_suspended")
        self.assertEqual(state["incident_code"], "framework_suspended")
        self.assertEqual(state["generation"], 1)
        with self.assertRaisesRegex(ConfigurationError, "FRAMEWORK_INACTIVE"):
            prepare_native_tasks(
                self.workspace, self.workspace / "unopened.jsonl",
                [Segment("fixture-1", "SOURCE_FIXTURE")], self.workspace / "tasks",
            )

    def test_analysis_preserves_existing_contamination_without_new_session(self) -> None:
        incident = control_plane.record_control_plane_leak(self.workspace, "parent_opened_data_plane")
        state_path = self.workspace / control_plane.CONTROL_STATE_PATH
        before = state_path.read_bytes()
        self.change(logs=[self.external_log])
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(control_plane.control_plane_state(self.workspace)["incident_id"], incident["incident_id"])
        code, output, error = self.call("status")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["next_actions"], ["analyze_framework_logs"])
        self.assertNotIn("SYNTHETIC_DIAGNOSTIC_EVENT", output)

    def test_diagnostics_work_despite_corrupt_control_state_and_configuration(self) -> None:
        for path in (self.workspace / "game-l10n.json", self.workspace / control_plane.CONTROL_STATE_PATH):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("PRIVATE_SYNTHETIC_INVALID_JSON", encoding="utf-8")
        result = self.change(logs=[self.external_log])
        self.assertEqual(result["control_plane_issue"], "control_state_repair_required")
        relative = Path(result["framework_session"]["logs"][0]["path"])
        self.assertEqual(framework_session.diagnostic_log_bytes(self.workspace, relative), self.log_data)
        code, output, error = self.call("status")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["framework_session"]["mode"], "analysis")
        self.assertNotIn("PRIVATE_SYNTHETIC_INVALID_JSON", output)
        with self.assertRaisesRegex(ConfigurationError, "FRAMEWORK_INACTIVE"):
            control_plane.require_control_plane_ready(self.workspace)

    def test_reported_cli_stays_bound_despite_stale_import_paths(self) -> None:
        code, output, error = self.call("framework-mode", "--mode", "analysis", "--user-requested")
        self.assertEqual((code, error), (0, ""))
        command = json.loads(output)["core_runtime"]["cli_argv"]
        shadow = self.workspace.parent / "stale core \ud55c\uae00 & $"
        package = shadow / "game_l10n"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            "raise RuntimeError('SYNTHETIC_STALE_CORE_IMPORTED')\n", encoding="utf-8",
        )
        environment = {**os.environ, "PYTHONPATH": str(shadow)}
        result = subprocess.run(
            [*command, "framework-mode", "--workspace", str(self.workspace),
             "--mode", "shutdown", "--user-requested"],
            cwd=shadow, env=environment, capture_output=True, text=True, check=True,
        )
        state = json.loads(result.stdout)
        self.assertEqual(state["framework_session"]["mode"], "shutdown")
        self.assertFalse(state["framework_session"]["instructions_active"])
        result = subprocess.run(
            [*state["core_runtime"]["cli_argv"], "status", "--workspace", str(self.workspace)],
            cwd=shadow, env=environment, capture_output=True, text=True, check=True,
        )
        status = json.loads(result.stdout)
        self.assertEqual(status["next_actions"], ["framework_shutdown"])
        result = subprocess.run(
            [*status["core_runtime"]["cli_argv"], "framework-status", "--workspace", str(self.workspace)],
            cwd=shadow, env=environment, capture_output=True, text=True, check=True,
        )
        self.assertEqual(json.loads(result.stdout)["framework_session"], state["framework_session"])

    def test_log_grant_is_exact_and_original_changes_do_not_change_evidence(self) -> None:
        result = self.change(logs=[self.external_log])
        binding = result["framework_session"]["logs"][0]
        self.assertNotIn(str(self.external_log), json.dumps(result))
        self.external_log.write_bytes(b"DIFFERENT_SYNTHETIC_EVENT")
        self.assertEqual(
            framework_session.diagnostic_log_bytes(self.workspace, Path(binding["path"])),
            self.log_data,
        )
        for unapproved in (self.external_log, Path("../host.log"), Path("game-l10n.json")):
            with self.subTest(unapproved=unapproved):
                with self.assertRaisesRegex(ConfigurationError, "LOG_NOT_APPROVED"):
                    framework_session.diagnostic_log_bytes(self.workspace, unapproved)
        snapshot = self.workspace / binding["path"]
        snapshot.write_bytes(b"CHANGED_SYNTHETIC_SNAPSHOT")
        with self.assertRaisesRegex(ConfigurationError, "LOG_CHANGED"):
            framework_session.diagnostic_log_bytes(self.workspace, Path(binding["path"]))
        snapshot.unlink()
        with self.assertRaisesRegex(ConfigurationError, "LOG_UNAVAILABLE"):
            framework_session.diagnostic_log_bytes(self.workspace, Path(binding["path"]))

    def test_unreadable_selected_log_still_disables_localization_without_partial_grants(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "LOG_UNAVAILABLE"):
            self.change(logs=[self.external_log, Path("missing.log")])
        state = framework_session.framework_session_state(self.workspace)
        self.assertEqual(state["mode"], "analysis")
        self.assertFalse(state["instructions_active"])
        self.assertEqual(state["logs"], [])
        with self.assertRaisesRegex(ConfigurationError, "FRAMEWORK_INACTIVE"):
            control_plane.require_control_plane_ready(self.workspace)

    def test_shutdown_revokes_log_access_without_deleting_evidence_or_results(self) -> None:
        result = self.change(logs=[self.external_log])
        relative = Path(result["framework_session"]["logs"][0]["path"])
        completed = self.workspace / "locked-result.json"
        usage = self.workspace / ".game-l10n" / "native" / "control" / "usage.json"
        usage.parent.mkdir(parents=True, exist_ok=True)
        completed.write_bytes(b'{"fixture":"completed"}')
        usage.write_bytes(b'{"fixture_calls":9}')
        expected = {path: path.read_bytes() for path in (completed, usage, self.workspace / relative)}
        self.change("shutdown")
        self.assertEqual(framework_session.framework_session_state(self.workspace)["logs"], [])
        with self.assertRaisesRegex(ConfigurationError, "ANALYSIS_REQUIRED"):
            framework_session.diagnostic_log_bytes(self.workspace, relative)
        self.assertEqual({path: path.read_bytes() for path in expected}, expected)

    def test_new_analysis_request_replaces_log_grants(self) -> None:
        first = self.change(logs=[self.external_log])["framework_session"]["logs"][0]
        other = self.external_log.with_name("other.log")
        other.write_bytes(b"OTHER_SYNTHETIC_EVENT")
        second = self.change(logs=[other])["framework_session"]["logs"][0]
        with self.assertRaisesRegex(ConfigurationError, "LOG_NOT_APPROVED"):
            framework_session.diagnostic_log_bytes(self.workspace, Path(first["path"]))
        self.assertEqual(
            framework_session.diagnostic_log_bytes(self.workspace, Path(second["path"])),
            b"OTHER_SYNTHETIC_EVENT",
        )

    def test_existing_direct_orchestrator_cannot_translate_after_shutdown(self) -> None:
        calls = []
        provider = ScriptedProvider("fixture-provider", lambda **request: calls.append(request))
        orchestrator = LocalizationOrchestrator(
            registry=ProviderRegistry({provider.name: provider}),
            config=OrchestratorConfig([provider.name], [], require_review=False),
            project=ProjectContext("FIXTURE", "en", "ko"), work_dir=self.workspace,
        )
        self.change("shutdown")
        segment = Segment("fixture-1", "SOURCE_FIXTURE")
        for invoke in (
            lambda: orchestrator.translate_one(segment),
            lambda: orchestrator.translate_many([segment]),
            lambda: orchestrator.review_existing_one(segment, "TARGET_FIXTURE"),
            lambda: orchestrator.review_existing_many([segment], {}),
        ):
            with self.assertRaisesRegex(ConfigurationError, "FRAMEWORK_INACTIVE"):
                invoke()
        self.assertEqual(calls, [])

    def test_cli_cannot_reenable_localization_with_acceptance_or_entry(self) -> None:
        self.change()
        for command in (
            ("accept-control-plane-exposure", "--contracts-read", "--user-confirmed-exposure-acceptance"),
            ("enter-control-plane", "--contracts-read", "--source-unopened"),
            ("native-runtime", "--host", "omp"),
        ):
            with self.subTest(command=command):
                code, output, error = self.call(*command)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("FRAMEWORK_INACTIVE", error)
        code, _, error = self.call("resume-control-plane")
        self.assertEqual(code, 2)
        self.assertIn("FRESH_SESSION_REQUIRED", error)
        self.assertFalse(framework_session.framework_session_state(self.workspace)["instructions_active"])

    def test_canonical_fresh_recovery_restores_localization_and_revokes_logs(self) -> None:
        result = self.change(logs=[self.external_log])
        relative = Path(result["framework_session"]["logs"][0]["path"])
        code, _, error = self.call(
            "resume-control-plane", "--fresh-session-confirmed", "--contracts-read", "--source-unopened",
        )
        self.assertEqual((code, error), (0, ""))
        state = framework_session.framework_session_state(self.workspace)
        self.assertEqual(state["mode"], "localization")
        self.assertTrue(state["instructions_active"])
        self.assertEqual(state["logs"], [])
        self.assertEqual(control_plane.control_plane_state(self.workspace)["generation"], 2)
        control_plane.require_control_plane_entry(self.workspace)
        with self.assertRaisesRegex(ConfigurationError, "ANALYSIS_REQUIRED"):
            framework_session.diagnostic_log_bytes(self.workspace, relative)

    def test_interrupted_fresh_recovery_does_not_open_localization(self) -> None:
        self.change()
        original = control_plane._atomic_json

        def fail_final_state(path: Path, payload: dict) -> None:
            if payload.get("resumed_from_incident"):
                raise OSError("synthetic recovery interruption")
            original(path, payload)

        with patch.object(control_plane, "_atomic_json", side_effect=fail_final_state):
            with self.assertRaisesRegex(OSError, "synthetic recovery interruption"):
                control_plane.acknowledge_control_plane_rotation(
                    self.workspace, self.workspace / control_plane.CONTROL_HANDOFF_PATH,
                )
        with self.assertRaisesRegex(ConfigurationError, "ROTATION_REQUIRED"):
            control_plane.require_control_plane_ready(self.workspace)
        control_plane.acknowledge_control_plane_rotation(
            self.workspace, self.workspace / control_plane.CONTROL_HANDOFF_PATH,
        )
        control_plane.establish_control_plane_entry(self.workspace)
        control_plane.require_control_plane_ready(self.workspace)

    def test_malformed_lifecycle_state_fails_closed_without_echoing_private_values(self) -> None:
        self.change()
        path = self.workspace / framework_session.FRAMEWORK_SESSION_PATH
        valid = json.loads(path.read_text(encoding="utf-8"))
        invalid = [
            {**valid, "mode": ["PRIVATE_SYNTHETIC_MODE"]},
            {**valid, "revision": True},
            {**valid, "instructions_active": True},
            {**valid, "logs": [{"source": "PRIVATE_SYNTHETIC_LOG"}]},
            {**valid, "unsupported": "PRIVATE_SYNTHETIC_FIELD"},
        ]
        for state in invalid:
            with self.subTest(state=state):
                path.write_text(json.dumps(state), encoding="utf-8")
                with self.assertRaisesRegex(ConfigurationError, "SESSION_INVALID") as error:
                    control_plane.require_control_plane_ready(self.workspace)
                self.assertNotIn("PRIVATE_SYNTHETIC", str(error.exception))

    def test_late_analysis_snapshot_cannot_overwrite_newer_shutdown(self) -> None:
        original = framework_session._snapshot_log

        def shutdown_during_copy(workspace: Path, source: Path) -> dict:
            self.change("shutdown")
            return original(workspace, source)

        with patch.object(framework_session, "_snapshot_log", side_effect=shutdown_during_copy):
            with self.assertRaisesRegex(ConfigurationError, "MODE_CHANGED"):
                self.change(logs=[self.external_log])
        state = framework_session.framework_session_state(self.workspace)
        self.assertEqual(state["mode"], "shutdown")
        self.assertEqual(state["logs"], [])

    def test_log_revocation_during_read_returns_no_private_bytes(self) -> None:
        result = self.change(logs=[self.external_log])
        relative = Path(result["framework_session"]["logs"][0]["path"])
        original = Path.read_bytes

        def revoked(path: Path) -> bytes:
            if path == self.workspace / relative:
                self.change("shutdown")
            return original(path)

        with patch.object(Path, "read_bytes", revoked):
            with self.assertRaisesRegex(ConfigurationError, "LOG_NOT_APPROVED"):
                framework_session.diagnostic_log_bytes(self.workspace, relative)

    def test_mode_and_recovery_mutations_share_one_exclusive_transaction(self) -> None:
        self.change()
        with framework_session._framework_session_lock(self.workspace):
            with self.assertRaisesRegex(ValueError, "TRANSITION_BUSY"):
                self.change("shutdown")
            with self.assertRaisesRegex(ValueError, "TRANSITION_BUSY"):
                control_plane.acknowledge_control_plane_rotation(
                    self.workspace, self.workspace / control_plane.CONTROL_HANDOFF_PATH,
                )
        self.assertEqual(framework_session.framework_session_state(self.workspace)["mode"], "analysis")


if __name__ == "__main__":
    unittest.main()
