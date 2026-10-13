from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from importlib.resources import as_file, files
from pathlib import Path
from typing import Sequence

from ..errors import ToolExecutionError
from .build import build_compile_command, find_csharp_compiler, resolve_references


DEFAULT_BRIDGE_REFERENCES = (
    "BepInEx.Core",
    "BepInEx.Unity.IL2CPP",
    "0Harmony",
)
DEFAULT_FRAMEWORK_REFERENCES = (
    "System.Runtime",
    "System.Collections",
    "System.Linq",
    "System.Text.Json",
    "System.Security.Cryptography.Algorithms",
    "System.Security.Cryptography.Primitives",
    "System.Threading",
    "System.Threading.Thread",
    "System.Threading.ThreadPool",
    "System.Runtime.Extensions",
    "System.Runtime.Loader",
    "System.Memory",
    "System.Text.Encoding.Extensions",
    "System.Reflection",
    "System.Reflection.Extensions",
    "System.ObjectModel",
    "System.ComponentModel",
    "System.Console",
    "netstandard",
)
INTEROP_FRAMEWORK_REFERENCES = (
    "System.Runtime",
    "System.Collections",
    "System.Linq",
    "System.Runtime.Loader",
    "System.Console",
    "System.Runtime.Extensions",
    "System.Memory",
    "System.Text.Encoding.Extensions",
    "System.Reflection",
    "System.Text.RegularExpressions",
    "netstandard",
)
INTEROP_TOOL_REFERENCES = (
    "Cpp2IL.Core",
    "LibCpp2IL",
    "AssetRipper.Primitives",
    "AsmResolver",
    "AsmResolver.DotNet",
    "AsmResolver.PE",
    "Il2CppInterop.Generator",
    "Il2CppInterop.Common",
)
INTEROP_RUNTIME_REFERENCES = (
    "Microsoft.Extensions.Logging",
    "Microsoft.Extensions.Logging.Abstractions",
)


@dataclass(slots=True, frozen=True)
class BepInExIl2CppLayout:
    root: Path
    core_root: Path
    runtime_root: Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_bepinex_il2cpp(loader_root: Path, *, require_generator: bool = False) -> BepInExIl2CppLayout:
    root = loader_root.resolve()
    core_root = root / "BepInEx" / "core"
    runtime_root = root / "dotnet"
    required_core = [*DEFAULT_BRIDGE_REFERENCES]
    if require_generator:
        required_core.extend(INTEROP_TOOL_REFERENCES)
    missing = [name for name in required_core if not (core_root / f"{name}.dll").is_file()]
    if require_generator:
        missing.extend(
            name for name in INTEROP_RUNTIME_REFERENCES if not (runtime_root / f"{name}.dll").is_file()
        )
    if missing:
        raise FileNotFoundError("BepInEx IL2CPP files were not found: " + ", ".join(missing))
    return BepInExIl2CppLayout(root=root, core_root=core_root, runtime_root=runtime_root)


def find_reference_pack(compiler: Path, target_framework: str = "net6.0") -> Path:
    candidates: list[Path] = []
    for parent in compiler.resolve().parents:
        pack_root = parent / "packs" / "Microsoft.NETCore.App.Ref"
        if not pack_root.is_dir():
            continue
        candidates.extend(path / "ref" / target_framework for path in pack_root.iterdir() if path.is_dir())
    available = [path for path in candidates if (path / "System.Runtime.dll").is_file()]
    if not available:
        raise FileNotFoundError(f"{target_framework} reference pack was not found beside the selected .NET SDK")

    def version_key(path: Path) -> tuple[int, ...]:
        values = re.findall(r"\d+", path.parents[1].name)
        return tuple(int(value) for value in values)

    return max(available, key=version_key)


def _framework_references(reference_pack: Path, names: Sequence[str]) -> list[Path]:
    return resolve_references(reference_pack, names)


