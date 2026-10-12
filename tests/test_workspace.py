from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.privacy import PrivacyViolation
from game_l10n.tooling import run_cached_tool
from game_l10n.workspace import initialize_workspace, prepare_sdk_profile, update_repository_policy


class WorkspaceTests(unittest.TestCase):
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
