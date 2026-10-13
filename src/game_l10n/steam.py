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
STATUS_FILE_NAME = "parr0t-patch-status.txt"
SCHEMA = "parr0t.steam.patch/v3"
INSTALLER_CONTRACT = "parr0t.windows-installer/v1"
INSTALLER_UI_LOCALE = "ko-KR"
INSTALLER_PUBLIC_ERROR_CODES = frozenset(
    {
        "PARR0T-E001",
        "PARR0T-E002",
        "PARR0T-E005",
        "PARR0T-E006",
        "PARR0T-E007",
        "PARR0T-E008",
        "PARR0T-E009",
        "PARR0T-E010",
        "PARR0T-E011",
        "PARR0T-E012",
        "PARR0T-E099",
    }
)
INSTALLER_SUCCESS_CODES = frozenset(
    {"PARR0T-S001", "PARR0T-S002", "PARR0T-S003", "PARR0T-S004"}
)
INSTALLER_MENU_LABELS = (
    "1. 설치 또는 업데이트",
    "2. 복원",
    "3. 설치 상태 확인",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_CUSTOM_INTERNAL_CODE = re.compile(r"^E_CUSTOM_[A-Z0-9_]{1,72}$")
_CUSTOM_PUBLIC_CODE = re.compile(r"^PARR0T-[A-Z0-9][A-Z0-9-]{1,30}-E\d{3}$")
_RESERVED_ROOTS = {
    MANIFEST_NAME.casefold(),
    INSTALLER_PS1.casefold(),
    INSTALLER_BAT.casefold(),
    README_NAME.casefold(),
    STATUS_FILE_NAME.casefold(),
    ".parr0t",
}
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class SteamPackageError(GameL10nError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_fingerprint(manifest: dict[str, Any]) -> str:
    lines = [
        f"schema:{manifest['schema']}",
        f"patch_id:{manifest['patch_id']}",
        f"release_id:{manifest['release_id']}",
        f"display_name:{manifest['display_name']}",
        f"installer.contract:{manifest['installer']['contract']}",
        f"installer.ui_locale:{manifest['installer']['ui_locale']}",
        f"installer.status_file:{manifest['installer']['status_file']}",
        "installer.operations:" + ",".join(manifest["installer"]["operations"]),
        f"steam.app_id:{manifest['steam']['app_id']}",
        f"steam.install_dir:{manifest['steam']['install_dir']}",
    ]
    lines.extend(
        "installer.custom:{internal_code}\t{public_code}\t{message}\t{resolution}".format(**item)
        for item in manifest["installer"]["custom_errors"]
    )
    lines.extend(
        f"installer.replace:{item['path']}\t{item['backup_path']}"
        for item in manifest["installer"]["replace_files"]
    )
    lines.extend(f"steam.required:{item}" for item in manifest["steam"]["required_files"])
    lines.extend(
        f"file:{item['path']}\t{item['bytes']}\t{item['sha256']}" for item in manifest["files"]
    )
    return _sha256(("\n".join(lines) + "\n").encode("utf-8"))


def _safe_relative(value: str, *, field: str) -> str:
    normalized = value.replace("\\", "/").strip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise SteamPackageError(f"{field} must be a safe relative path: {value!r}")
    if ":" in normalized or any(not part for part in path.parts):
        raise SteamPackageError(f"{field} must not contain a drive or empty component: {value!r}")
    if any(ord(character) < 32 for character in normalized):
        raise SteamPackageError(f"{field} must not contain control characters: {value!r}")
    return path.as_posix()


def _string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SteamPackageError(f"spec field {key!r} must be a non-empty string")
    return value.strip()


def _plain_message(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SteamPackageError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > 240 or any(ord(character) < 32 for character in value):
        raise SteamPackageError(f"{field} must be one plain-text line of at most 240 characters")
    return value


def _custom_errors(payload: dict[str, Any]) -> list[dict[str, str]]:
    configured = payload.get("custom_errors", [])
    if not isinstance(configured, list):
        raise SteamPackageError("spec field 'custom_errors' must be a list")
    result: list[dict[str, str]] = []
    internal_seen: set[str] = set()
    public_seen: set[str] = set()
    for index, item in enumerate(configured):
        if not isinstance(item, dict):
            raise SteamPackageError(f"custom_errors[{index}] must be an object")
        internal = _plain_message(item.get("internal_code"), field=f"custom_errors[{index}].internal_code")
        public = _plain_message(item.get("public_code"), field=f"custom_errors[{index}].public_code")
        if not _CUSTOM_INTERNAL_CODE.fullmatch(internal):
            raise SteamPackageError("custom internal codes must use E_CUSTOM_<NAME>")
        if not _CUSTOM_PUBLIC_CODE.fullmatch(public):
            raise SteamPackageError("custom public codes must use PARR0T-<SCOPE>-E###")
        if internal in internal_seen or public in public_seen:
            raise SteamPackageError("custom error codes must be unique")
        internal_seen.add(internal)
        public_seen.add(public)
        result.append(
            {
                "internal_code": internal,
                "public_code": public,
                "message": _plain_message(item.get("message"), field=f"custom_errors[{index}].message"),
                "resolution": _plain_message(
                    item.get("resolution"), field=f"custom_errors[{index}].resolution"
                ),
            }
        )
    return sorted(result, key=lambda item: (item["public_code"], item["internal_code"]))


def _replace_files(payload: dict[str, Any], patch_id: str) -> list[dict[str, str]]:
    configured = payload.get("replace_files", [])
    if not isinstance(configured, list):
        raise SteamPackageError("spec field 'replace_files' must be a list")
    result: list[dict[str, str]] = []
    targets: set[str] = set()
    backups: set[str] = set()
    for index, item in enumerate(configured):
        if not isinstance(item, dict):
            raise SteamPackageError(f"replace_files[{index}] must be an object")
        path = _safe_relative(str(item.get("path", "")), field=f"replace_files[{index}].path")
        default_backup = f".parr0t/backups/{patch_id}/{path}"
        backup = _safe_relative(
            str(item.get("backup_path", default_backup)),
            field=f"replace_files[{index}].backup_path",
        )
        path_folded = path.casefold()
        backup_folded = backup.casefold()
        if path_folded == backup_folded:
            raise SteamPackageError("replacement path and backup path must differ")
        if path_folded in targets or backup_folded in backups:
            raise SteamPackageError("replacement and backup paths must be unique")
        if path_folded == STATUS_FILE_NAME.casefold() or backup_folded == STATUS_FILE_NAME.casefold():
            raise SteamPackageError("replacement entries cannot use the patch status filename")
        if backup_folded in {".parr0t", f".parr0t/{patch_id}.json".casefold()}:
            raise SteamPackageError("replacement backup path collides with installer state")
        targets.add(path_folded)
        backups.add(backup_folded)
        result.append({"path": path, "backup_path": backup})
    if targets & backups:
        raise SteamPackageError("a replacement backup path cannot also be a replacement target")
    return sorted(result, key=lambda item: item["path"].casefold())


def _load_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SteamPackageError("Steam package spec must be a JSON object")
    patch_id = _string(payload, "patch_id")
    release_id = _string(payload, "release_id")
    display_name = _string(payload, "display_name")
    if any(ord(character) < 32 for character in display_name):
        raise SteamPackageError("display_name must not contain control characters")
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
    if (
        any(item in install_dir for item in ("/", "\\", ":"))
        or install_dir in {".", ".."}
        or any(ord(character) < 32 for character in install_dir)
    ):
        raise SteamPackageError("steam.install_dir must be one directory name")
    required_files = steam.get("required_files", [])
    if not isinstance(required_files, list) or not all(isinstance(item, str) for item in required_files):
        raise SteamPackageError("steam.required_files must be a list of relative paths")
    return {
        "schema": SCHEMA,
        "patch_id": patch_id,
        "release_id": release_id,
        "display_name": display_name,
        "installer": {
            "contract": INSTALLER_CONTRACT,
            "ui_locale": INSTALLER_UI_LOCALE,
            "status_file": STATUS_FILE_NAME,
            "operations": ["install", "restore", "verify"],
            "interactive_authorization": "menu_confirmation",
            "automation_authorization": "runtime_approval_v1",
            "custom_errors": _custom_errors(payload),
            "replace_files": _replace_files(payload, patch_id),
        },
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
    data = resources.files("game_l10n").joinpath("templates", name).read_bytes()
    if name.casefold().endswith(".ps1") and not data.startswith(b"\xef\xbb\xbf"):
        return b"\xef\xbb\xbf" + data
    return data


def _readme(manifest: dict[str, Any]) -> bytes:
    text = (
        f"{manifest['display_name']}\n\n"
        f"패치 ID: {manifest['patch_id']}\n"
        f"배포 버전: {manifest['release_id']}\n\n"
        "1. 이 ZIP을 게임 폴더 밖의 원하는 위치에 모두 압축 해제합니다.\n"
        f"2. {INSTALLER_BAT}을 더블클릭합니다.\n"
        "3. 화면에서 설치, 복원 또는 설치 상태 확인을 선택합니다.\n\n"
        "설치기는 Steam 게임 위치를 자동으로 찾고, 찾지 못하면 게임 폴더를 물어봅니다.\n"
        "사용자가 메뉴에서 선택하고 최종 확인한 작업은 별도의 Parr0t 승인 파일이 필요하지 않습니다.\n"
        "에이전트나 스크립트의 비대화형 설치/복원만 별도의 일회성 승인이 필요합니다.\n"
        f"설치 상태와 패치 ID는 게임 폴더의 {STATUS_FILE_NAME}에서 확인합니다.\n"
        "설치기는 이 패치의 영수증에 기록된 파일만 관리하며 다른 게임 파일은 변경하지 않습니다.\n"
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
    payload_paths = {relative.casefold() for relative, _ in payload}
    for replacement in manifest["installer"]["replace_files"]:
        if replacement["path"].casefold() not in payload_paths:
            raise SteamPackageError("every replacement path must have a corresponding payload file")
        if replacement["backup_path"].casefold() in payload_paths:
            raise SteamPackageError("a replacement backup path cannot be part of the payload")
    manifest["files"] = [
        {"path": relative, "bytes": len(data), "sha256": _sha256(data)}
        for relative, data in payload
    ]
    manifest["artifact_fingerprint"] = _manifest_fingerprint(manifest)
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
        installer_bytes = archive.read(INSTALLER_PS1)
        installer_sha256 = _sha256(installer_bytes)
        if manifest.get("schema") != SCHEMA:
            raise SteamPackageError("Package manifest schema is not supported")
        installer_contract = manifest.get("installer")
        if not isinstance(installer_contract, dict):
            raise SteamPackageError("Package installer contract is not supported")
        custom_errors = installer_contract.get("custom_errors")
        expected_contract = {
            "contract": INSTALLER_CONTRACT,
            "ui_locale": INSTALLER_UI_LOCALE,
            "status_file": STATUS_FILE_NAME,
            "operations": ["install", "restore", "verify"],
            "interactive_authorization": "menu_confirmation",
            "automation_authorization": "runtime_approval_v1",
            "custom_errors": custom_errors,
            "replace_files": installer_contract.get("replace_files"),
        }
        if installer_contract != expected_contract:
            raise SteamPackageError("Package installer contract is not supported")
        # Reuse the spec validator so archive authors cannot smuggle malformed or
        # core-overriding custom error definitions into the data-driven registry.
        validated_custom = _custom_errors({"custom_errors": custom_errors})
        if custom_errors != validated_custom:
            raise SteamPackageError("Package custom error registry is not canonical")
        validated_replacements = _replace_files(
            {"replace_files": installer_contract.get("replace_files")}, str(manifest.get("patch_id", ""))
        )
        if installer_contract.get("replace_files") != validated_replacements:
            raise SteamPackageError("Package replacement registry is not canonical")
        installer_text = installer_bytes.decode("utf-8-sig")
        lowered_installer = installer_text.casefold()
        if "localconfig.vdf" in lowered_installer or "steam.exe -shutdown" in lowered_installer:
            raise SteamPackageError("Installer must not edit Steam client configuration or shut down Steam")
        if re.search(r"\b(?:stop-process|taskkill)\b[^\r\n]{0,100}\bsteam(?:\.exe)?\b", lowered_installer):
            raise SteamPackageError("Installer must not terminate Steam")
        public_errors = set(re.findall(r"PARR0T-E\d{3}", installer_text))
        success_codes = set(re.findall(r"PARR0T-S\d{3}", installer_text))
        if public_errors != set(INSTALLER_PUBLIC_ERROR_CODES):
            raise SteamPackageError("Installer public error registry does not match the contract")
        if success_codes != set(INSTALLER_SUCCESS_CODES):
            raise SteamPackageError("Installer success-message registry does not match the contract")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise SteamPackageError("Package manifest contains no payload files")
        payload_paths = {str(item.get("path", "")).casefold() for item in files}
        for replacement in validated_replacements:
            if replacement["path"].casefold() not in payload_paths:
                raise SteamPackageError("Replacement path is absent from the payload")
            if replacement["backup_path"].casefold() in payload_paths:
                raise SteamPackageError("Replacement backup path overlaps the payload")
        fingerprint = manifest.get("artifact_fingerprint")
        if not isinstance(fingerprint, str) or fingerprint != _manifest_fingerprint(manifest):
            raise SteamPackageError("Package artifact fingerprint is invalid")
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
        "installer_contract": manifest["installer"]["contract"],
        "artifact_fingerprint": manifest["artifact_fingerprint"],
        "installer_sha256": installer_sha256,
        "files": len(files),
        "bytes": package_path.stat().st_size,
        "sha256": _sha256(package_path.read_bytes()),
    }
