from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from game_l10n.unity.bundle import build_patch_bundle, verify_patch_bundle


class UnityBundleTests(unittest.TestCase):
    def _write_spec(self, root: Path, files: list[dict[str, str]]) -> Path:
        spec = root / "fixture-spec.json"
        spec.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "package": {"id": "fixture.localization", "version": "1.0.0"},
                    "install": {"root": "Mods", "directory": "FixtureLocalization"},
                    "files": files,
                }
            ),
            encoding="utf-8",
        )
        return spec

    def test_builds_deterministic_verified_sidecar_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build").mkdir()
            (root / "build" / "Fixture.Mod.dll").write_bytes(b"fixture-assembly")
            (root / "build" / "fixture-runtime.json").write_text("{}\n", encoding="utf-8")
            spec = self._write_spec(
                root,
                [
                    {"source": "build/Fixture.Mod.dll", "destination": "Fixture.Mod.dll", "role": "entry_assembly"},
                    {"source": "build/fixture-runtime.json", "destination": "fixture-runtime.json", "role": "localization_payload"},
                ],
            )
            first = root / "first.zip"
            second = root / "second.zip"
            first_result = build_patch_bundle(spec, root, first)
            second_result = build_patch_bundle(spec, root, second)
            verified = verify_patch_bundle(first, Path(first_result["manifest"]))
            first_bytes = first.read_bytes()
            second_bytes = second.read_bytes()
            with zipfile.ZipFile(first) as archive:
                names = archive.namelist()

        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(verified["files"], 2)
        self.assertEqual(
            names,
            [
                "Mods/FixtureLocalization/Fixture.Mod.dll",
                "Mods/FixtureLocalization/fixture-runtime.json",
            ],
        )

    def test_rejects_player_runtime_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Fixture.Mod.dll").write_bytes(b"fixture")
            (root / "Assembly-CSharp.dll").write_bytes(b"fixture-runtime")
            spec = self._write_spec(
                root,
                [
                    {"source": "Fixture.Mod.dll", "destination": "Fixture.Mod.dll", "role": "entry_assembly"},
                    {"source": "Assembly-CSharp.dll", "destination": "Assembly-CSharp.dll", "role": "dependency"},
                ],
            )
            with self.assertRaises(ValueError):
                build_patch_bundle(spec, root, root / "fixture.zip")

    def test_rejects_output_that_would_overwrite_a_staged_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "fixture.zip"
            output.write_bytes(b"fixture-assembly")
            spec = self._write_spec(
                root,
                [
                    {"source": "fixture.zip", "destination": "Fixture.Mod.dll", "role": "entry_assembly"},
                ],
            )
            with self.assertRaises(ValueError):
                build_patch_bundle(spec, root, output)

    def test_rejects_drive_relative_archive_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Fixture.Mod.dll").write_bytes(b"fixture")
            spec = root / "fixture-spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "package": {"id": "fixture.localization", "version": "1.0.0"},
                        "install": {"root": "C:Fixture", "directory": "FixtureLocalization"},
                        "files": [
                            {"source": "Fixture.Mod.dll", "destination": "Fixture.Mod.dll", "role": "entry_assembly"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                build_patch_bundle(spec, root, root / "fixture.zip")


if __name__ == "__main__":
    unittest.main()
