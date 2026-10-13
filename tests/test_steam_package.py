from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from game_l10n.steam import MANIFEST_NAME, SteamPackageError, build_steam_package, validate_steam_package


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

        self.assertEqual(manifest["schema"], "parr0t.steam.patch/v1")
        self.assertEqual(manifest["files"][0]["path"], "fixture-plugin/synthetic.bin")
        self.assertIn("payload/fixture-plugin/synthetic.bin", names)
        self.assertIn("if (-not (Test-Path -LiteralPath $root -PathType Container))", installer)
        self.assertIn("E_UNOWNED_COLLISION", installer)
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
            game = root / "fixture-game"
            game.mkdir()
            (game / "fixture-game.exe").write_bytes(b"fixture-executable")
            spec = root / "fixture-spec.json"
            payload = root / "fixture-payload"
            payload.mkdir()
            target_relative = Path("fixture-plugin") / "synthetic.bin"

            def make_release(release_id: str, data: bytes) -> Path:
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
                package = root / f"fixture-{release_id}.zip"
                extracted = root / f"fixture-{release_id}"
                build_steam_package(payload, spec, package)
                with zipfile.ZipFile(package) as archive:
                    archive.extractall(extracted)
                return extracted / "Install-Translation-Patch.ps1"

            def run(script: Path, mode: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
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
                    ],
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=False,
                )

            first = make_release("r1", b"release-one")
            installed = run(first, "Install")
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            self.assertEqual((game / target_relative).read_bytes(), b"release-one")

            second = make_release("r2", b"release-two")
            updated = run(second, "Install")
            self.assertEqual(updated.returncode, 0, updated.stdout + updated.stderr)
            self.assertEqual((game / target_relative).read_bytes(), b"release-two")
            verified = run(second, "Verify")
            self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
            removed = run(second, "Remove")
            self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
            self.assertFalse((game / target_relative).exists())
            self.assertTrue((game / "fixture-game.exe").exists())


if __name__ == "__main__":
    unittest.main()
