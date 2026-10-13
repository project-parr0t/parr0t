from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from game_l10n.runtime_approval import create_runtime_approval
from game_l10n.steam import MANIFEST_NAME, SteamPackageError, build_steam_package, validate_steam_package
from game_l10n.workspace import initialize_workspace


class SteamPackageTests(unittest.TestCase):
    def test_package_is_deterministic_and_manifest_driven(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "fixture-payload"
            (payload / "fixture-plugin").mkdir(parents=True)
            (payload / "fixture-plugin" / "synthetic.bin").write_bytes(b"synthetic-payload")
            spec = root / "fixture-spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture localization patch",
                        "steam": {
                            "app_id": "123456",
                            "install_dir": "Fixture Game",
                            "required_files": ["fixture-game.exe"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            first = root / "fixture-first.zip"
            second = root / "fixture-second.zip"
            result = build_steam_package(payload, spec, first)
            build_steam_package(payload, spec, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(result["status"], "valid")
            self.assertEqual(result["files"], 1)
            with zipfile.ZipFile(first) as archive:
                manifest = json.loads(archive.read(MANIFEST_NAME))
                installer = archive.read("Install-Translation-Patch.ps1").decode("utf-8")
                names = set(archive.namelist())

        self.assertEqual(manifest["schema"], "parr0t.steam.patch/v2")
        self.assertEqual(len(manifest["artifact_fingerprint"]), 64)
        self.assertEqual(len(result["installer_sha256"]), 64)
        self.assertEqual(manifest["files"][0]["path"], "fixture-plugin/synthetic.bin")
        self.assertIn("payload/fixture-plugin/synthetic.bin", names)
        self.assertIn("if (-not (Test-Path -LiteralPath $root -PathType Container))", installer)
        self.assertIn("E_UNOWNED_COLLISION", installer)
        self.assertIn("E_RUNTIME_APPROVAL_REQUIRED", installer)
        self.assertIn("E_RUNTIME_APPROVAL_INSTALLER_MISMATCH", installer)
        self.assertNotIn("previous-package", installer.casefold())
        self.assertNotIn("legacy fingerprint", installer.casefold())

    def test_validator_rejects_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "fixture-payload"
            payload.mkdir()
            (payload / "synthetic.bin").write_bytes(b"source")
            spec = root / "fixture-spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture localization patch",
                        "steam": {"app_id": "123456", "install_dir": "Fixture Game"},
                    }
                ),
                encoding="utf-8",
            )
            package = root / "fixture.zip"
            build_steam_package(payload, spec, package)
            rewritten = root / "fixture-tampered.zip"
            with zipfile.ZipFile(package, "r") as source, zipfile.ZipFile(rewritten, "w") as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename == "payload/synthetic.bin":
                        data = b"tampered"
                    target.writestr(info, data)
            with self.assertRaises(SteamPackageError):
                validate_steam_package(rewritten)

    def test_required_file_paths_cannot_escape_the_game_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "fixture-payload"
            payload.mkdir()
            (payload / "synthetic.bin").write_bytes(b"source")
            spec = root / "fixture-spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture localization patch",
                        "steam": {
                            "app_id": "123456",
                            "install_dir": "Fixture Game",
                            "required_files": ["../outside.bin"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(SteamPackageError):
                build_steam_package(payload, spec, root / "fixture.zip")

    @unittest.skipUnless(sys.platform == "win32", "PowerShell installer lifecycle requires Windows")
    def test_generated_installer_runs_one_synthetic_lifecycle(self) -> None:
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if not powershell:
            self.skipTest("PowerShell is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "sdk"
            work = root / "work"
            game = root / "fixture-game"
            sdk.mkdir()
            game.mkdir()
            (game / "fixture-game.exe").write_bytes(b"fixture-executable")
            initialize_workspace(work, sdk, git_choice="disabled", game_roots=[game])
            spec = work / "fixture-spec.json"
            payload = work / "fixture-payload"
            payload.mkdir()
            target_relative = Path("fixture-plugin") / "synthetic.bin"

            def make_release(release_id: str, data: bytes) -> tuple[Path, Path]:
                (payload / target_relative).parent.mkdir(parents=True, exist_ok=True)
                (payload / target_relative).write_bytes(data)
                spec.write_text(
                    json.dumps(
                        {
                            "patch_id": "fixture-localization",
                            "release_id": release_id,
                            "display_name": "Fixture localization patch",
                            "steam": {
                                "app_id": "123456",
                                "install_dir": "Fixture Game",
                                "required_files": ["fixture-game.exe"],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                package = work / f"fixture-{release_id}.zip"
                extracted = work / f"fixture-{release_id}"
                build_steam_package(payload, spec, package)
                with zipfile.ZipFile(package) as archive:
                    archive.extractall(extracted)
                return extracted / "Install-Translation-Patch.ps1", package

            def run(
                script: Path, mode: str, approval: Path | None = None
            ) -> subprocess.CompletedProcess[str]:
                command = [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-Mode",
                    mode,
                    "-GameRoot",
                    str(game),
                ]
                if approval:
                    command.extend(["-ApprovalFile", str(approval)])
                return subprocess.run(
                    command,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=False,
                )

            first, first_package = make_release("r1", b"release-one")
            denied = run(first, "Install")
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("E_RUNTIME_APPROVAL_REQUIRED", denied.stdout + denied.stderr)
            self.assertFalse((game / target_relative).exists())

            install_approval, _ = create_runtime_approval(
                work,
                action="install",
                artifact_path=first_package,
                target_path=game,
                user_confirmed=True,
            )
            installed = run(first, "Install", install_approval)
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            self.assertEqual((game / target_relative).read_bytes(), b"release-one")

            second, second_package = make_release("r2", b"release-two")
            install_only, _ = create_runtime_approval(
                work,
                action="install",
                artifact_path=second_package,
                target_path=game,
                user_confirmed=True,
            )
            denied_update = run(second, "Install", install_only)
            self.assertNotEqual(denied_update.returncode, 0)
            self.assertIn("E_RUNTIME_APPROVAL_ACTION_MISMATCH", denied_update.stdout + denied_update.stderr)
            self.assertEqual((game / target_relative).read_bytes(), b"release-one")

            update_approval, _ = create_runtime_approval(
                work,
                action="update",
                artifact_path=second_package,
                target_path=game,
                user_confirmed=True,
            )
            updated = run(second, "Install", update_approval)
            self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
            self.assertEqual((game / target_relative).read_bytes(), b"release-two")
            verified = run(second, "Verify")
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            remove_approval, _ = create_runtime_approval(
                work,
                action="remove",
                artifact_path=second_package,
                target_path=game,
                user_confirmed=True,
            )
            removed = run(second, "Remove", remove_approval)
            self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
            self.assertFalse((game / target_relative).exists())
            self.assertTrue((game / "fixture-game.exe").exists())


if __name__ == "__main__":
    unittest.main()
