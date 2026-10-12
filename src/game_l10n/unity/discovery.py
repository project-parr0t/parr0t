from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


UNITY_VERSION_RE = re.compile(rb"20\d{2}\.\d+\.\d+[abfp]\d+")


@dataclass(slots=True, frozen=True)
class UnityInstallation:
    game_root: Path
    data_root: Path
    managed_root: Path
    unity_version: str
    backend: str
    capabilities: dict[str, bool]
    loader_roots: tuple[str, ...]

    @property
    def unity_series(self) -> str:
        match = re.match(r"^(\d{4}\.\d+)", self.unity_version)
        return match.group(1) if match else "unknown"

    @property
    def supports_managed_mod_build(self) -> bool:
        return (
            self.backend == "mono"
            and self.unity_series == "2021.3"
            and self.capabilities.get("assembly_csharp", False)
            and self.capabilities.get("unity_core", False)
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["game_root"] = str(self.game_root)
        payload["data_root"] = str(self.data_root)
        payload["managed_root"] = str(self.managed_root)
        payload["unity_series"] = self.unity_series
        payload["supports_managed_mod_build"] = self.supports_managed_mod_build
        return payload


def _read_unity_version(data_root: Path) -> str:
    candidates = [
        data_root / "globalgamemanagers",
        data_root / "data.unity3d",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        with candidate.open("rb") as handle:
            sample = handle.read(8 * 1024 * 1024)
        match = UNITY_VERSION_RE.search(sample)
        if match:
            return match.group(0).decode("ascii")
    return "unknown"


def _find_data_root(game_root: Path) -> Path:
    candidates = sorted(
        path
        for path in game_root.iterdir()
        if path.is_dir() and path.name.casefold().endswith("_data")
    )
    if not candidates:
        raise FileNotFoundError("No Unity *_Data directory was found")
    if len(candidates) == 1:
        return candidates[0]

    executable_stems = {
        path.stem.casefold()
        for path in game_root.iterdir()
        if path.is_file() and path.suffix.casefold() == ".exe"
    }
    matched = [path for path in candidates if path.name[:-5].casefold() in executable_stems]
    if len(matched) == 1:
        return matched[0]
    raise ValueError("Multiple Unity data directories were found; use a root containing one player build")


def discover_unity_installation(game_root: Path) -> UnityInstallation:
    game_root = game_root.resolve()
    if not game_root.is_dir():
        raise FileNotFoundError(game_root)
    data_root = _find_data_root(game_root)
    managed_root = data_root / "Managed"
    il2cpp = (game_root / "GameAssembly.dll").is_file() and (data_root / "il2cpp_data").is_dir()
    capabilities = {
        "assembly_csharp": (managed_root / "Assembly-CSharp.dll").is_file(),
        "unity_core": (managed_root / "UnityEngine.CoreModule.dll").is_file(),
        "unity_localization": (managed_root / "Unity.Localization.dll").is_file(),
        "textmeshpro": (managed_root / "Unity.TextMeshPro.dll").is_file(),
        "addressables": (managed_root / "Unity.Addressables.dll").is_file(),
        "harmony": any(
            (managed_root / name).is_file()
            for name in ("0Harmony.dll", "HarmonyX.dll")
        ),
    }
    loader_candidates: tuple[tuple[str, Path | None], ...] = (
        ("Mods", game_root / "Mods"),
        ("BepInEx/plugins", game_root / "BepInEx" / "plugins"),
        ("MelonLoader/Mods", game_root / "Mods" if (game_root / "MelonLoader").exists() else None),
    )
    loader_roots = tuple(label for label, path in loader_candidates if path is not None and path.is_dir())
    return UnityInstallation(
        game_root=game_root,
        data_root=data_root,
        managed_root=managed_root,
        unity_version=_read_unity_version(data_root),
        backend="il2cpp" if il2cpp else "mono",
        capabilities=capabilities,
        loader_roots=loader_roots,
    )
