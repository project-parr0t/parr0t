from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_l10n.unity.il2cpp import (
    DEFAULT_BRIDGE_REFERENCES,
    INTEROP_RUNTIME_REFERENCES,
    INTEROP_TOOL_REFERENCES,
    find_reference_pack,
    inspect_bepinex_il2cpp,
)


class UnityIl2CppTests(unittest.TestCase):
    def _write_loader(self, root: Path, *, generator: bool) -> None:
        core = root / "BepInEx" / "core"
        runtime = root / "dotnet"
        core.mkdir(parents=True)
        runtime.mkdir()
        names = list(DEFAULT_BRIDGE_REFERENCES)
        if generator:
            names.extend(INTEROP_TOOL_REFERENCES)
        for name in names:
            (core / f"{name}.dll").write_bytes(b"fixture")
        if generator:
            for name in INTEROP_RUNTIME_REFERENCES:
                (runtime / f"{name}.dll").write_bytes(b"fixture")

    def test_validates_bridge_and_generator_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_loader(root, generator=True)
            layout = inspect_bepinex_il2cpp(root, require_generator=True)

        self.assertEqual(layout.core_root.name, "core")
        self.assertEqual(layout.runtime_root.name, "dotnet")

    def test_reports_missing_generator_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_loader(root, generator=False)
            with self.assertRaises(FileNotFoundError):
                inspect_bepinex_il2cpp(root, require_generator=True)

    def test_finds_net6_reference_pack_beside_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compiler = root / "sdk" / "6.0.100" / "Roslyn" / "bincore" / "csc.dll"
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b"fixture")
            reference_pack = root / "packs" / "Microsoft.NETCore.App.Ref" / "6.0.1" / "ref" / "net6.0"
            reference_pack.mkdir(parents=True)
            (reference_pack / "System.Runtime.dll").write_bytes(b"fixture")

            selected = find_reference_pack(compiler)

        self.assertEqual(selected, reference_pack.resolve())


if __name__ == "__main__":
    unittest.main()
