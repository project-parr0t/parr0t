from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import GameL10nError

MANIFEST_NAME = "parr0t-patch-manifest.json"
INSTALLER_PS1 = "Install-Translation-Patch.ps1"
INSTALLER_BAT = "Install-Translation-Patch.bat"
README_NAME = "README.txt"
SCHEMA = "parr0t.steam.patch/v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_RESERVED_ROOTS = {MANIFEST_NAME.casefold(), INSTALLER_PS1.casefold(), INSTALLER_BAT.casefold(), README_NAME.casefold()}
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class SteamPackageError(GameL10nError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_relative(value: str, *, field: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise SteamPackageError(f"{field} must be a safe relative path: {value!r}")
    if ":" in normalized or any(not part for part in path.parts):
        raise SteamPackageError(f"{field} must not contain a drive or empty component: {value!r}")
    return path.as_posix()


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SteamPackageError(f"spec field {key!r} must be a non-empty string")
    return value.strip()


def _load_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SteamPackageError("Steam package spec must be a JSON object")
    patch_id = _string(payload, "patch_id")
    release_id = _string(payload, "release_id")
    display_name = _string(payload, "display_name")
    if not _SAFE_ID.fullmatch(patch_id):
        raise SteamPackageError("patch_id must contain only letters, digits, dot, underscore, and hyphen")
    if not _SAFE_ID.fullmatch(release_id):
        raise SteamPackageError("release_id must contain only letters, digits, dot, underscore, and hyphen")
    steam = payload.get("steam")
    if not isinstance(steam, dict):
        raise SteamPackageError("spec field 'steam' must be an object")
    app_id = _string(steam, "app_id")
    install_dir = _string(steam, "install_dir")
    if not app_id.isdecimal():
        raise SteamPackageError("steam.app_id must contain decimal digits only")
    if any(item in install_dir for item in ("/", "\\", ":")) or install_dir in {".", ".."}:
        raise SteamPackageError("steam.install_dir must be one directory name")
    required_files = steam.get("required_files", [])
    if not isinstance(required_files, list) or not all(isinstance(item, str) for item in required_files):
        raise SteamPackageError("steam.required_files must be a list of relative paths")
    return {
        "schema": SCHEMA,
        "patch_id": patch_id,
        "release_id": release_id,
        "display_name": display_name,
        "steam": {
            "app_id": app_id,
            "install_dir": install_dir,
            "required_files": [_safe_relative(item, field="steam.required_files") for item in required_files],
        },
    }


def _payload_files(root: Path) -> list[tuple[str, bytes]]:
    if not root.is_dir():
        raise SteamPackageError(f"Payload directory does not exist: {root}")
    result: list[tuple[str, bytes]] = []
    seen: set[str] = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if path.is_symlink():
            raise SteamPackageError(f"Payload symlinks are not supported: {path}")
        if not path.is_file():
            continue
        relative = _safe_relative(path.relative_to(root).as_posix(), field="payload path")
        folded = relative.casefold()
        if folded in seen:
            raise SteamPackageError(f"Payload contains case-insensitive duplicate paths: {relative}")
        if relative.split("/", 1)[0].casefold() in _RESERVED_ROOTS:
            raise SteamPackageError(f"Payload path uses a reserved package name: {relative}")
        seen.add(folded)
        result.append((relative, path.read_bytes()))
    if not result:
        raise SteamPackageError("Payload directory contains no files")
    return result


def _template(name: str) -> bytes:
    return resources.files("game_l10n").joinpath("templates", name).read_bytes()


def _readme(manifest: dict[str, Any]) -> bytes:
    text = (
        f"{manifest['display_name']}\n\n"
        f"Patch ID: {manifest['patch_id']}\n"
        f"Release ID: {manifest['release_id']}\n\n"
        "1. Extract this archive outside the game directory.\n"
        f"2. Run {INSTALLER_BAT}.\n"
        "3. Choose install, remove, or verify.\n\n"
        "The installer discovers Steam libraries and asks for the game directory when discovery fails.\n"
        "It manages only files owned by this patch receipt and does not modify unrelated game files.\n"
    )
    return text.encode("utf-8")


def _zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, _ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def build_steam_package(payload_root: Path, spec_path: Path, output_path: Path) -> dict[str, Any]:
    payload_root = payload_root.resolve()
    spec_path = spec_path.resolve()
    output_path = output_path.resolve()
    if not spec_path.is_file():
        raise SteamPackageError(f"Steam package spec does not exist: {spec_path}")
    try:
        output_path.relative_to(payload_root)
    except ValueError:
        pass
    else:
        raise SteamPackageError("Package output must not be inside the payload directory")

    manifest = _load_spec(spec_path)
    payload = _payload_files(payload_root)
    manifest["files"] = [
        {"path": relative, "bytes": len(data), "sha256": _sha256(data)}
        for relative, data in payload
    ]
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    entries = [
        (INSTALLER_BAT, _template(INSTALLER_BAT)),
        (INSTALLER_PS1, _template(INSTALLER_PS1)),
        (MANIFEST_NAME, manifest_bytes),
        (README_NAME, _readme(manifest)),
        *((f"payload/{relative}", data) for relative, data in payload),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, data in entries:
                _zip_entry(archive, name, data)
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    result = validate_steam_package(output_path)
    result["output"] = str(output_path)
    return result


def validate_steam_package(package_path: Path) -> dict[str, Any]:
    package_path = package_path.resolve()
    if not package_path.is_file():
        raise SteamPackageError(f"Steam package does not exist: {package_path}")
    with zipfile.ZipFile(package_path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(name.casefold() for name in names)):
            raise SteamPackageError("Package contains duplicate or case-colliding paths")
        required = {INSTALLER_BAT, INSTALLER_PS1, MANIFEST_NAME, README_NAME}
        if not required.issubset(names):
            raise SteamPackageError("Package is missing an installer, manifest, or README")
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("schema") != SCHEMA:
            raise SteamPackageError("Package manifest schema is not supported")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise SteamPackageError("Package manifest contains no payload files")
        expected = required | {f"payload/{item['path']}" for item in files}
        if set(names) != expected:
            raise SteamPackageError("Package entries do not match the manifest")
        for item in files:
            relative = _safe_relative(str(item.get("path", "")), field="manifest file path")
            data = archive.read(f"payload/{relative}")
            if len(data) != int(item.get("bytes", -1)) or _sha256(data) != item.get("sha256"):
                raise SteamPackageError(f"Payload digest mismatch: {relative}")
    return {
        "status": "valid",
        "schema": manifest["schema"],
        "patch_id": manifest["patch_id"],
        "release_id": manifest["release_id"],
        "files": len(files),
        "bytes": package_path.stat().st_size,
        "sha256": _sha256(package_path.read_bytes()),
    }
