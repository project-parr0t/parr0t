from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..privacy import is_within


PACKAGE_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
PROTECTED_RUNTIME_ASSEMBLIES = {
    "assembly-csharp.dll",
    "assembly-csharp-firstpass.dll",
    "mscorlib.dll",
    "netstandard.dll",
    "system.dll",
    "system.core.dll",
    "unity.localization.dll",
    "unity.addressables.dll",
    "unity.resourcemanager.dll",
    "unity.textmeshpro.dll",
}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_archive_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    raw_parts = normalized.split("/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in raw_parts)
        or re.match(r"^[A-Za-z]:", raw_parts[0])
    ):
        raise ValueError(f"Unsafe patch archive path: {value}")
    return path


def _load_spec(spec_path: Path) -> dict[str, Any]:
    payload = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Patch specification requires schema_version 1")
    package = payload.get("package")
    install = payload.get("install")
    files = payload.get("files")
    if not isinstance(package, dict) or not isinstance(install, dict) or not isinstance(files, list):
        raise ValueError("Patch specification requires package, install, and files")
    for key in ("id", "version"):
        if not isinstance(package.get(key), str) or not PACKAGE_PART_RE.fullmatch(package[key]):
            raise ValueError(f"Invalid package {key}")
    for key in ("root", "directory"):
        if not isinstance(install.get(key), str):
            raise ValueError(f"Install {key} must be a relative path")
        _safe_archive_path(install[key])
    if not files:
        raise ValueError("Patch specification must contain at least one file")
    return payload


def _entry_path(spec: dict[str, Any], destination: str) -> str:
    root = _safe_archive_path(spec["install"]["root"])
    directory = _safe_archive_path(spec["install"]["directory"])
    leaf = _safe_archive_path(destination)
    return str(root / directory / leaf)


def _validate_runtime_filename(path: PurePosixPath) -> None:
    name = path.name.casefold()
    if name in PROTECTED_RUNTIME_ASSEMBLIES or name.startswith("unityengine."):
        raise ValueError(f"Patch must not redistribute an engine or game runtime assembly: {path.name}")


def build_patch_bundle(spec_path: Path, workspace: Path, output: Path) -> dict[str, Any]:
    workspace = workspace.resolve()
    spec_path = spec_path.resolve()
    output = output.resolve()
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    temporary_output = output.with_name(output.name + ".tmp")
    if not is_within(spec_path, workspace):
        raise ValueError("Patch specification must stay inside the agent workspace")
    if not is_within(output, workspace):
        raise ValueError("Patch output must stay inside the agent workspace")
    if output == spec_path or manifest_path == spec_path or temporary_output == spec_path:
        raise ValueError("Patch outputs must not overwrite the patch specification")
    spec = _load_spec(spec_path)
    entries: list[tuple[str, bytes, str]] = []
    destinations: set[str] = set()
    roles: set[str] = set()
    for index, item in enumerate(spec["files"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Patch file entry {index} must be an object")
        source_value = item.get("source")
        destination_value = item.get("destination")
        role = item.get("role")
        if not all(isinstance(value, str) and value for value in (source_value, destination_value, role)):
            raise ValueError(f"Patch file entry {index} is incomplete")
        source = (workspace / source_value).resolve()
        if not is_within(source, workspace) or not source.is_file():
            raise ValueError(f"Patch source must be a workspace file: {source_value}")
        if source in {output, manifest_path, temporary_output}:
            raise ValueError("Patch outputs must not overwrite a staged source file")
        archive_path = _entry_path(spec, destination_value)
        if archive_path in destinations:
            raise ValueError(f"Duplicate patch destination: {archive_path}")
        destinations.add(archive_path)
        roles.add(role)
        _validate_runtime_filename(PurePosixPath(source.name))
        _validate_runtime_filename(PurePosixPath(archive_path))
        entries.append((archive_path, source.read_bytes(), role))
    if "entry_assembly" not in roles:
        raise ValueError("Patch specification requires one entry_assembly role")

    output.parent.mkdir(parents=True, exist_ok=True)
    if temporary_output.exists():
        temporary_output.unlink()
    try:
        with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for archive_path, data, _ in sorted(entries):
                info = zipfile.ZipInfo(archive_path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        temporary_output.replace(output)
    finally:
        if temporary_output.exists():
            temporary_output.unlink()

    manifest = {
        "schema_version": 1,
        "package": spec["package"],
        "archive": output.name,
        "archive_sha256": _sha256_bytes(output.read_bytes()),
        "files": [
            {
                "path": path,
                "role": role,
                "bytes": len(data),
                "sha256": _sha256_bytes(data),
            }
            for path, data, role in sorted(entries)
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {
        "files": len(entries),
        "bytes": output.stat().st_size,
        "sha256": manifest["archive_sha256"],
        "manifest": str(manifest_path),
    }


def verify_patch_bundle(archive_path: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Patch verification manifest requires schema_version 1")
    expected_hash = manifest.get("archive_sha256")
    if not isinstance(expected_hash, str):
        raise ValueError("Patch verification manifest has no archive digest")
    actual_hash = _sha256_bytes(archive_path.read_bytes())
    if actual_hash != expected_hash:
        raise ValueError("Patch archive digest does not match its verification manifest")
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise ValueError("Patch verification manifest contains no files")
    expected: dict[str, dict[str, Any]] = {}
    for item in records:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("bytes"), int)
            or not isinstance(item.get("sha256"), str)
        ):
            raise ValueError("Patch verification manifest contains an invalid file record")
        path = str(_safe_archive_path(item["path"]))
        if path in expected:
            raise ValueError("Patch verification manifest contains duplicate paths")
        expected[path] = item
    with zipfile.ZipFile(archive_path, "r") as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)):
            raise ValueError("Patch archive contains duplicate paths")
        if set(names) != set(expected):
            raise ValueError("Patch archive file set does not match its verification manifest")
        total_uncompressed = 0
        for member in members:
            safe = _safe_archive_path(member.filename)
            _validate_runtime_filename(safe)
            if member.is_dir():
                raise ValueError("Patch archive must not contain directory entries")
            file_type = (member.external_attr >> 16) & 0o170000
            if file_type == 0o120000:
                raise ValueError("Patch archive must not contain symbolic links")
            total_uncompressed += member.file_size
            if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Patch archive exceeds the uncompressed size limit")
            if member.compress_size and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
                raise ValueError(f"Patch archive entry has an unsafe compression ratio: {member.filename}")
            data = archive.read(member)
            record = expected[member.filename]
            if len(data) != record.get("bytes") or _sha256_bytes(data) != record.get("sha256"):
                raise ValueError(f"Patch file digest mismatch: {member.filename}")
    return {
        "files": len(expected),
        "bytes": archive_path.stat().st_size,
        "uncompressed_bytes": total_uncompressed,
        "sha256": actual_hash,
    }
