from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from game_l10n.cli import cmd_status
from game_l10n.privacy import PrivacyViolation
from game_l10n.tooling import run_cached_tool
from game_l10n.workspace import (
    initialize_workspace,
    load_config,
    prepare_sdk_profile,
    record_sdk_evaluation,
    record_sdk_selection,
    refresh_sdk_inventory,
    require_sdk_selection,
    update_repository_policy,
)


class WorkspaceTests(unittest.TestCase):
    def test_default_provider_preset_is_ready_after_keys_are_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="disabled")
            config = load_config(work)
            generated_agents = (work / "AGENTS.md").read_text(encoding="utf-8")

        providers = {item["name"]: item for item in config["providers"]}
        self.assertEqual(config["provider_preset"], "openai-deepseek")
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
        self.assertEqual(config["execution"]["native_shard_records"], 256)
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
        self.assertIn("<Game Display Name> Patch <Patch Version> (Game Version <Game Version>)", generated_agents)
        self.assertIn("docs/installer-contract.md", generated_agents)
        self.assertIn("docs/steam-faq.md", generated_agents)
        self.assertIn("docs/wrapper-executable.md", generated_agents)
        self.assertIn("parr0t-patch-status.txt", generated_agents)
        self.assertIn("interactive player's menu choice", generated_agents)
        self.assertIn("preserve it once", generated_agents)
        self.assertIn("Never read or edit `localconfig.vdf`", generated_agents)
        self.assertIn("ad hoc game-workspace binary", generated_agents)
        self.assertIn("game-l10n sdk-inventory", generated_agents)
        self.assertIn("private-new", generated_agents)
        self.assertEqual(config["sdk_selection"]["status"], "pending")

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
            record_sdk_selection(work, "private-new")
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

    def test_existing_sdk_must_be_evaluated_before_private_new(self) -> None:
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
                record_sdk_selection(work, "private-new")
            with self.assertRaises(Exception):
                record_sdk_evaluation(
                    work, "sdk-fixture-1.0", "partial", "reusable_with_extension"
                )
            record_sdk_evaluation(
                work,
                "sdk-fixture-1.0",
                "partial",
                "reusable_with_extension",
                ["docs/fixture-sdk.md"],
            )
            with self.assertRaises(Exception):
                record_sdk_selection(work, "private-new")
            decision = record_sdk_selection(work, "extend", "sdk-fixture-1.0")
            self.assertEqual(decision["mode"], "extend")
            self.assertEqual(require_sdk_selection(load_config(work)), decision)

            refreshed = refresh_sdk_inventory(work)
            self.assertEqual(refreshed["status"], "pending")
            self.assertIn("sdk-fixture-1.0", refreshed["evaluations"])
            self.assertIsNone(refreshed["decision"])

    def test_new_sdk_promotion_requires_private_new_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="disabled")
            with self.assertRaises(Exception):
                prepare_sdk_profile(work, "fixture-engine", "1.0", user_confirmed_success=True)
            record_sdk_selection(work, "private-new")
            profile = prepare_sdk_profile(
                work, "fixture-engine", "1.0", user_confirmed_success=True
            )
            self.assertTrue((profile / "profile.json").is_file())

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


if __name__ == "__main__":
    unittest.main()
