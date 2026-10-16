from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any

from .release_checkpoint import (
    CHECKPOINT_NAME,
    CHECKPOINT_SCHEMA,
    ReleaseCheckpointError,
    canonical_checkpoint_bytes,
    validate_release_checkpoint,
)
from .steam import (
    INSTALLER_BAT,
    INSTALLER_CONTRACT,
    INSTALLER_PS1,
    INSTALLER_PUBLIC_ERROR_CODES,
    INSTALLER_SUCCESS_CODES,
    INSTALLER_UI_LOCALE,
    MANIFEST_NAME,
    README_NAME,
    STATUS_FILE_NAME,
    SteamPackageError,
    _custom_errors,
    _payload_files,
    _replace_files,
    _safe_relative,
    _sha256,
    _source_bindings,
    _string,
    _template,
    _zip_entry,
)


SCHEMA = "parr0t.filesystem.patch/v1"


def _fingerprint(manifest: dict[str, Any]) -> str:
    lines = [
        f"schema:{manifest['schema']}",
        f"patch_id:{manifest['patch_id']}",
        f"release_id:{manifest['release_id']}",
        f"display_name:{manifest['display_name']}",
        f"installer.contract:{manifest['installer']['contract']}",
        f"installer.ui_locale:{manifest['installer']['ui_locale']}",
        f"installer.status_file:{manifest['installer']['status_file']}",
        "installer.operations:" + ",".join(manifest["installer"]["operations"]),
        f"target.selection:{manifest['target']['selection']}",
        f"checkpoint.path:{manifest['checkpoint']['path']}",
        f"checkpoint.schema:{manifest['checkpoint']['schema']}",
        f"checkpoint.bytes:{manifest['checkpoint']['bytes']}",
        f"checkpoint.sha256:{manifest['checkpoint']['sha256']}",
    ]
    lines.extend(
        "installer.custom:{internal_code}\t{public_code}\t{message}\t{resolution}".format(**item)
        for item in manifest["installer"]["custom_errors"]
    )
    lines.extend(
        f"installer.replace:{item['path']}\t{item['backup_path']}"
        for item in manifest["installer"]["replace_files"]
    )
    lines.extend(f"target.required:{item}" for item in manifest["target"]["required_files"])
    lines.extend(
        "target.source_binding:{path}\t{bytes}\t{sha256}\t{error_code}".format(**item)
        for item in manifest["target"]["source_bindings"]
    )
    lines.extend(
        f"file:{item['path']}\t{item['bytes']}\t{item['sha256']}" for item in manifest["files"]
    )
    return _sha256(("\n".join(lines) + "\n").encode("utf-8"))


