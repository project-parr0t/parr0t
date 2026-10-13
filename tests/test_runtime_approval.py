from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from game_l10n.errors import RuntimeApprovalError
from game_l10n.runtime_approval import create_runtime_approval, require_runtime_approval
from game_l10n.workspace import initialize_workspace


class RuntimeApprovalTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        sdk = root / "sdk"
        work = root / "work"
        game = root / "fixture-game"
        sdk.mkdir()
        game.mkdir()
        executable = game / "fixture-game.exe"
        executable.write_bytes(b"fixture-executable")
        initialize_workspace(work, sdk, git_choice="disabled", game_roots=[game])
        artifact = work / "build" / "fixture-patch.bin"
        artifact.parent.mkdir()
        artifact.write_bytes(b"fixture-patch")
        return work, game, executable, artifact

    def test_exact_approval_is_one_shot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, _, artifact = self._fixture(Path(directory))
            approval_path, _ = create_runtime_approval(
                work,
                action="install",
                artifact_path=artifact,
                target_path=game,
                user_confirmed=True,
            )
            require_runtime_approval(
                work,
                approval_path,
                action="install",
                artifact_path=artifact,
                target_path=game,
                consume=True,
            )
            with self.assertRaisesRegex(RuntimeApprovalError, "E_RUNTIME_APPROVAL_CONSUMED"):
                require_runtime_approval(
                    work,
                    approval_path,
                    action="install",
                    artifact_path=artifact,
                    target_path=game,
                )

    def test_changed_artifact_and_action_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, _, artifact = self._fixture(Path(directory))
            approval_path, _ = create_runtime_approval(
                work,
                action="install",
                artifact_path=artifact,
                target_path=game,
                user_confirmed=True,
            )
            with self.assertRaisesRegex(RuntimeApprovalError, "E_RUNTIME_APPROVAL_ACTION_MISMATCH"):
                require_runtime_approval(
                    work,
                    approval_path,
                    action="update",
                    artifact_path=artifact,
                    target_path=game,
                )
            artifact.write_bytes(b"changed-fixture-patch")
            with self.assertRaisesRegex(RuntimeApprovalError, "E_RUNTIME_APPROVAL_ARTIFACT_MISMATCH"):
                require_runtime_approval(
                    work,
                    approval_path,
                    action="install",
                    artifact_path=artifact,
                    target_path=game,
                )

    def test_launch_requires_its_exact_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, executable, artifact = self._fixture(Path(directory))
            with self.assertRaisesRegex(RuntimeApprovalError, "E_RUNTIME_EXECUTABLE_REQUIRED"):
                create_runtime_approval(
                    work,
                    action="launch",
                    artifact_path=artifact,
                    target_path=game,
                    user_confirmed=True,
                )
            approval_path, _ = create_runtime_approval(
                work,
                action="launch",
                artifact_path=artifact,
                target_path=game,
                executable_path=executable,
                user_confirmed=True,
            )
            require_runtime_approval(
                work,
                approval_path,
                action="launch",
                artifact_path=artifact,
                target_path=game,
                executable_path=executable,
            )
            other_executable = game / "fixture-other.exe"
            other_executable.write_bytes(b"fixture-other-executable")
            with self.assertRaisesRegex(RuntimeApprovalError, "E_RUNTIME_APPROVAL_EXECUTABLE_MISMATCH"):
                require_runtime_approval(
                    work,
                    approval_path,
                    action="launch",
                    artifact_path=artifact,
                    target_path=game,
                    executable_path=other_executable,
                )

    def test_expired_approval_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work, game, _, artifact = self._fixture(Path(directory))
            approval_path, approval = create_runtime_approval(
                work,
                action="install",
                artifact_path=artifact,
                target_path=game,
                user_confirmed=True,
            )
            approval["expires_at_utc"] = "2000-01-01T00:00:00Z"
            approval_path.write_text(json.dumps(approval, indent=2) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeApprovalError, "E_RUNTIME_APPROVAL_EXPIRED"):
                require_runtime_approval(
                    work,
                    approval_path,
                    action="install",
                    artifact_path=artifact,
                    target_path=game,
                )

    def test_user_confirmation_and_configured_target_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work, game, _, artifact = self._fixture(root)
            with self.assertRaisesRegex(RuntimeApprovalError, "E_RUNTIME_USER_CONFIRMATION_REQUIRED"):
                create_runtime_approval(
                    work,
                    action="install",
                    artifact_path=artifact,
                    target_path=game,
                    user_confirmed=False,
                )
            other = root / "other-game"
            other.mkdir()
            with self.assertRaisesRegex(RuntimeApprovalError, "E_RUNTIME_TARGET_NOT_CONFIGURED"):
                create_runtime_approval(
                    work,
                    action="install",
                    artifact_path=artifact,
                    target_path=other,
                    user_confirmed=True,
                )


if __name__ == "__main__":
    unittest.main()
