from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from game_l10n.filesystem_package import (
    _fingerprint,
    build_filesystem_package,
    validate_filesystem_package,
)
from game_l10n.steam import (
    MANIFEST_NAME,
    STATUS_FILE_NAME,
    SteamPackageError,
    _manifest_fingerprint,
    build_steam_package,
    validate_steam_package,
)
from tests.test_steam_package import write_release_checkpoint


class PackagePathTests(unittest.TestCase):
    def _spec(self, root: Path, *, steam: bool, marker: str = "fixture-game.exe") -> Path:
        checkpoint = write_release_checkpoint(root, "r1")
        spec = {
            "patch_id": "fixture-localization",
            "release_id": "r1",
            "display_name": "Synthetic patch",
            "checkpoint_file": checkpoint.name,
        }
        if steam:
            spec["steam"] = {
                "app_id": "123456", "install_dir": "Synthetic Game", "required_files": [marker]
            }
        else:
            spec["target"] = {"required_files": [marker]}
        path = root / "spec.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_builders_reject_ambiguous_windows_marker_paths(self) -> None:
        invalid = (
            "/outside.bin", "\\outside.bin", "//server/share/file", "C:/file", "a//b",
            "a/./b", "a/../b", "a/", ".parr0t./state", "folder /file", "file.",
            "CON.txt", "nul", "COM1.bin", "LPT¹.txt", "CON .txt", "CONOUT$",
            "file:stream", "a?b", "a*b", 'a"b', "a<b", "a>b", "a|b", "a\x00b",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.mkdir()
            (payload / "fixture.dat").write_bytes(b"synthetic")
            for steam, build in ((False, build_filesystem_package), (True, build_steam_package)):
                for marker in invalid:
                    with self.subTest(steam=steam, marker=marker):
                        spec = self._spec(root, steam=steam, marker=marker)
                        with self.assertRaises(SteamPackageError):
                            build(payload, spec, root / "patch.zip")
                spec = self._spec(root, steam=steam, marker="\ud55c\uae00 \ud3f4\ub354\\fixture-game.exe")
                package = root / "valid.zip"
                build(payload, spec, package)
                with zipfile.ZipFile(package) as archive:
                    manifest = json.loads(archive.read(MANIFEST_NAME))
                contract = manifest["steam" if steam else "target"]
                self.assertEqual(contract["required_files"], ["\ud55c\uae00 \ud3f4\ub354/fixture-game.exe"])

    def test_archive_validators_reject_reserved_payload_roots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.mkdir()
            data = b"synthetic"
            (payload / "fixture.dat").write_bytes(data)
            for steam, build, validate, fingerprint in (
                (False, build_filesystem_package, validate_filesystem_package, _fingerprint),
                (True, build_steam_package, validate_steam_package, _manifest_fingerprint),
            ):
                original = root / "original.zip"
                build(payload, self._spec(root, steam=steam), original)
                with zipfile.ZipFile(original) as archive:
                    entries = {name: archive.read(name) for name in archive.namelist()}
                for relative in (STATUS_FILE_NAME, ".parr0t/fixture-localization.json"):
                    with self.subTest(steam=steam, relative=relative):
                        manifest = json.loads(entries[MANIFEST_NAME])
                        manifest["files"] = [{
                            "path": relative, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }]
                        manifest["artifact_fingerprint"] = fingerprint(manifest)
                        modified = dict(entries)
                        del modified["payload/fixture.dat"]
                        modified[f"payload/{relative}"] = data
                        modified[MANIFEST_NAME] = json.dumps(manifest).encode("utf-8")
                        package = root / "reserved.zip"
                        with zipfile.ZipFile(package, "w") as archive:
                            for name, content in modified.items():
                                archive.writestr(name, content)
                        with self.assertRaisesRegex(SteamPackageError, "reserved"):
                            validate(package)

    @unittest.skipUnless(os.name == "nt", "Windows junction required")
    def test_builders_reject_payload_junction_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.mkdir()
            (payload / "fixture.dat").write_bytes(b"synthetic")
            junction = payload / "cycle"
            subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(junction), str(payload)],
                capture_output=True, check=True,
            )
            try:
                for steam, build in ((False, build_filesystem_package), (True, build_steam_package)):
                    with self.subTest(steam=steam):
                        with self.assertRaisesRegex(SteamPackageError, "reparse"):
                            build(payload, self._spec(root, steam=steam), root / "patch.zip")
            finally:
                junction.rmdir()

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_installer_rejects_payload_and_state_junctions_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            (payload / "fixture-data").mkdir(parents=True)
            (payload / "fixture-data" / "fixture.dat").write_bytes(b"synthetic")
            package = root / "patch.zip"
            build_filesystem_package(payload, self._spec(root, steam=False), package)
            extracted = root / "extracted"
            with zipfile.ZipFile(package) as archive:
                archive.extractall(extracted)
            for linked in ("fixture-data", ".parr0t"):
                with self.subTest(linked=linked):
                    game = root / ("\uac8c\uc784 \ud3f4\ub354 " + linked)
                    game.mkdir()
                    (game / "fixture-game.exe").write_bytes(b"synthetic")
                    outside = root / ("outside " + linked)
                    outside.mkdir()
                    (outside / "sentinel").write_bytes(b"unchanged")
                    junction = game / linked
                    subprocess.run(
                        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
                        capture_output=True, check=True,
                    )
                    try:
                        result = subprocess.run(
                            [shutil.which("powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass",
                             "-File", str(extracted / "Install-Translation-Patch.ps1"),
                             "-GameRoot", str(game), "-Diagnostic"],
                            input="1\ny\n", text=True, encoding="utf-8", errors="replace",
                            capture_output=True, check=False,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("E_UNSAFE_PATH", result.stdout + result.stderr)
                        self.assertEqual(list(outside.iterdir()), [outside / "sentinel"])
                        self.assertEqual((outside / "sentinel").read_bytes(), b"unchanged")
                        self.assertFalse((game / STATUS_FILE_NAME).exists())
                    finally:
                        junction.rmdir()


    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows PowerShell required")
    def test_install_and_restore_preserve_unowned_staging_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "payload"
            payload.mkdir()
            (payload / "fixture.dat").write_bytes(b"patched")
            spec = self._spec(root, steam=False)
            value = json.loads(spec.read_text(encoding="utf-8"))
            value["replace_files"] = [{"path": "fixture.dat", "backup_path": "fixture-original.dat"}]
            spec.write_text(json.dumps(value), encoding="utf-8")
            package = root / "patch.zip"
            build_filesystem_package(payload, spec, package)
            extracted = root / "extracted"
            with zipfile.ZipFile(package) as archive:
                archive.extractall(extracted)
            game = root / "game"
            (game / ".parr0t").mkdir(parents=True)
            (game / "fixture-game.exe").write_bytes(b"synthetic")
            (game / "fixture.dat").write_bytes(b"original")
            unowned = [
                game / "fixture.dat.parr0t-new", game / "fixture.dat.parr0t-restore",
                game / "fixture-original.dat.parr0t-new", game / (STATUS_FILE_NAME + ".tmp"),
                game / ".parr0t" / "fixture-localization.json.tmp",
            ]
            for path in unowned:
                path.write_bytes(b"unowned")
            for choice, expected in (("1", b"patched"), ("2", b"original")):
                if choice == "2":
                    before = {path.relative_to(game): path.read_bytes() for path in game.rglob("*") if path.is_file()}
                    def quote(value: Path) -> str:
                        return "'" + str(value).replace("'", "''") + "'"
                    command = (
                        "$held = [IO.File]::Open(" + quote(game / "fixture.dat")
                        + ", [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read); "
                        + "& " + quote(extracted / "Install-Translation-Patch.ps1")
                        + " -GameRoot " + quote(game) + " -Diagnostic"
                    )
                    locked = subprocess.run(
                        [shutil.which("powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-Command", command], input="2\ny\n", text=True, encoding="utf-8",
                        errors="replace", capture_output=True, check=False,
                    )
                    self.assertNotEqual(locked.returncode, 0, locked.stdout + locked.stderr)
                    after = {path.relative_to(game): path.read_bytes() for path in game.rglob("*") if path.is_file()}
                    self.assertEqual(after, before)
                result = subprocess.run(
                    [shutil.which("powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(extracted / "Install-Translation-Patch.ps1"),
                     "-GameRoot", str(game), "-Diagnostic"],
                    input=choice + "\ny\n", text=True, encoding="utf-8", errors="replace",
                    capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((game / "fixture.dat").read_bytes(), expected)
                for path in unowned:
                    self.assertTrue(path.is_file(), str(path))
                    self.assertEqual(path.read_bytes(), b"unowned")


if __name__ == "__main__":
    unittest.main()
