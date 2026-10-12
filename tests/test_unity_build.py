from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_l10n.unity.build import build_compile_command, resolve_references


class UnityBuildTests(unittest.TestCase):
    def test_resolves_reference_names_and_builds_argument_array(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed = root / "Managed"
            managed.mkdir()
            assembly = managed / "Assembly-CSharp.dll"
            core = managed / "UnityEngine.CoreModule.dll"
            assembly.write_bytes(b"fixture")
            core.write_bytes(b"fixture")
            source = root / "FixtureBridge.cs"
            source.write_text("public sealed class FixtureBridge {}\n", encoding="utf-8")
            references = resolve_references(managed, ["Assembly-CSharp", "unityengine.coremodule.dll"])
            command = build_compile_command(
                "dotnet",
                root / "csc.dll",
                [source],
                references,
                root / "Fixture.Mod.dll",
                defines=["FIXTURE_LOADER"],
            )

        self.assertEqual(references, [assembly, core])
        self.assertEqual(command[0], "dotnet")
        self.assertIn("/deterministic+", command)
        self.assertIn("/define:FIXTURE_LOADER", command)
        self.assertTrue(any(item.startswith("/reference:") for item in command))
        self.assertNotIn("shell=True", command)

    def test_rejects_reference_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            managed = Path(directory)
            with self.assertRaises(ValueError):
                resolve_references(managed, ["fixture/Assembly-CSharp.dll"])

    def test_compile_command_rejects_invalid_define(self) -> None:
        with self.assertRaises(ValueError):
            build_compile_command(
                "dotnet",
                Path("fixture-csc.dll"),
                [Path("FixtureBridge.cs")],
                [],
                Path("Fixture.Mod.dll"),
                defines=["FIXTURE;INJECT"],
            )


if __name__ == "__main__":
    unittest.main()