def _load_spec(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SteamPackageError("Filesystem package spec must be a JSON object")
    patch_id = _string(payload, "patch_id")
    release_id = _string(payload, "release_id")
    display_name = _string(payload, "display_name")
    if any(ord(character) < 32 for character in display_name):
        raise SteamPackageError("display_name must not contain control characters")
    safe_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
    if not safe_id.fullmatch(patch_id) or not safe_id.fullmatch(release_id):
        raise SteamPackageError("patch_id and release_id must be safe identifiers")
    target = payload.get("target")
    if not isinstance(target, dict):
        raise SteamPackageError("spec field 'target' must be an object")
    required_files = target.get("required_files", [])
    if not isinstance(required_files, list) or not all(isinstance(item, str) for item in required_files):
        raise SteamPackageError("target.required_files must be a list of relative paths")
    canonical_required = [
        _safe_relative(item, field="target.required_files") for item in required_files
    ]
    if len(canonical_required) != len({item.casefold() for item in canonical_required}):
        raise SteamPackageError("target.required_files must be case-insensitively unique")
    custom_errors = _custom_errors(payload)
    source_bindings = _source_bindings(target, custom_errors, field="target")
    if not canonical_required and not source_bindings:
        raise SteamPackageError(
            "filesystem packages require at least one required file or source binding"
        )
    return {
        "schema": SCHEMA,
        "patch_id": patch_id,
        "release_id": release_id,
        "display_name": display_name,
        "checkpoint_file": _safe_relative(_string(payload, "checkpoint_file"), field="checkpoint_file"),
        "installer": {
            "contract": INSTALLER_CONTRACT,
            "ui_locale": INSTALLER_UI_LOCALE,
            "status_file": STATUS_FILE_NAME,
            "operations": ["install", "restore", "verify"],
            "interactive_authorization": "menu_confirmation",
            "automation_authorization": "runtime_approval_v1",
            "custom_errors": custom_errors,
            "replace_files": _replace_files(payload, patch_id),
        },
        "target": {
            "selection": "explicit_game_root",
            "required_files": canonical_required,
            "source_bindings": source_bindings,
        },
    }


def _readme(manifest: dict[str, Any]) -> bytes:
    return (
        f"{manifest['display_name']}\n\n"
        f"패치 ID: {manifest['patch_id']}\n"
        f"배포 버전: {manifest['release_id']}\n\n"
        "1. 이 ZIP을 게임 폴더 밖의 원하는 위치에 모두 압축 해제합니다.\n"
        f"2. {INSTALLER_BAT}을 더블클릭합니다.\n"
        "3. 화면에서 설치, 복원 또는 설치 상태 확인을 선택합니다.\n"
        "4. 게임 폴더에서 실행하면 현재 폴더를 사용합니다. 다른 위치라면 절대/상대 경로를 입력하거나 설치기 창으로 끌어 놓습니다.\n\n"
        "이 패키지는 특정 스토어 또는 런처 설정을 변경하지 않습니다.\n"
        "설치기는 영수증에 기록된 파일만 관리하며, 비대화형 실행은 일회성 승인이 필요합니다.\n"
        f"설치 상태와 패치 ID는 게임 폴더의 {STATUS_FILE_NAME}에서 확인합니다.\n"
        f"{CHECKPOINT_NAME}은 향후 번역 수정과 게임 업데이트 대응을 위한 배포 체크포인트입니다.\n"
    ).encode("utf-8")


def build_filesystem_package(payload_root: Path, spec_path: Path, output_path: Path) -> dict[str, Any]:
    payload_root = payload_root.resolve()
    spec_path = spec_path.resolve()
    output_path = output_path.resolve()
    if not spec_path.is_file():
        raise SteamPackageError(f"Filesystem package spec does not exist: {spec_path}")
    try:
        output_path.relative_to(payload_root)
    except ValueError:
        pass
    else:
        raise SteamPackageError("Package output must not be inside the payload directory")
    manifest = _load_spec(spec_path)
    checkpoint_relative = manifest.pop("checkpoint_file")
    checkpoint_path = (spec_path.parent / checkpoint_relative).resolve()
    try:
        checkpoint_path.relative_to(spec_path.parent.resolve())
    except ValueError as error:
        raise SteamPackageError("checkpoint_file must stay beside or below the package spec") from error
    try:
        checkpoint, checkpoint_bytes = canonical_checkpoint_bytes(
            checkpoint_path,
            expected_patch_id=manifest["patch_id"],
            expected_release_id=manifest["release_id"],
        )
    except ReleaseCheckpointError as error:
        raise SteamPackageError(str(error)) from error
    manifest["checkpoint"] = {
        "path": CHECKPOINT_NAME,
        "schema": checkpoint["schema"],
        "bytes": len(checkpoint_bytes),
        "sha256": _sha256(checkpoint_bytes),
    }
    payload = _payload_files(payload_root)
    payload_paths = {relative.casefold() for relative, _ in payload}
    replacement_paths = {
        replacement["path"].casefold() for replacement in manifest["installer"]["replace_files"]
    }
    for replacement in manifest["installer"]["replace_files"]:
        if replacement["path"].casefold() not in payload_paths:
            raise SteamPackageError("every replacement path must have a corresponding payload file")
        if replacement["backup_path"].casefold() in payload_paths:
            raise SteamPackageError("a replacement backup path cannot be part of the payload")
    for binding in manifest["target"]["source_bindings"]:
        if binding["path"].casefold() in payload_paths - replacement_paths:
            raise SteamPackageError(
                "a source-bound payload path must be declared as an original-file replacement"
            )
    manifest["files"] = [
        {"path": relative, "bytes": len(data), "sha256": _sha256(data)}
        for relative, data in payload
    ]
    manifest["artifact_fingerprint"] = _fingerprint(manifest)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    entries = [
        (INSTALLER_BAT, _template(INSTALLER_BAT)),
        (INSTALLER_PS1, _template(INSTALLER_PS1)),
        (MANIFEST_NAME, manifest_bytes),
        (CHECKPOINT_NAME, checkpoint_bytes),
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
    result = validate_filesystem_package(output_path)
    result["output"] = str(output_path)
    return result


def validate_filesystem_package(package_path: Path) -> dict[str, Any]:
    package_path = package_path.resolve()
    if not package_path.is_file():
        raise SteamPackageError(f"Filesystem package does not exist: {package_path}")
    with zipfile.ZipFile(package_path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(name.casefold() for name in names)):
            raise SteamPackageError("Package contains duplicate or case-colliding paths")
        required = {INSTALLER_BAT, INSTALLER_PS1, MANIFEST_NAME, CHECKPOINT_NAME, README_NAME}
        if not required.issubset(names):
            raise SteamPackageError("Package is missing its fixed installer contract files")
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("schema") != SCHEMA or "steam" in manifest:
            raise SteamPackageError("Package filesystem manifest schema is not supported")
        if set(manifest) != {
            "schema",
            "patch_id",
            "release_id",
            "display_name",
            "installer",
            "target",
            "checkpoint",
            "files",
            "artifact_fingerprint",
        }:
            raise SteamPackageError("Package filesystem manifest shape is not supported")
        safe_id = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
        if not isinstance(manifest["patch_id"], str) or not safe_id.fullmatch(manifest["patch_id"]):
            raise SteamPackageError("Package patch ID is invalid")
        if not isinstance(manifest["release_id"], str) or not safe_id.fullmatch(manifest["release_id"]):
            raise SteamPackageError("Package release ID is invalid")
        if not isinstance(manifest["display_name"], str) or not manifest["display_name"].strip():
            raise SteamPackageError("Package display name is invalid")
        checkpoint_bytes = archive.read(CHECKPOINT_NAME)
        if manifest.get("checkpoint") != {
            "path": CHECKPOINT_NAME,
            "schema": CHECKPOINT_SCHEMA,
            "bytes": len(checkpoint_bytes),
            "sha256": _sha256(checkpoint_bytes),
        }:
            raise SteamPackageError("Package release checkpoint binding is invalid")
        checkpoint = json.loads(checkpoint_bytes.decode("utf-8"))
        try:
            validate_release_checkpoint(
                checkpoint,
                expected_patch_id=str(manifest.get("patch_id", "")),
                expected_release_id=str(manifest.get("release_id", "")),
            )
        except ReleaseCheckpointError as error:
            raise SteamPackageError(str(error)) from error
        if checkpoint_bytes != (json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"):
            raise SteamPackageError("Package release checkpoint is not canonical")
        installer = manifest.get("installer")
        if not isinstance(installer, dict):
            raise SteamPackageError("Package installer contract is invalid")
        custom = _custom_errors({"custom_errors": installer.get("custom_errors")})
        replacements = _replace_files(
            {"replace_files": installer.get("replace_files")}, str(manifest.get("patch_id", ""))
        )
        expected_installer = {
            "contract": INSTALLER_CONTRACT,
            "ui_locale": INSTALLER_UI_LOCALE,
            "status_file": STATUS_FILE_NAME,
            "operations": ["install", "restore", "verify"],
            "interactive_authorization": "menu_confirmation",
            "automation_authorization": "runtime_approval_v1",
            "custom_errors": custom,
            "replace_files": replacements,
        }
        if installer != expected_installer:
            raise SteamPackageError("Package installer contract is not canonical")
        target = manifest.get("target")
        if not isinstance(target, dict) or set(target) != {"selection", "required_files", "source_bindings"}:
            raise SteamPackageError("Package target contract is invalid")
        required_files = target["required_files"]
        if target["selection"] != "explicit_game_root" or not isinstance(required_files, list):
            raise SteamPackageError("Package target selection is invalid")
        canonical_required = [_safe_relative(str(item), field="target.required_files") for item in required_files]
        if (
            required_files != canonical_required
            or len(required_files) != len({item.casefold() for item in required_files})
        ):
            raise SteamPackageError("Package required-file registry is not canonical")
        bindings = _source_bindings(
            {"source_bindings": target["source_bindings"]}, custom, field="target"
        )
        if target["source_bindings"] != bindings:
            raise SteamPackageError("Package source binding registry is not canonical")
        if not required_files and not bindings:
            raise SteamPackageError("Package has no game-root identity marker")
        installer_bytes = archive.read(INSTALLER_PS1)
        installer_text = installer_bytes.decode("utf-8-sig")
        lowered = installer_text.casefold()
        if "localconfig.vdf" in lowered or "steam.exe -shutdown" in lowered:
            raise SteamPackageError("Installer must not edit Steam client configuration or shut down Steam")
        if set(re.findall(r"PARR0T-E\d{3}", installer_text)) != set(INSTALLER_PUBLIC_ERROR_CODES):
            raise SteamPackageError("Installer public error registry does not match the contract")
        if set(re.findall(r"PARR0T-S\d{3}", installer_text)) != set(INSTALLER_SUCCESS_CODES):
            raise SteamPackageError("Installer success-message registry does not match the contract")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise SteamPackageError("Package manifest contains no payload files")
        payload_paths = {str(item.get("path", "")).casefold() for item in files}
        replacement_paths = {item["path"].casefold() for item in replacements}
        for replacement in replacements:
            if replacement["path"].casefold() not in payload_paths:
                raise SteamPackageError("Replacement path is absent from the payload")
            if replacement["backup_path"].casefold() in payload_paths:
                raise SteamPackageError("Replacement backup path overlaps the payload")
        for binding in bindings:
            if binding["path"].casefold() in payload_paths - replacement_paths:
                raise SteamPackageError("A source-bound payload path is not an original-file replacement")
        if manifest.get("artifact_fingerprint") != _fingerprint(manifest):
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
        "checkpoint_schema": manifest["checkpoint"]["schema"],
        "artifact_fingerprint": manifest["artifact_fingerprint"],
        "installer_sha256": _sha256(installer_bytes),
        "files": len(files),
        "bytes": package_path.stat().st_size,
        "sha256": _sha256(package_path.read_bytes()),
    }