def _snapshot(paths: Sequence[Path]) -> dict[Path, tuple[int, int, str]]:
    return {
        path: (path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
        for path in paths
    }


def _require_unchanged(before: dict[Path, tuple[int, int, str]]) -> None:
    for path, expected in before.items():
        current = (path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
        if current != expected:
            raise ToolExecutionError(f"A read-only input changed during the IL2CPP operation: {path.name}")


def build_il2cpp_bridge(
    sources: Sequence[Path],
    loader_root: Path,
    output: Path,
    *,
    loader_reference_names: Sequence[str] = DEFAULT_BRIDGE_REFERENCES,
    framework_reference_names: Sequence[str] = DEFAULT_FRAMEWORK_REFERENCES,
    defines: Sequence[str] = (),
    reference_pack: Path | None = None,
    dotnet: str = "dotnet",
) -> dict[str, int | str]:
    layout = inspect_bepinex_il2cpp(loader_root)
    output = output.resolve()
    for source in sources:
        if not source.is_file() or source.suffix.casefold() != ".cs":
            raise FileNotFoundError(f"C# source was not found: {source}")
        if source.resolve() == output:
            raise ValueError("IL2CPP bridge output must not overwrite a source file")
    if output.is_relative_to(layout.root):
        raise ValueError("IL2CPP bridge output must not be written into the loader distribution")

    dotnet_path, compiler = find_csharp_compiler(dotnet)
    framework_root = reference_pack.resolve() if reference_pack else find_reference_pack(compiler)
    framework = _framework_references(framework_root, framework_reference_names)
    loader = resolve_references(layout.core_root, loader_reference_names)
    snapshots = _snapshot([*framework, *loader])
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(output.name + ".tmp")
    if temporary_output.exists():
        temporary_output.unlink()
    command = build_compile_command(
        dotnet_path,
        compiler,
        sources,
        [*framework, *loader],
        temporary_output,
        defines=defines,
        langversion="10.0",
        nullable=True,
    )
    try:
        completed = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, shell=False)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout)[-4000:]
            raise ToolExecutionError(f"IL2CPP bridge compilation failed ({completed.returncode}): {message}")
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise ToolExecutionError("IL2CPP bridge compiler did not create a non-empty assembly")
        _require_unchanged(snapshots)
        temporary_output.replace(output)
    finally:
        if temporary_output.exists():
            temporary_output.unlink()
    return {
        "sources": len(sources),
        "references": len(framework) + len(loader),
        "bytes": output.stat().st_size,
        "sha256": _sha256(output),
    }


def generate_il2cpp_interop(
    *,
    loader_root: Path,
    game_assembly: Path,
    metadata: Path,
    unity_base_libs: Path,
    unity_version: str,
    metadata_version: int,
    output: Path,
    reference_pack: Path | None = None,
    dotnet: str = "dotnet",
    timeout_seconds: int = 900,
) -> dict[str, object]:
    layout = inspect_bepinex_il2cpp(loader_root, require_generator=True)
    for source in (game_assembly, metadata):
        if not source.is_file():
            raise FileNotFoundError(source)
    if not unity_base_libs.is_dir():
        raise FileNotFoundError(unity_base_libs)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Interop output already exists: {output}")
    if output.is_relative_to(layout.root):
        raise ValueError("Interop output must not be written into the loader distribution")

    dotnet_path, compiler = find_csharp_compiler(dotnet)
    framework_root = reference_pack.resolve() if reference_pack else find_reference_pack(compiler)
    framework = _framework_references(framework_root, INTEROP_FRAMEWORK_REFERENCES)
    tools = resolve_references(layout.core_root, INTEROP_TOOL_REFERENCES)
    runtime = resolve_references(layout.runtime_root, INTEROP_RUNTIME_REFERENCES)
    protected = [game_assembly.resolve(), metadata.resolve(), *framework, *tools, *runtime]
    snapshots = _snapshot(protected)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="unity-il2cpp-", dir=output.parent) as directory:
        staging = Path(directory)
        runner = staging / "InteropRunner.dll"
        generated = staging / "generated"
        generated.mkdir()
        source_resource = files("game_l10n.unity").joinpath("resources/offline_interop.cs")
        with as_file(source_resource) as source:
            command = build_compile_command(
                dotnet_path,
                compiler,
                [source],
                [*framework, *tools, *runtime],
                runner,
                target="exe",
                langversion="10.0",
                nullable=True,
            )
            compiled = subprocess.run(command, text=True, encoding="utf-8", capture_output=True, shell=False)
        if compiled.returncode != 0:
            message = (compiled.stderr or compiled.stdout)[-4000:]
            raise ToolExecutionError(f"Interop runner compilation failed ({compiled.returncode}): {message}")
        runtime_config = {
            "runtimeOptions": {
                "tfm": "net6.0",
                "framework": {"name": "Microsoft.NETCore.App", "version": "6.0.0"},
            }
        }
        runner.with_suffix(".runtimeconfig.json").write_text(
            json.dumps(runtime_config, indent=2) + "\n", encoding="utf-8"
        )
        run = subprocess.run(
            [
                dotnet_path,
                str(runner),
                str(layout.core_root),
                str(layout.runtime_root),
                str(game_assembly.resolve()),
                str(metadata.resolve()),
                str(unity_base_libs.resolve()),
                str(generated),
                unity_version,
                str(metadata_version),
            ],
            cwd=staging,
            text=True,
            encoding="utf-8",
            capture_output=True,
            shell=False,
            timeout=timeout_seconds,
        )
        _require_unchanged(snapshots)
        if run.returncode != 0:
            message = (run.stderr or run.stdout)[-4000:]
            raise ToolExecutionError(f"Interop generation failed ({run.returncode}): {message}")
        assemblies = sorted(generated.glob("*.dll"))
        if not assemblies:
            raise ToolExecutionError("Interop generation produced no assemblies")
        phases = []
        for line in run.stdout.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("phase"):
                phases.append(item)
        generated.replace(output)
    return {
        "metadata_version": metadata_version,
        "assemblies": len(assemblies),
        "phases": phases,
        "inputs_unchanged": True,
    }
