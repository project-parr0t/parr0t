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
        self.assertLessEqual(len(generated_agents.split()), 1000)
        for required in (
            "game-l10n status",
            "skills/request-scope-guard/SKILL.md",
            "Do not inspect images for text",
            "two independent complete ordered inventories",
            "primary translation",
            "translation fallback for failures only",
            "complete independent review",
            "review fallback for failures only",
            "parr0t.control-envelope.v2",
            "game-l10n validate-control-envelope",
            "game-l10n record-control-plane-leak",
            "fresh control plane",
            "fallback_required",
            "route evidence",
            "parr0t.translation-exchange/v1",
            "metadata.blocking_on_failure=true",
            "docs/sdk-selection.md",
            "docs/engine-faq-contract.md",
            "docs/coverage-gates.md",
            "docs/release-checkpoint.md",
            "docs/minimal-corrections.md",
            "docs/segment-metadata.md",
            "docs/runtime-approval.md",
            "docs/installer-contract.md",
            "docs/steam-faq.md",
            "docs/wrapper-executable.md",
            "docs/instance-release.md",
            "startup-current-directory-relative paths",
        ):
            self.assertIn(required, generated_agents)
        self.assertLess(
            generated_agents.index("primary translation"),
            generated_agents.index("translation fallback for failures only"),
        )
        self.assertLess(
            generated_agents.index("translation fallback for failures only"),
            generated_agents.index("complete independent review"),
        )
        self.assertLess(
            generated_agents.index("complete independent review"),
            generated_agents.index("review fallback for failures only"),
        )
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
            output.write_bytes(b"changed")
            third = run_cached_tool(
                stage="unpack", command=command, input_path=source, output_path=output, work_dir=work
            )
            profile = prepare_sdk_profile(work, "fixture-engine", "1.0", user_confirmed_success=True)
            manifest = json.loads((profile / "profile.json").read_text(encoding="utf-8"))
            exported = {path.relative_to(profile).as_posix() for path in profile.rglob("*") if path.is_file()}
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(third["status"], "completed")
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

    def test_status_returns_one_immediate_action_and_its_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            game = root / "game"
            work = root / "work"
            sdk.mkdir()
            game.mkdir()
            initialize_workspace(work, sdk, game_roots=[game])

            first_output = StringIO()
            with redirect_stdout(first_output):
                cmd_status(SimpleNamespace(workspace=str(work)))
            first = json.loads(first_output.getvalue())

            update_repository_policy(work, initial_choice="disabled")
            second_output = StringIO()
            with redirect_stdout(second_output):
                cmd_status(SimpleNamespace(workspace=str(work)))
            second = json.loads(second_output.getvalue())

        self.assertEqual(first["next_actions"], ["ask_user_for_initial_git_choice"])
        self.assertEqual(first["contract_base"], str(sdk.resolve()))
        self.assertEqual(
            first["required_contracts"],
            ["skills/request-scope-guard/SKILL.md"],
        )
        self.assertEqual(
            second["next_actions"],
            ["record_target_identity_evaluate_and_select_sdk"],
        )
        self.assertEqual(
            second["required_contracts"],
            ["docs/sdk-selection.md", "docs/sdk-branch-contract.md"],
        )

    def test_status_exit_code_matches_the_immediate_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            missing_game = root / "missing-game"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, game_roots=[missing_game])

            first_output = StringIO()
            with redirect_stdout(first_output):
                first_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            first = json.loads(first_output.getvalue())

            update_repository_policy(work, initial_choice="disabled")
            second_output = StringIO()
            with redirect_stdout(second_output):
                second_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            second = json.loads(second_output.getvalue())

        self.assertEqual(first_exit, 0)
        self.assertEqual(first["next_actions"], ["ask_user_for_initial_git_choice"])
        self.assertEqual(second_exit, 1)
        self.assertEqual(second["next_actions"], ["fix_missing_game_root"])
        self.assertEqual(second["required_contracts"], [])

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
        self.assertEqual(
            status["required_contracts"], ["docs/control-plane-contract.md"]
        )

    def test_status_routes_every_post_context_stage_to_local_patch_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            game = root / "game"
            work = root / "work"
            sdk.mkdir()
            game.mkdir()
            initialize_workspace(work, sdk, game_roots=[game], git_choice="disabled")
            record_sdk_selection(
                work,
                "private-new",
                target_engine="fixture-engine",
                target_version="1.0",
            )
            (work / "inputs" / "primary.json").write_text("{}\n", encoding="utf-8")
            state_root = work / ".game-l10n"
            inventory_fingerprint = "fixture-inventory-fingerprint"
            (state_root / "inventory-attestation.json").write_text(
                json.dumps(
                    {
                        "schema": "parr0t.inventory-attestation.v1",
                        "status": "static_verified",
                        "static_complete": True,
                        "primary_fingerprint": inventory_fingerprint,
                    }
                ),
                encoding="utf-8",
            )
            (work / "context" / "context-pack.json").write_text("{}\n", encoding="utf-8")
            (state_root / "context-state.json").write_text(
                json.dumps(
                    {
                        "status": "success",
                        "execution": {
                            "status": "success",
                            "total_parts": 1,
                            "completed_parts": 1,
                            "parts": [{"status": "success"}],
                        },
                    }
                ),
                encoding="utf-8",
            )

            translation_output = StringIO()
            with redirect_stdout(translation_output):
                translation_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            translation_status = json.loads(translation_output.getvalue())

            (state_root / "completion-attestation.json").write_text(
                json.dumps(
                    {
                        "schema": "parr0t.completion-attestation.v1",
                        "source_inventory": {
                            "fingerprint": inventory_fingerprint,
                            "status": "static_verified",
                            "static_complete": True,
                        },
                        "result_alignment": {"complete": True},
                        "target_scope": {"full_inventory": True},
                        "semantic_review": {"complete": False},
                        "target_completion": False,
                        "static_completion": False,
                        "issue_codes": ["SEMANTIC_REVIEW_INCOMPLETE"],
                    }
                ),
                encoding="utf-8",
            )
            gap_output = StringIO()
            with redirect_stdout(gap_output):
                gap_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            gap_status = json.loads(gap_output.getvalue())

            (state_root / "completion-attestation.json").write_text(
                json.dumps(
                    {
                        "schema": "parr0t.completion-attestation.v1",
                        "source_inventory": {
                            "fingerprint": inventory_fingerprint,
                            "status": "static_verified",
                            "static_complete": True,
                        },
                        "result_alignment": {"complete": True},
                        "target_scope": {"full_inventory": True},
                        "semantic_review": {"complete": True},
                        "target_completion": True,
                        "static_completion": True,
                        "issue_codes": [],
                    }
                ),
                encoding="utf-8",
            )
            pack_output = StringIO()
            with redirect_stdout(pack_output):
                pack_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            pack_status = json.loads(pack_output.getvalue())

            translated = work / "translations" / "full.json"
            translated.parent.mkdir(parents=True, exist_ok=True)
            translated.write_text("{}\n", encoding="utf-8")
            patch_path = work / "build" / "patch.json"
            run_cached_tool(
                stage="pack",
                command=["python", "tools/pack.py", "{input}", "{output}"],
                input_path=translated,
                output_path=patch_path,
                work_dir=work,
            )
            ready_output = StringIO()
            with redirect_stdout(ready_output):
                ready_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            ready_status = json.loads(ready_output.getvalue())

            patch_path.write_text("changed\n", encoding="utf-8")
            changed_output = StringIO()
            with redirect_stdout(changed_output):
                changed_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            changed_status = json.loads(changed_output.getvalue())

        self.assertEqual(translation_exit, 0)
        self.assertEqual(
            translation_status["next_actions"],
            ["run_primary_translation_review_pipeline"],
        )
        self.assertIn("docs/review-pipeline.md", translation_status["required_contracts"])
        self.assertEqual(gap_exit, 0)
        self.assertEqual(gap_status["next_actions"], ["resolve_translation_review_gaps"])
        self.assertEqual(pack_exit, 0)
        self.assertEqual(
            pack_status["next_actions"], ["validate_and_pack_recoverable_patch"]
        )
        self.assertEqual(ready_exit, 0)
        self.assertEqual(ready_status["next_actions"], ["local_patch_ready"])
        self.assertEqual(ready_status["local_artifact"]["status"], "valid")
        self.assertEqual(
            ready_status["local_artifact"]["artifact_path"], str(patch_path.resolve())
        )
        self.assertEqual(changed_exit, 0)
        self.assertEqual(
            changed_status["next_actions"], ["validate_and_pack_recoverable_patch"]
        )
        self.assertEqual(changed_status["state_issue_codes"], ["TOOL_OUTPUT_CHANGED"])

    def test_status_routes_invalid_and_stale_metadata_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            game = root / "game"
            work = root / "work"
            sdk.mkdir()
            game.mkdir()
            initialize_workspace(work, sdk, game_roots=[game], git_choice="disabled")
            record_sdk_selection(
                work,
                "private-new",
                target_engine="fixture-engine",
                target_version="1.0",
            )
            (work / "inputs" / "primary.json").write_text("{}\n", encoding="utf-8")
            state_root = work / ".game-l10n"
            inventory_path = state_root / "inventory-attestation.json"
            inventory_path.write_text("{", encoding="utf-8")

            invalid_output = StringIO()
            with redirect_stdout(invalid_output):
                invalid_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            invalid_status = json.loads(invalid_output.getvalue())

            inventory_path.write_text(
                json.dumps(
                    {
                        "schema": "parr0t.inventory-attestation.v1",
                        "status": "static_verified",
                        "static_complete": True,
                        "primary_fingerprint": "current-inventory",
                    }
                ),
                encoding="utf-8",
            )
            (state_root / "failure-queue.json").write_text(
                json.dumps(
                    {
                        "schema": "parr0t.failure-queue.v1",
                        "inventory_fingerprint": "old-inventory",
                        "pending_count": 1,
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )
            stale_output = StringIO()
            with redirect_stdout(stale_output):
                stale_exit = cmd_status(SimpleNamespace(workspace=str(work)))
            stale_status = json.loads(stale_output.getvalue())

        self.assertEqual(invalid_exit, 1)
        self.assertEqual(invalid_status["next_actions"], ["repair_invalid_workspace_state"])
        self.assertEqual(
            invalid_status["state_issue_codes"], ["INVENTORY_ATTESTATION_UNREADABLE"]
        )
        self.assertEqual(stale_exit, 1)
        self.assertEqual(
            stale_status["next_actions"], ["reconcile_stale_failure_queue"]
        )
        self.assertEqual(stale_status["state_issue_codes"], ["FAILURE_QUEUE_STALE"])


if __name__ == "__main__":
    unittest.main()
