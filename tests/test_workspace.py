from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from game_l10n.cli import cmd_status, cmd_translate
from game_l10n.control_plane import record_control_plane_leak
from game_l10n.errors import ConfigurationError
from game_l10n.privacy import PrivacyViolation
from game_l10n.providers import ProviderRegistry
from game_l10n.tooling import run_cached_tool
from game_l10n.workspace import (
    initialize_workspace,
    load_config,
    prepare_sdk_profile,
    publish_sdk_profile,
    record_sdk_evaluation,
    record_sdk_selection,
    refresh_sdk_inventory,
    require_sdk_selection,
    update_repository_policy,
)


class WorkspaceTests(unittest.TestCase):
    def test_api_provider_preset_is_ready_after_keys_are_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(
                work,
                sdk,
                git_choice="disabled",
                provider_preset="openai-deepseek",
            )
            config = load_config(work)
            generated_agents = (work / "AGENTS.md").read_text(encoding="utf-8")
            control_state = json.loads(
                (work / ".game-l10n" / "control-plane-state.json").read_text(
                    encoding="utf-8"
                )
            )

        providers = {item["name"]: item for item in config["providers"]}
        self.assertEqual(config["provider_preset"], "openai-deepseek")
        self.assertEqual(config["execution_mode"], "api")
        self.assertFalse(config["provider_policy"]["key_presence_selects_mode"])
        self.assertEqual(config["project"]["context_mode"], "auto")
        self.assertEqual(providers["openai-luna"]["model"], "gpt-6-luna")
        self.assertEqual(providers["deepseek"]["model"], "deepseek-flash")
        self.assertNotIn("temperature", providers["deepseek"])
        self.assertEqual(providers["deepseek"]["thinking"], "disabled")
        self.assertEqual(providers["deepseek"]["reasoning_effort"], "none")
        self.assertEqual(providers["deepseek"]["review_verdict_format"], "plain")
        self.assertEqual(providers["openai-sol"]["model"], "gpt-6.1-sol")
        self.assertEqual(providers["openai-luna"]["reasoning_effort"], "none")
        self.assertEqual(providers["openai-sol"]["reasoning_effort"], "low")
        self.assertEqual(config["routing"]["translators"], ["openai-luna", "deepseek"])
        self.assertEqual(config["routing"]["reviewers"], ["openai-sol", "deepseek"])
        self.assertEqual(config["routing"]["rewriters"], ["deepseek", "openai-sol"])
        self.assertEqual(config["routing"]["review_max_rounds"], 2)
        self.assertEqual(
            providers["openai-sol"]["task_max_output_tokens"][
                "game_translation_review_judge"
            ],
            512,
        )
        self.assertEqual(config["routing"]["grace_retries"], 3)
        self.assertEqual(config["routing"]["review_refusal_policy"], "fallback")
        self.assertTrue(config["routing"]["defer_fallbacks_until_primary_complete"])
        self.assertTrue(config["routing"]["preserve_source_on_failure"])
        self.assertEqual(config["execution"]["scope_mode"], "minimum_requested_artifact")
        self.assertEqual(config["execution"]["validation_profile"], "proportional")
        self.assertEqual(config["execution"]["same_failure_limit"], 2)
        self.assertFalse(config["execution"]["delegate_simple_tasks"])
        self.assertEqual(config["execution"]["native_shard_records"], 32)
        self.assertEqual(config["execution"]["native_shard_material_tokens"], 12000)
        self.assertEqual(config["execution"]["native_worker_history"], "none")
        self.assertFalse(config["execution"]["native_worker_reuse"])
        self.assertEqual(
            config["control_plane"]["envelope_schema"],
            "parr0t.control-envelope.v2",
        )
        self.assertTrue(config["control_plane"]["metadata_only_for_all_data_plane_stages"])
        self.assertTrue(config["control_plane"]["reject_free_form_worker_returns"])
        self.assertTrue(config["control_plane"]["rotate_after_leak"])
        self.assertEqual(control_state["status"], "ready")
        self.assertEqual(control_state["generation"], 1)
        self.assertTrue(config["coverage"]["require_independent_inventory"])
        self.assertTrue(config["coverage"]["separate_static_and_runtime_completion"])
        self.assertTrue(config["failure_queue"]["resume_only_after_user_request"])
        self.assertTrue(config["failure_queue"]["hold_patch_when_blocking_target_fails"])
        self.assertTrue(config["release_checkpoint"]["required_for_distribution"])
        self.assertTrue(config["release_checkpoint"]["reuse_verified_unchanged_targets"])
        self.assertTrue(config["release_checkpoint"]["selective_user_corrections"])
        self.assertEqual(
            config["release_checkpoint"]["locale_fallback"],
            "best_effort_declared",
        )
        self.assertTrue(config["instance_release"]["dormant_until_user_publication_command"])
        self.assertTrue(config["instance_release"]["gate_required_before_external_publication"])
        self.assertTrue(config["instance_release"]["package_validation_is_not_release_readiness"])
        self.assertEqual(config["instance_release"]["metadata_file"], "parr0t-instance-release.json")
        self.assertEqual(config["runtime"]["default_policy"], "deny")
        self.assertTrue(config["runtime"]["separate_install_and_launch"])
        self.assertTrue(config["runtime"]["user_executes_by_default"])
        self.assertNotIn("publish_approved", config["repository"])
        self.assertIn("docs/instance-release.md", generated_agents)
        self.assertIn("exact verified bytes", generated_agents)
        self.assertIn("Preserve the user's existing README", generated_agents)
        self.assertIn("default to Windows", generated_agents)
        self.assertIn("player's execution environment", generated_agents)
        self.assertIn("workflow is dormant", generated_agents)
        self.assertIn("do not mention, propose, ask about, preview, or prepare", generated_agents.lower())
        self.assertIn("only as GitHub Release assets", generated_agents)
        self.assertIn("game-l10n instance-release-gate", generated_agents)
        self.assertIn("before any push, tag, `gh release create`, or upload", generated_agents)
        self.assertIn("duplicate translation exports out of the instance branch", generated_agents)
        self.assertIn("translation source repository", generated_agents)
        self.assertIn("use each for its separate role before any model call", generated_agents)
        self.assertIn("<Game Display Name> Patch <Patch Version> (Game Version <Game Version>)", generated_agents)
        self.assertIn("docs/installer-contract.md", generated_agents)
        self.assertIn("docs/steam-faq.md", generated_agents)
        self.assertIn("docs/wrapper-executable.md", generated_agents)
        self.assertIn("parr0t-patch-status.txt", generated_agents)
        self.assertIn("interactive player's menu choice", generated_agents)
        self.assertIn("game-l10n verify-inventory", generated_agents)
        self.assertIn("game-l10n review-existing", generated_agents)
        self.assertIn("metadata.blocking_on_failure=true", generated_agents)
        self.assertIn("parr0t-release-checkpoint.json", generated_agents)
        self.assertIn("reuse a reviewed target only", generated_agents)
        self.assertIn("fallback as unsupported", generated_agents)
        self.assertIn("docs/segment-metadata.md", generated_agents)
        self.assertIn("ui_slot_role", generated_agents)
        self.assertIn("game-l10n inspect-installed-state", generated_agents)
        self.assertIn("docs/minimal-corrections.md", generated_agents)
        self.assertIn("game-l10n apply-user-correction", generated_agents)
        self.assertIn("zero model/context calls", generated_agents)
        self.assertIn("fresh native worker", generated_agents)
        self.assertIn("no parent conversation history", generated_agents)
        self.assertIn("parr0t.control-envelope.v2", generated_agents)
        self.assertIn("fallback_required", generated_agents)
        self.assertIn("route evidence", generated_agents)
        self.assertIn("game-l10n validate-control-envelope", generated_agents)
        self.assertIn("game-l10n record-control-plane-leak", generated_agents)
        self.assertIn("fresh control plane", generated_agents)
        self.assertIn("preserve it once", generated_agents)
        self.assertIn("Never read or edit `localconfig.vdf`", generated_agents)
        self.assertIn("ad hoc game-workspace binary", generated_agents)
        self.assertIn("skills/request-scope-guard/SKILL.md", generated_agents)
        self.assertIn("Re-evaluate after every user message", generated_agents)
        self.assertIn("give workers only the exact authorized targets and side effects", generated_agents)
        self.assertIn("game-l10n sdk-inventory", generated_agents)
        self.assertIn("docs/engine-faq-contract.md", generated_agents)
        self.assertIn("automated_fix", generated_agents)
        self.assertIn("engine_runbook", generated_agents)
        self.assertIn("Entry count is not a goal", generated_agents)
        self.assertIn("implementation reuse separate from compatibility ownership", generated_agents)
        self.assertIn("derive-new", generated_agents)
        self.assertIn("promotion engine/version must equal the target identity", generated_agents)
        self.assertIn("private-new", generated_agents)
        self.assertEqual(config["sdk_selection"]["status"], "pending")

    def test_default_provider_preset_is_locked_codex_app(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="disabled")
            config = load_config(work)

        self.assertEqual(config["provider_preset"], "codex-app-deepseek")
        self.assertEqual(config["execution_mode"], "codex_app_native")
        self.assertEqual([item["name"] for item in config["providers"]], ["deepseek"])

    def test_codex_app_preset_locks_native_workers_and_scopes_key_to_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "fixture-openai-key",
                "DEEPSEEK_API_KEY": "fixture-deepseek-key",
            },
        ):
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(
                work,
                sdk,
                git_choice="disabled",
                provider_preset="codex-app-deepseek",
            )
            config = load_config(work)
            mode_lock = json.loads(
                (work / ".game-l10n" / "provider-mode.json").read_text(encoding="utf-8")
            )

        self.assertEqual(config["provider_preset"], "codex-app-deepseek")
        self.assertEqual(config["execution_mode"], "codex_app_native")
        self.assertEqual(mode_lock["provider_preset"], "codex-app-deepseek")
        self.assertEqual(mode_lock["execution_mode"], "codex_app_native")
        self.assertEqual(
            config["native_workflow"]["launcher"],
            "collaboration",
        )
        self.assertFalse(config["native_workflow"]["child_model_cli_allowed"])
        self.assertFalse(config["native_workflow"]["external_provider_primary_allowed"])
        self.assertEqual([item["name"] for item in config["providers"]], ["deepseek"])
        self.assertEqual(
            set(config["providers"][0]["allowed_roles"]),
            {"translation_fallback", "selective_rewrite", "judge_fallback"},
        )
        self.assertEqual(config["routing"]["context_analyzers"], ["native-sol"])
        self.assertEqual(config["routing"]["translators"], ["native-luna", "deepseek"])
        self.assertEqual(config["routing"]["reviewers"], ["native-sol", "deepseek"])
        self.assertEqual(config["routing"]["rewriters"], ["deepseek"])

    def test_codex_app_mode_lock_rejects_agent_switch_to_custom(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(
                work,
                sdk,
                git_choice="disabled",
                provider_preset="codex-app-deepseek",
            )
            config_path = work / "game-l10n.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["provider_preset"] = "custom"
            config["execution_mode"] = "custom_provider"
            config["provider_policy"]["selected_preset"] = "custom"
            config_path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigurationError, "E_PROVIDER_MODE_DRIFT"):
                load_config(work)

    def test_codex_app_rejects_child_command_provider_before_registry_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(
                work,
                sdk,
                git_choice="disabled",
                provider_preset="codex-app-deepseek",
            )
            config = load_config(work)
            config["providers"] = [
                {
                    "name": "native-luna",
                    "type": "command",
                    "command": ["codex.exe", "exec"],
                }
            ]

            with self.assertRaisesRegex(ConfigurationError, "E_PROVIDER_MODE_DRIFT"):
                ProviderRegistry.from_config(config, work)

    def test_codex_app_direct_translate_stops_before_reading_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(
                work,
                sdk,
                git_choice="disabled",
                provider_preset="codex-app-deepseek",
            )
            args = SimpleNamespace(
                workspace=str(work),
                input="inputs/fixture.jsonl",
                context_input=None,
                context_mode=None,
                output="translations/fixture.jsonl",
                force=False,
                force_context=False,
            )
            with patch("game_l10n.cli.read_segments") as read_segments:
                with self.assertRaisesRegex(
                    ConfigurationError,
                    "E_NATIVE_WORKFLOW_REQUIRED",
                ):
                    cmd_translate(args)
                read_segments.assert_not_called()

    def test_workspace_must_be_outside_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sdk = Path(directory) / "sdk"
            sdk.mkdir()
            with self.assertRaises(PrivacyViolation):
                initialize_workspace(sdk / "private-game", sdk)

    def test_tools_are_cached_and_profile_has_no_game_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="disabled", game_roots=[root / "installed-game"])
            source = work / "game-data" / "asset.bin"
            source.write_bytes(b"private")
            output = work / "inputs" / "unpacked.bin"
            record_sdk_selection(
                work,
                "private-new",
                target_engine="fixture-engine",
                target_version="1.0",
            )
            command = ["python", "tools/unpack.py", "{input}", "{output}"]
            first = run_cached_tool(
                stage="unpack", command=command, input_path=source, output_path=output, work_dir=work
            )
            second = run_cached_tool(
                stage="unpack", command=command, input_path=source, output_path=output, work_dir=work
            )
            profile = prepare_sdk_profile(work, "fixture-engine", "1.0", user_confirmed_success=True)
            manifest = json.loads((profile / "profile.json").read_text(encoding="utf-8"))
            exported = {path.relative_to(profile).as_posix() for path in profile.rglob("*") if path.is_file()}
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(manifest["profile"], "sdk-fixture-engine-1.0")
        self.assertNotIn("next_step", manifest)
        self.assertNotIn("publish", json.dumps(manifest).lower())
        self.assertEqual(exported, {"profile.json", "tools/pack.py", "tools/unpack.py"})

    def test_sdk_selection_separates_reference_reuse_from_branch_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()

            def git(*args: str) -> None:
                completed = subprocess.run(
                    ["git", *args], cwd=sdk, text=True, capture_output=True, shell=False
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

            git("init", "-b", "main")
            git("config", "user.name", "Fixture User")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "commit.gpgsign", "false")
            (sdk / "README.md").write_text("fixture\n", encoding="utf-8")
            git("add", "README.md")
            git("commit", "-m", "fixture main")
            git("switch", "-c", "sdk-fixture-1.0")
            (sdk / "docs").mkdir()
            (sdk / "docs" / "fixture-sdk.md").write_text("fixture contract\n", encoding="utf-8")
            git("add", "docs/fixture-sdk.md")
            git("commit", "-m", "fixture sdk")
            git("switch", "main")

            initialize_workspace(work, sdk, git_choice="disabled")
            selection = load_config(work)["sdk_selection"]
            self.assertEqual([item["branch"] for item in selection["candidates"]], ["sdk-fixture-1.0"])
            self.assertIn("docs/fixture-sdk.md", selection["candidates"][0]["public_contract_paths"])

            with self.assertRaises(Exception):
                record_sdk_selection(
                    work,
                    "private-new",
                    target_engine="fixture",
                    target_version="2.0",
                )
            with self.assertRaises(Exception):
                record_sdk_evaluation(
                    work,
                    "sdk-fixture-1.0",
                    "reference",
                    "reusable_for_new_identity",
                    target_engine="fixture",
                    target_version="2.0",
                )
            with self.assertRaises(Exception):
                record_sdk_evaluation(
                    work,
                    "sdk-fixture-1.0",
                    "partial",
                    "reusable_with_extension",
                    ["docs/fixture-sdk.md"],
                    target_engine="fixture",
                    target_version="2.0",
                )
            record_sdk_evaluation(
                work,
                "sdk-fixture-1.0",
                "reference",
                "reusable_for_new_identity",
                ["docs/fixture-sdk.md"],
                target_engine="fixture",
                target_version="2.0",
            )
            with self.assertRaises(Exception):
                record_sdk_selection(
                    work,
                    "extend",
                    "sdk-fixture-1.0",
                    target_engine="fixture",
                    target_version="2.0",
                )
            with self.assertRaises(Exception):
                record_sdk_selection(
                    work,
                    "private-new",
                    target_engine="fixture",
                    target_version="2.0",
                )
            decision = record_sdk_selection(
                work,
                "derive-new",
                "sdk-fixture-1.0",
                target_engine="fixture",
                target_version="2.0",
            )
            self.assertEqual(decision["mode"], "derive-new")
            self.assertEqual(decision["target"]["branch"], "sdk-fixture-2.0")
            self.assertEqual(require_sdk_selection(load_config(work)), decision)
            derived_profile = prepare_sdk_profile(
                work, "fixture", "2.0", user_confirmed_success=True
            )
            derived_manifest = json.loads(
                (derived_profile / "profile.json").read_text(encoding="utf-8")
            )
            self.assertEqual(derived_manifest["selection_mode"], "derive-new")
            self.assertEqual(derived_manifest["reference_branch"], "sdk-fixture-1.0")

            refreshed = refresh_sdk_inventory(work)
            self.assertEqual(refreshed["status"], "pending")
            self.assertIn("sdk-fixture-1.0", refreshed["evaluations"])
            self.assertIsNone(refreshed["decision"])

            record_sdk_evaluation(
                work,
                "sdk-fixture-1.0",
                "partial",
                "reusable_with_extension",
                ["docs/fixture-sdk.md"],
                target_engine="fixture",
                target_version="1.0",
            )
            same_identity = record_sdk_selection(
                work,
                "extend",
                "sdk-fixture-1.0",
                target_engine="fixture",
                target_version="1.0",
            )
            self.assertEqual(same_identity["mode"], "extend")
            self.assertEqual(same_identity["target"]["branch"], "sdk-fixture-1.0")

    def test_new_sdk_promotion_requires_private_new_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="disabled")
            with self.assertRaises(Exception):
                prepare_sdk_profile(work, "fixture-engine", "1.0", user_confirmed_success=True)
            record_sdk_selection(
                work,
                "private-new",
                target_engine="fixture-engine",
                target_version="1.0",
            )
            with self.assertRaises(Exception):
                prepare_sdk_profile(
                    work, "fixture-engine", "2.0", user_confirmed_success=True
                )
            profile = prepare_sdk_profile(
                work, "fixture-engine", "1.0", user_confirmed_success=True
            )
            manifest = json.loads((profile / "profile.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["selection_mode"], "private-new")
            self.assertIsNone(manifest["reference_branch"])

    def test_publication_rejects_profile_with_changed_target_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            completed = subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=sdk,
                text=True,
                capture_output=True,
                shell=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            initialize_workspace(work, sdk, git_choice="enabled")
            record_sdk_selection(
                work,
                "private-new",
                target_engine="fixture-engine",
                target_version="1.0",
            )
            profile = prepare_sdk_profile(
                work, "fixture-engine", "1.0", user_confirmed_success=True
            )
            profile_path = profile / "profile.json"
            manifest = json.loads(profile_path.read_text(encoding="utf-8"))
            manifest["profile"] = "sdk-fixture-engine-2.0"
            profile_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            update_repository_policy(work, publish_approved=True)
            with self.assertRaises(Exception):
                publish_sdk_profile(work, sdk, profile)

    def test_tool_output_cannot_overwrite_its_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fixture.bin"
            source.write_bytes(b"fixture")
            with self.assertRaises(Exception):
                run_cached_tool(
                    stage="pack",
                    command=["python", "tools/pack.py", "{input}", "{output}"],
                    input_path=source,
                    output_path=source,
                    work_dir=root,
                )

    def test_publish_approval_requires_initial_git_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="disabled")
            with self.assertRaises(Exception):
                update_repository_policy(work, publish_approved=True)

    def test_publication_state_exists_only_after_explicit_policy_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="enabled")
            self.assertNotIn("publish_approved", load_config(work)["repository"])
            update_repository_policy(work, publish_approved=True)
            self.assertTrue(load_config(work)["repository"]["publish_approved"])

    def test_ordinary_status_does_not_surface_inactive_publication_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="enabled")
            update_repository_policy(work, publish_approved=False)
            output = StringIO()
            with redirect_stdout(output):
                cmd_status(SimpleNamespace(workspace=str(work)))
            status = json.loads(output.getvalue())
            self.assertEqual(status["repository"], {"initial_choice": "enabled"})
            self.assertEqual(status["control_plane"]["status"], "ready")

    def test_status_reports_only_rotation_action_after_control_plane_leak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="disabled")
            record_control_plane_leak(work, "semantic_judgment_performed")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = cmd_status(SimpleNamespace(workspace=str(work)))
            status = json.loads(output.getvalue())

        self.assertEqual(exit_code, 1)
        self.assertEqual(status["control_plane"]["status"], "rotation_required")
        self.assertEqual(status["next_actions"], ["resume_in_fresh_control_plane"])


if __name__ == "__main__":
    unittest.main()
