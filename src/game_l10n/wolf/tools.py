from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Sequence

from ..errors import ToolExecutionError


def run_checked(command: Sequence[str | Path], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    argv = [str(part) for part in command]
    if not argv:
        raise ValueError("command must not be empty")
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            text=True,
            encoding="utf-8",
            capture_output=True,
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise ToolExecutionError(f"Could not start tool: {exc}") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout)[-4000:]
        raise ToolExecutionError(f"Tool exited {completed.returncode}: {message}")
    return completed


def decompile_runtime_scripts(
    wolfdawn_command: Sequence[str | Path],
    data_root: Path,
    output_root: Path,
) -> dict[str, Path]:
    """Decompile CommonEvent.dat and every map into WScript for deterministic audits."""
    output_root.mkdir(parents=True, exist_ok=True)
    scripts: dict[str, Path] = {}
    common_source = data_root / "BasicData" / "CommonEvent.dat"
    if not common_source.is_file():
        raise FileNotFoundError(f"Missing WOLF common event data: {common_source}")
    common_output = output_root / "CommonEvent.dat.wscript"
    run_checked([*wolfdawn_command, "decompile", common_source, "-o", common_output])
    scripts["CommonEvent.dat"] = common_output
    map_root = data_root / "MapData"
    if not map_root.is_dir():
        raise FileNotFoundError(f"Missing WOLF map data directory: {map_root}")
    for source in sorted(map_root.glob("*.mps")):
        output = output_root / f"{source.name}.wscript"
        run_checked([*wolfdawn_command, "decompile", source, "-o", output])
        scripts[source.name] = output
    return scripts


def pack_pro_archives(
    packer_command: Sequence[str | Path],
    built_data_root: Path,
    key_archive: Path,
    output_dir: Path,
    *,
    archive_names: Iterable[str] = ("BasicData", "MapData", "Text_Script"),
) -> list[Path]:
    """Pack all WOLF Pro replacement archives with the same Game.wolf-derived scheme.

    The external packer contract is ``pack <directory> <output.wolf> <Game.wolf>``.
    No packer binary or game-derived key is distributed with this SDK.
    """
    if not key_archive.is_file():
        raise FileNotFoundError(f"Missing WOLF Pro key archive: {key_archive}")
    names = tuple(archive_names)
    if not names:
        raise ValueError("archive_names must not be empty")
    for name in names:
        if not (built_data_root / name).is_dir():
            raise FileNotFoundError(f"Missing build directory: {built_data_root / name}")
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="game-l10n-wolf-pack-") as temporary:
        stage = Path(temporary)
        for name in names:
            archive = stage / f"{name}.wolf"
            run_checked([*packer_command, "pack", built_data_root / name, archive, key_archive])
            if not archive.is_file() or archive.stat().st_size == 0:
                raise ToolExecutionError(f"Packer did not create a non-empty archive: {archive.name}")
            destination = output_dir / archive.name
            shutil.copy2(archive, destination)
            results.append(destination)
    return results
