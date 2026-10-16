from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from game_l10n.filesystem_package import SCHEMA, build_filesystem_package, validate_filesystem_package
from game_l10n.release_checkpoint import CHECKPOINT_SCHEMA
from game_l10n.steam import MANIFEST_NAME, STATUS_FILE_NAME


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _checkpoint(root: Path) -> Path:
    target = "SYNTHETIC_TARGET_TEXT"
    path = root / "checkpoint.json"
    path.write_text(
        json.dumps(
            {
                "schema": CHECKPOINT_SCHEMA,
                "patch_id": "fixture-localization",
                "release_id": "r1",
                "sdk": {
                    "branch": "sdk-fixture-1.0",
                    "commit": "a" * 40,
                    "adapter_fingerprint": _digest("adapter"),
                },
                "game_builds": [{"id": "fixture-build", "binding_fingerprint": _digest("binding")}],
                "source_inventory": {
                    "method": "fixture",
                    "fingerprint": _digest("inventory"),
                    "target_count": 1,
                },
                "context": {
                    "fingerprint": _digest("context"),
                    "review_policy_fingerprint": _digest("review"),
                },
                "entries": [
                    {
                        "id": "target-1",
                        "source_sha256": _digest("source"),
                        "target": target,
                        "target_sha256": _digest(target),
                        "location_fingerprint": _digest("location"),
                        "dependency_fingerprint": _digest("dependency"),
                        "review_fingerprint": _digest("review-entry"),
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
                    "reason_code": "FIXTURE_NO_RUNTIME_FALLBACK",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class FilesystemPackageTests(unittest.TestCase):
    def test_launcher_neutral_package_uses_explicit_game_root_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.mkdir()
            (payload / "localization.dat").write_bytes(b"fixture")
            checkpoint = _checkpoint(root)
            spec = root / "spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "patch_id": "fixture-localization",
                        "release_id": "r1",
                        "display_name": "Fixture patch",
                        "checkpoint_file": checkpoint.name,
                        "target": {"required_files": ["fixture-game.exe"]},
                    }
                ),
                encoding="utf-8",
            )
            first = root / "first.zip"
            second = root / "second.zip"
            result = build_filesystem_package(payload, spec, first)
            build_filesystem_package(payload, spec, second)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(result["schema"], SCHEMA)
            self.assertEqual(validate_filesystem_package(first)["status"], "valid")
            with zipfile.ZipFile(first) as archive:
                manifest = json.loads(archive.read(MANIFEST_NAME))
                installer = archive.read("Install-Translation-Patch.ps1").decode("utf-8-sig")
            self.assertNotIn("steam", manifest)
            self.assertEqual(manifest["target"]["selection"], "explicit_game_root")
            self.assertEqual(manifest["installer"]["status_file"], STATUS_FILE_NAME)
            self.assertIn("target.source_binding:", installer)
            self.assertIn("Get-TargetContract", installer)

            powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
            if powershell:
                extracted = root / "extracted"
                game = root / "game"
                game.mkdir()
                (game / "fixture-game.exe").write_bytes(b"fixture")
                with zipfile.ZipFile(first) as archive:
                    archive.extractall(extracted)
                script = extracted / "Install-Translation-Patch.ps1"

                def run_menu(
                    answers: str,
                    *,
                    game_root: str | None,
                    cwd: Path,
                ) -> subprocess.CompletedProcess[str]:
                    def quote(value: str) -> str:
                        return "'" + value.replace("'", "''") + "'"

                    invocation = (
                        f"Set-Location -LiteralPath {quote(str(cwd))}; "
                        f"& {quote(str(script))}"
                    )
                    if game_root is not None:
                        invocation += f" -GameRoot {quote(game_root)}"
                    invocation += "; exit $LASTEXITCODE"
                    command = [
                            powershell,
                            "-NoProfile",
                            "-ExecutionPolicy",
                            "Bypass",
                            "-Command",
                            invocation,
                        ]
                    return subprocess.run(
                        command,
                        input=answers,
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        check=False,
                    )

                installed = run_menu("1\ny\n", game_root=None, cwd=game)
                self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
                self.assertTrue((game / "localization.dat").is_file())
                verified = run_menu("3\n", game_root="game", cwd=root)
                self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
                restored = run_menu("2\ny\n", game_root=None, cwd=game)
                self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
                self.assertFalse((game / "localization.dat").exists())


if __name__ == "__main__":
    unittest.main()
