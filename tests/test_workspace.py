from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.privacy import PrivacyViolation
from game_l10n.tooling import run_cached_tool
from game_l10n.workspace import initialize_workspace, load_config, prepare_sdk_profile, update_repository_policy


class WorkspaceTests(unittest.TestCase):
    def test_default_provider_preset_is_ready_after_keys_are_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            sdk.mkdir()
            initialize_workspace(work, sdk, git_choice="disabled")
            config = load_config(work)

        providers = {item["name"]: item for item in config["providers"]}
        self.assertEqual(config["provider_preset"], "openai-deepseek")
        self.assertEqual(providers["openai-luna"]["model"], "gpt-6-luna")
        self.assertEqual(providers["deepseek"]["model"], "deepseek-flash")
        self.assertEqual(providers["deepseek"]["temperature"], 0.0)
        self.assertEqual(providers["openai-sol"]["model"], "gpt-6.1-sol")
        self.assertEqual(config["routing"]["translators"], ["openai-luna", "deepseek"])
        self.assertTrue(config["routing"]["defer_fallbacks_until_primary_complete"])
        self.assertTrue(config["routing"]["preserve_source_on_failure"])

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
            command = ["python", "tools/unpack.py", "{input}", "{output}"]
            first = run_cached_tool(
                stage="unpack", command=command, input_path=source, output_path=output, work_dir=work
            )
            second = run_cached_tool(
                stage="unpack", command=command, input_path=source, output_path=output, work_dir=work
            )
            profile = prepare_sdk_profile(work, "unity", "2023.1", user_confirmed_success=True)
            manifest = json.loads((profile / "profile.json").read_text(encoding="utf-8"))
            exported = {path.relative_to(profile).as_posix() for path in profile.rglob("*") if path.is_file()}
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(manifest["profile"], "sdk-unity-2023.1")
        self.assertEqual(exported, {"profile.json", "tools/pack.py", "tools/unpack.py"})

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


if __name__ == "__main__":
    unittest.main()
