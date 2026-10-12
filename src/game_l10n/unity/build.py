from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Sequence

from ..errors import ToolExecutionError


DEFAULT_REFERENCES = (
    "mscorlib",
    "System",
    "System.Core",
    "netstandard",
    "Assembly-CSharp",
    "UnityEngine",
    "UnityEngine.CoreModule",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def find_csharp_compiler(dotnet: str = "dotnet") -> tuple[str, Path]:
    executable = shutil.which(dotnet)
    if not executable:
        raise FileNotFoundError(f".NET host was not found: {dotnet}")
    completed = subprocess.run(
        [executable, "--list-sdks"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise ToolExecutionError("Could not enumerate installed .NET SDKs")
    matches = [
        re.match(r"^([^ ]+) \[(.+)\]$", line.strip())
        for line in completed.stdout.splitlines()
    ]
    available = [match for match in matches if match]
    if not available:
        raise ToolExecutionError("A .NET SDK with the Roslyn compiler is required")
    selected = available[-1]
    compiler = Path(selected.group(2)) / selected.group(1) / "Roslyn" / "bincore" / "csc.dll"
    if not compiler.is_file():
        raise FileNotFoundError(compiler)
    return executable, compiler


def resolve_references(managed_root: Path, names: Iterable[str]) -> list[Path]:
    available = {path.name.casefold(): path for path in managed_root.glob("*.dll") if path.is_file()}
    resolved: list[Path] = []
    seen: set[str] = set()
    for value in names:
        name = value if value.casefold().endswith(".dll") else f"{value}.dll"
        if Path(name).name != name:
            raise ValueError(f"Reference must be an assembly filename, not a path: {value}")
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        path = available.get(key)
        if path is None:
            raise FileNotFoundError(f"Managed reference was not found: {name}")
        resolved.append(path)
    return resolved


def build_compile_command(
    dotnet: str,
    compiler: Path,
    sources: Sequence[Path],
    references: Sequence[Path],
    output: Path,
    *,
    defines: Sequence[str] = (),
) -> list[str]:
    if not sources:
        raise ValueError("At least one C# source file is required")
    command = [
        dotnet,
        str(compiler),
        "/nologo",
        "/target:library",
        "/nostdlib+",
        "/optimize+",
        "/deterministic+",
        "/langversion:9.0",
        f"/out:{output}",
    ]
    for symbol in defines:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
            raise ValueError(f"Invalid C# define: {symbol}")
        command.append(f"/define:{symbol}")
    command.extend(f"/reference:{path}" for path in references)
    command.extend(str(path) for path in sources)
    return command


def build_managed_mod(
    sources: Sequence[Path],
    managed_root: Path,
    output: Path,
    *,
    reference_names: Sequence[str],
    defines: Sequence[str] = (),
    dotnet: str = "dotnet",
) -> dict[str, int | str]:
    output = output.resolve()
    for source in sources:
        if not source.is_file() or source.suffix.casefold() != ".cs":
            raise FileNotFoundError(f"C# source was not found: {source}")
        if source.resolve() == output:
            raise ValueError("Managed build output must not overwrite a source file")
    try:
        output.relative_to(managed_root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("Managed build output must not be written into the installed player")
    references = resolve_references(managed_root, reference_names)
    snapshots = {
        path: (path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
        for path in references
    }
    dotnet_path, compiler = find_csharp_compiler(dotnet)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_name(output.name + ".tmp")
    if temporary_output.exists():
        temporary_output.unlink()
    command = build_compile_command(
        dotnet_path,
        compiler,
        sources,
        references,
        temporary_output,
        defines=defines,
    )
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            capture_output=True,
            shell=False,
            check=False,
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout)[-4000:]
            raise ToolExecutionError(f"Managed mod compilation failed ({completed.returncode}): {message}")
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise ToolExecutionError("Managed mod compiler did not create a non-empty assembly")
        for path, before in snapshots.items():
            after = (path.stat().st_size, path.stat().st_mtime_ns, _sha256(path))
            if after != before:
                raise ToolExecutionError(f"A read-only managed reference changed during compilation: {path.name}")
        temporary_output.replace(output)
    finally:
        if temporary_output.exists():
            temporary_output.unlink()
    return {
        "sources": len(sources),
        "references": len(references),
        "bytes": output.stat().st_size,
        "sha256": _sha256(output),
    }
