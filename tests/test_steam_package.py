from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from game_l10n.runtime_approval import create_runtime_approval
from game_l10n.release_checkpoint import CHECKPOINT_NAME, CHECKPOINT_SCHEMA
from game_l10n.steam import (
    INSTALLED_STATE_SCHEMA,
    INSTALLER_CONTRACT,
    INSTALLER_MENU_LABELS,
    INSTALLER_PUBLIC_ERROR_CODES,
    INSTALLER_SUCCESS_CODES,
    MANIFEST_NAME,
    SCHEMA,
    STATUS_FILE_NAME,
    SteamPackageError,
    build_steam_package,
    inspect_installed_state,
    validate_steam_package,
)
from game_l10n.workspace import initialize_workspace


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_release_checkpoint(
    root: Path,
    release_id: str,
    *,
    patch_id: str = "fixture-localization",
) -> Path:
    target = "TARGET_FIXTURE_TEXT"
    path = root / f"fixture-checkpoint-{release_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema": CHECKPOINT_SCHEMA,
                "patch_id": patch_id,
                "release_id": release_id,
                "sdk": {
                    "branch": "sdk-fixture-1.0",
                    "commit": "a" * 40,
                    "adapter_fingerprint": _digest("fixture-adapter"),
                },
                "game_builds": [
                    {
                        "id": "fixture-build-1",
                        "binding_fingerprint": _digest("fixture-binding"),
                    }
                ],
                "source_inventory": {
                    "method": "fixture-primary",
                    "fingerprint": _digest("fixture-inventory"),
                    "target_count": 1,
                },
                "context": {
                    "fingerprint": _digest("fixture-context"),
                    "review_policy_fingerprint": _digest("fixture-review-policy"),
                },
                "entries": [
                    {
                        "id": "fixture-target-1",
                        "source_sha256": _digest("SOURCE_FIXTURE_TEXT"),
                        "target": target,
                        "target_sha256": _digest(target),
                        "location_fingerprint": _digest("fixture-location"),
                        "dependency_fingerprint": _digest("fixture-dependency"),
                        "review_fingerprint": _digest("fixture-review"),
                        "status": "reviewed",
                        "dependencies": [],
                    }
                ],
                "locale_fallback": {
                    "status": "unsupported",
                    "mode": "none",
                    "fallback_locale": "",
                    "placeholder": "",
                    "verified": False,
                    "reason_code": "FIXTURE_RUNTIME_HAS_NO_LOCALE_FALLBACK",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class SteamPackageTests(unittest.TestCase):
    def test_installed_state_inspection_uses_only_receipt_owned_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game = Path(directory) / "fixture-game"
            game.mkdir()

            absent = inspect_installed_state(game)
            self.assertEqual(absent["schema"], INSTALLED_STATE_SCHEMA)
            self.assertEqual(absent["state"], "not_installed")

            installed = game / "fixture-data.bin"
            backup = game / ".parr0t" / "backups" / "fixture" / "fixture-launcher.exe"
            installed.write_text("PATCHED_FIXTURE", encoding="utf-8")
            backup.parent.mkdir(parents=True)
            backup.write_text("ORIGINAL_FIXTURE", encoding="utf-8")
            (game / STATUS_FILE_NAME).write_text(
                "contract=parr0t.patch-status/v1\n"
                "patch_id=fixture-localization\n"
                "release_id=r1\n",
                encoding="utf-8",
            )
            receipt = {
                "patch_id": "fixture-localization",
                "release_id": "r1",
                "files": [
                    {
                        "path": "fixture-data.bin",
                        "bytes": installed.stat().st_size,
                        "sha256": _digest("PATCHED_FIXTURE"),
                    }
                ],
                "originals": [
                    {
                        "path": "fixture-launcher.exe",
                        "backup_path": ".parr0t/backups/fixture/fixture-launcher.exe",
                        "bytes": backup.stat().st_size,
                        "sha256": _digest("ORIGINAL_FIXTURE"),
                    }
                ],
            }
            receipt_path = game / ".parr0t" / "fixture-localization.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            intact = inspect_installed_state(game)
            self.assertEqual(intact["state"], "installed_intact")
            self.assertEqual(intact["tracked_files"]["recorded"], 1)

            installed.write_text("EXTERNAL_CHANGE", encoding="utf-8")
            changed = inspect_installed_state(game)
            self.assertEqual(changed["state"], "possible_game_update")
            self.assertTrue(changed["possible_game_update"])
            self.assertIn("patched_file_hash_changed", changed["signals"])

            installed.write_text("PATCHED_FIXTURE", encoding="utf-8")
            backup.write_text("DAMAGED_BACKUP", encoding="utf-8")
            damaged = inspect_installed_state(game)
            self.assertEqual(damaged["state"], "damaged_state")
            self.assertEqual(damaged["recommended_action"], "stop_before_mutation_and_recover_state")

    def test_package_is_deterministic_and_manifest_driven(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "fixture-payload"
            (payload / "fixture-plugin").mkdir(parents=True)
            (payload / "fixture-plugin" / "synthetic.bin").write_bytes(b"synthetic-payload")
            spec = root / "fixture-spec.json"
            checkpoint = write_release_checkpoint(root, "r1")
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture localization patch",
                        "checkpoint_file": checkpoint.name,
                        "custom_errors": [
                            {
                                "internal_code": "E_CUSTOM_FIXTURE_BINDING",
                                "public_code": "PARR0T-FIXTURE-E001",
                                "message": "Synthetic binding mismatch.",
                                "resolution": "Use the matching synthetic fixture.",
                            }
                        ],
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
                installer = archive.read("Install-Translation-Patch.ps1").decode("utf-8-sig")
                names = set(archive.namelist())

        self.assertEqual(manifest["schema"], SCHEMA)
        self.assertEqual(manifest["checkpoint"]["schema"], CHECKPOINT_SCHEMA)
        self.assertEqual(result["checkpoint_schema"], CHECKPOINT_SCHEMA)
        self.assertEqual(manifest["installer"]["contract"], INSTALLER_CONTRACT)
        self.assertEqual(manifest["installer"]["status_file"], STATUS_FILE_NAME)
        self.assertEqual(manifest["installer"]["operations"], ["install", "restore", "verify"])
        self.assertEqual(manifest["installer"]["custom_errors"][0]["public_code"], "PARR0T-FIXTURE-E001")
        self.assertEqual(len(manifest["artifact_fingerprint"]), 64)
        self.assertEqual(len(result["installer_sha256"]), 64)
        self.assertEqual(manifest["files"][0]["path"], "fixture-plugin/synthetic.bin")
        self.assertIn("payload/fixture-plugin/synthetic.bin", names)
        self.assertIn("if (-not (Test-Path -LiteralPath $root -PathType Container))", installer)
        self.assertIn("E_UNOWNED_COLLISION", installer)
        self.assertIn("E_RUNTIME_APPROVAL_REQUIRED", installer)
        self.assertIn("E_RUNTIME_APPROVAL_INSTALLER_MISMATCH", installer)
        self.assertIn("E_CHECKPOINT_HASH", installer)
        self.assertIn("parr0t-patch-status.txt", installer)
        for label in INSTALLER_MENU_LABELS:
            self.assertIn(label, installer)
        self.assertNotIn("$matches =", installer.casefold())
        self.assertNotIn("localconfig.vdf", installer.casefold())
        self.assertNotRegex(installer.casefold(), r"(?:stop-process|taskkill).*steam")
        self.assertEqual(
            set(re.findall(r"PARR0T-E\d{3}", installer)),
            set(INSTALLER_PUBLIC_ERROR_CODES),
        )
        self.assertEqual(
            set(re.findall(r"PARR0T-S\d{3}", installer)),
            set(INSTALLER_SUCCESS_CODES),
        )
        self.assertEqual(
            {name for name in names if not name.startswith("payload/")},
            {
                "Install-Translation-Patch.bat",
                "Install-Translation-Patch.ps1",
                MANIFEST_NAME,
                CHECKPOINT_NAME,
                "README.txt",
            },
        )
        self.assertNotIn("previous-package", installer.casefold())
        self.assertNotIn("legacy fingerprint", installer.casefold())

    def test_validator_rejects_tampered_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "fixture-payload"
            payload.mkdir()
            (payload / "synthetic.bin").write_bytes(b"source")
            spec = root / "fixture-spec.json"
            checkpoint = write_release_checkpoint(root, "r1")
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture localization patch",
                        "checkpoint_file": checkpoint.name,
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

    def test_validator_rejects_tampered_release_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "fixture-payload"
            payload.mkdir()
            (payload / "synthetic.bin").write_bytes(b"source")
            checkpoint = write_release_checkpoint(root, "r1")
            spec = root / "fixture-spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture localization patch",
                        "checkpoint_file": checkpoint.name,
                        "steam": {"app_id": "123456", "install_dir": "Fixture Game"},
                    }
                ),
                encoding="utf-8",
            )
            package = root / "fixture.zip"
            build_steam_package(payload, spec, package)
            rewritten = root / "fixture-tampered-checkpoint.zip"
            with zipfile.ZipFile(package, "r") as source, zipfile.ZipFile(rewritten, "w") as target:
                for info in source.infolist():
                    data = source.read(info.filename)
                    if info.filename == CHECKPOINT_NAME:
                        data = data.replace(b"TARGET_FIXTURE_TEXT", b"TARGET_FIXTURE_EDIT")
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
            checkpoint = write_release_checkpoint(root, "r1")
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture localization patch",
                        "checkpoint_file": checkpoint.name,
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

    def test_custom_errors_require_a_separate_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "fixture-payload"
            payload.mkdir()
            (payload / "synthetic.bin").write_bytes(b"source")
            spec = root / "fixture-spec.json"
            checkpoint = write_release_checkpoint(root, "r1")
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture localization patch",
                        "checkpoint_file": checkpoint.name,
                        "custom_errors": [
                            {
                                "internal_code": "E_PAYLOAD_HASH",
                                "public_code": "PARR0T-E001",
                                "message": "Synthetic message.",
                                "resolution": "Synthetic resolution.",
                            }
                        ],
                        "steam": {"app_id": "123456", "install_dir": "Fixture Game"},
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
            replacement_relative = Path("fixture-launcher.exe")
            replacement_backup = Path("fixture-launcher.parr0t-original.exe")
            (game / replacement_relative).write_bytes(b"original-launcher")
            initialize_workspace(work, sdk, git_choice="disabled", game_roots=[game])
            spec = work / "fixture-spec.json"
            payload = work / "fixture-payload"
            payload.mkdir()
            target_relative = Path("fixture-plugin") / "synthetic.bin"

            def make_release(release_id: str, data: bytes) -> tuple[Path, Path]:
                (payload / target_relative).parent.mkdir(parents=True, exist_ok=True)
                (payload / target_relative).write_bytes(data)
                (payload / replacement_relative).write_bytes(b"wrapper-" + data)
                checkpoint = write_release_checkpoint(work, release_id)
                spec.write_text(
                    json.dumps(
                        {
                            "patch_id": "fixture-localization",
                            "release_id": release_id,
                            "display_name": "Fixture localization patch",
                            "checkpoint_file": checkpoint.name,
                            "replace_files": [
                                {
                                    "path": replacement_relative.as_posix(),
                                    "backup_path": replacement_backup.as_posix(),
                                }
                            ],
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

            def run_menu(script: Path, answers: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(script),
                        "-GameRoot",
                        str(game),
                    ],
                    input=answers,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=False,
                )

            first, first_package = make_release("r1", b"release-one")
            denied = run(first, "Install")
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("PARR0T-E005", denied.stdout + denied.stderr)
            self.assertNotIn("E_RUNTIME_APPROVAL_REQUIRED", denied.stdout + denied.stderr)
            self.assertFalse((game / target_relative).exists())

            interactive_install = run_menu(first, "1\ny\n")
            self.assertEqual(interactive_install.returncode, 0, interactive_install.stdout + interactive_install.stderr)
            self.assertEqual((game / target_relative).read_bytes(), b"release-one")
            self.assertEqual((game / replacement_relative).read_bytes(), b"wrapper-release-one")
            self.assertEqual((game / replacement_backup).read_bytes(), b"original-launcher")
            status = (game / STATUS_FILE_NAME).read_text(encoding="utf-8-sig")
            self.assertIn("patch_id=fixture-localization", status)
            self.assertFalse((game / CHECKPOINT_NAME).exists())
            (game / STATUS_FILE_NAME).write_text(
                status.replace("patch_id=fixture-localization", "patch_id=fixture-other"),
                encoding="utf-8",
            )
            wrong_patch = run_menu(first, "3\n")
            self.assertNotEqual(wrong_patch.returncode, 0)
            self.assertIn("PARR0T-E010", wrong_patch.stdout + wrong_patch.stderr)
            (game / STATUS_FILE_NAME).write_text(status, encoding="utf-8-sig")
            interactive_verify = run_menu(first, "3\n")
            self.assertEqual(interactive_verify.returncode, 0, interactive_verify.stdout + interactive_verify.stderr)
            interactive_restore = run_menu(first, "2\ny\n")
            self.assertEqual(interactive_restore.returncode, 0, interactive_restore.stdout + interactive_restore.stderr)
            self.assertFalse((game / target_relative).exists())
            self.assertEqual((game / replacement_relative).read_bytes(), b"original-launcher")
            self.assertFalse((game / replacement_backup).exists())
            self.assertFalse((game / STATUS_FILE_NAME).exists())

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
            self.assertEqual((game / replacement_relative).read_bytes(), b"wrapper-release-one")

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
            self.assertIn("PARR0T-E006", denied_update.stdout + denied_update.stderr)
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
            self.assertEqual((game / replacement_relative).read_bytes(), b"wrapper-release-two")
            self.assertEqual((game / replacement_backup).read_bytes(), b"original-launcher")
            verified = run(second, "Verify")
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            remove_approval, _ = create_runtime_approval(
                work,
                action="remove",
                artifact_path=second_package,
                target_path=game,
                user_confirmed=True,
            )
            removed = run(second, "Restore", remove_approval)
            self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
            self.assertFalse((game / target_relative).exists())
            self.assertEqual((game / replacement_relative).read_bytes(), b"original-launcher")
            self.assertFalse((game / replacement_backup).exists())
            self.assertFalse((game / STATUS_FILE_NAME).exists())
            self.assertTrue((game / "fixture-game.exe").exists())


if __name__ == "__main__":
    unittest.main()
