from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from game_l10n.unity.discovery import discover_unity_installation


class UnityDiscoveryTests(unittest.TestCase):
    def test_discovers_supported_2021_3_mono_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "SyntheticPlayer_Data"
            managed = data / "Managed"
            managed.mkdir(parents=True)
            (root / "SyntheticPlayer.exe").write_bytes(b"fixture")
            (data / "globalgamemanagers").write_bytes(b"fixture\x002021.3.45f2\x00")
            for name in (
                "Assembly-CSharp.dll",
                "UnityEngine.CoreModule.dll",
                "Unity.Localization.dll",
                "Unity.TextMeshPro.dll",
                "Unity.Addressables.dll",
                "0Harmony.dll",
            ):
                (managed / name).write_bytes(b"fixture")
            (root / "BepInEx" / "plugins").mkdir(parents=True)

            result = discover_unity_installation(root)

        self.assertEqual(result.unity_version, "2021.3.45f2")
        self.assertEqual(result.unity_series, "2021.3")
        self.assertEqual(result.backend, "mono")
        self.assertTrue(result.supports_managed_mod_build)
        self.assertTrue(result.capabilities["unity_localization"])
        self.assertEqual(result.loader_roots, ("BepInEx/plugins",))

    def test_il2cpp_is_discovered_but_not_marked_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "FixturePlayer_Data"
            (data / "il2cpp_data").mkdir(parents=True)
            (root / "GameAssembly.dll").write_bytes(b"fixture")
            (data / "globalgamemanagers").write_bytes(b"2021.3.1f1")

            result = discover_unity_installation(root)

        self.assertEqual(result.backend, "il2cpp")
        self.assertFalse(result.supports_managed_mod_build)


if __name__ == "__main__":
    unittest.main()
