from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import GameL10nError
from .release_checkpoint import CHECKPOINT_NAME, CHECKPOINT_SCHEMA, canonical_checkpoint_bytes
from .steam import MANIFEST_NAME, validate_steam_package


INSTANCE_RELEASE_SCHEMA = "parr0t.instance-release/v2"
INSTANCE_RELEASE_NAME = "parr0t-instance-release.json"
GATE_SCHEMA = "parr0t.instance-release-gate/v2"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_PATCH_VERSION = re.compile(r"^r[1-9][0-9]*$")
_LEGAL_NAMES = re.compile(
    r"^(?:licen[cs]e|copying|notice|authors?|credits?)(?:[._-].*)?$",
    re.IGNORECASE,
)
_FORBIDDEN_SUFFIXES = {
    ".7z",
    ".apk",
    ".bin",
    ".cab",
    ".dll",
    ".dmg",
    ".exe",
    ".gz",
    ".iso",
    ".msi",
    ".pak",
    ".rar",
    ".tar",
    ".zip",
}
_FORBIDDEN_PARTS = {
    ".game-l10n",
    "artifacts",
    "build",
    "cache",
    "game-data",
    "game_data",
    "logs",
    "outputs",
    "payload",
    "translations",
}
_GLUE_SUFFIXES = {
    ".cs",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".xml",
    ".yaml",
    ".yml",
}
_MAX_GLUE_BYTES = 2 * 1024 * 1024


class InstanceReleaseError(GameL10nError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _required_keys(payload: dict[str, Any], expected: set[str], *, scope: str) -> None:
    missing = expected - set(payload)
    extra = set(payload) - expected
    if missing or extra:
        raise InstanceReleaseError(
            f"{scope} fields do not match the instance release contract "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstanceReleaseError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, *, field: str) -> str:
    value = _text(value, field=field)
    if not _SHA256.fullmatch(value):
        raise InstanceReleaseError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _commit(value: Any, *, field: str, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    value = _text(value, field=field)
    if not _GIT_COMMIT.fullmatch(value):
        raise InstanceReleaseError(f"{field} must be a Git commit ID")
    return value


def _safe_relative(value: Any, *, field: str) -> str:
    value = _text(value, field=field).replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InstanceReleaseError(f"{field} must be a safe relative path")
    if any(ord(character) < 32 for character in value):
        raise InstanceReleaseError(f"{field} contains control characters")
    return path.as_posix()


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if check and process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip() or "Git command failed"
        raise InstanceReleaseError(message)
    return process.stdout.strip()


def _filename_component(value: str, *, field: str) -> str:
    component = re.sub(r"[^\w.-]+", "-", value.strip(), flags=re.UNICODE)
    component = re.sub(r"-+", "-", component).strip("._-")
    if not component:
        raise InstanceReleaseError(f"{field} cannot form a package name")
    return component


def _expected_title(
    display_name: str,
    language: str,
    patch_version: str,
    versions: list[str],
    display_type: str,
) -> str:
    label = "Game Build" if display_type == "build" else "Game Version"
    if len(versions) != 1:
        label += "s"
    return f"{display_name}-{language}-{patch_version} ({label} {', '.join(versions)})"


def _validate_metadata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InstanceReleaseError("Instance release metadata must be a JSON object")
    _required_keys(
        payload,
        {
            "schema",
            "patch_id",
            "release_id",
            "display_name",
            "language",
            "patch_version",
            "tag",
            "release_title",
            "target",
            "supported_game_builds",
            "commits",
            "asset",
            "checkpoint",
            "readme",
            "glue_paths",
        },
        scope="instance release metadata",
    )
    if payload["schema"] != INSTANCE_RELEASE_SCHEMA:
        raise InstanceReleaseError("Instance release metadata schema is unsupported")
    for field in (
        "patch_id",
        "release_id",
        "display_name",
        "language",
        "tag",
        "release_title",
    ):
        _text(payload[field], field=field)
    patch_version = _text(payload["patch_version"], field="patch_version")
    if not _PATCH_VERSION.fullmatch(patch_version):
        raise InstanceReleaseError("patch_version must be r followed by a positive revision")
    if payload["release_id"] != patch_version:
        raise InstanceReleaseError("release_id must exactly match patch_version")
    if payload["tag"] != patch_version:
        raise InstanceReleaseError("tag must exactly match patch_version")

    target = payload["target"]
    if not isinstance(target, dict):
        raise InstanceReleaseError("target must be an object")
    _required_keys(target, {"os", "architecture", "prerequisites"}, scope="target")
    _text(target["os"], field="target.os")
    _text(target["architecture"], field="target.architecture")
    if not isinstance(target["prerequisites"], list) or not all(
        isinstance(item, str) and item.strip() for item in target["prerequisites"]
    ):
        raise InstanceReleaseError("target.prerequisites must be non-empty strings")

    builds = payload["supported_game_builds"]
    if not isinstance(builds, list) or not builds:
        raise InstanceReleaseError("supported_game_builds must list every verified build")
    build_ids: set[str] = set()
    versions: list[str] = []
    display_types: set[str] = set()
    for index, build in enumerate(builds):
        if not isinstance(build, dict):
            raise InstanceReleaseError(f"supported_game_builds[{index}] must be an object")
        _required_keys(
            build,
            {"id", "display_version", "display_type"},
            scope=f"supported_game_builds[{index}]",
        )
        build_id = _text(build["id"], field=f"supported_game_builds[{index}].id")
        version = _text(
            build["display_version"], field=f"supported_game_builds[{index}].display_version"
        )
        display_type = _text(
            build["display_type"], field=f"supported_game_builds[{index}].display_type"
        )
        if display_type not in {"build", "version"}:
            raise InstanceReleaseError(
                f"supported_game_builds[{index}].display_type must be build or version"
            )
        if build_id in build_ids:
            raise InstanceReleaseError("supported game build IDs must be unique")
        build_ids.add(build_id)
        versions.append(version)
        display_types.add(display_type)
    if len(versions) != len(set(versions)):
        raise InstanceReleaseError("supported game display versions must be unique")
    if len(display_types) != 1:
        raise InstanceReleaseError("supported game builds must use one display_type")
    expected_title = _expected_title(
        payload["display_name"],
        payload["language"],
        patch_version,
        versions,
        display_types.pop(),
    )
    if payload["release_title"] != expected_title:
        raise InstanceReleaseError(f"release_title must be exactly: {expected_title}")

    commits = payload["commits"]
    if not isinstance(commits, dict):
        raise InstanceReleaseError("commits must be an object")
    _required_keys(commits, {"sdk", "translation_source"}, scope="commits")
    _commit(commits["sdk"], field="commits.sdk")
    _commit(commits["translation_source"], field="commits.translation_source", optional=True)

    asset = payload["asset"]
    if not isinstance(asset, dict):
        raise InstanceReleaseError("asset must be an object")
    _required_keys(
        asset,
        {"format", "name", "bytes", "sha256", "manifest_sha256"},
        scope="asset",
    )
    if asset["format"] != "parr0t-steam-package/v4":
        raise InstanceReleaseError("asset.format has no registered instance-release validator")
    if Path(_text(asset["name"], field="asset.name")).name != asset["name"]:
        raise InstanceReleaseError("asset.name must be one file name")
    expected_asset_name = (
        f"{_filename_component(payload['display_name'], field='display_name')}-"
        f"{_filename_component(payload['language'], field='language')}-"
        f"{patch_version}.zip"
    )
    if asset["name"] != expected_asset_name:
        raise InstanceReleaseError(f"asset.name must be exactly: {expected_asset_name}")
    if not isinstance(asset["bytes"], int) or asset["bytes"] < 1:
        raise InstanceReleaseError("asset.bytes must be positive")
    _digest(asset["sha256"], field="asset.sha256")
    _digest(asset["manifest_sha256"], field="asset.manifest_sha256")

    checkpoint = payload["checkpoint"]
    if not isinstance(checkpoint, dict):
        raise InstanceReleaseError("checkpoint must be an object")
    _required_keys(checkpoint, {"schema", "bytes", "sha256"}, scope="checkpoint")
    if checkpoint["schema"] != CHECKPOINT_SCHEMA:
        raise InstanceReleaseError("checkpoint.schema is unsupported")
    if not isinstance(checkpoint["bytes"], int) or checkpoint["bytes"] < 1:
        raise InstanceReleaseError("checkpoint.bytes must be positive")
    _digest(checkpoint["sha256"], field="checkpoint.sha256")

    readme = payload["readme"]
    if not isinstance(readme, dict):
        raise InstanceReleaseError("readme must be an object")
    _required_keys(readme, {"base_commit", "mode"}, scope="readme")
    _commit(readme["base_commit"], field="readme.base_commit")
    if readme["mode"] != "append_only":
        raise InstanceReleaseError("readme.mode must be append_only unless the contract is revised")

    glue_paths = payload["glue_paths"]
    if not isinstance(glue_paths, list):
        raise InstanceReleaseError("glue_paths must be a list")
    normalized_glue = [_safe_relative(value, field="glue_paths") for value in glue_paths]
    if normalized_glue != glue_paths:
        raise InstanceReleaseError("glue_paths must use canonical forward-slash paths")
    if len(normalized_glue) != len(set(item.casefold() for item in normalized_glue)):
        raise InstanceReleaseError("glue_paths must be unique")
    return payload


def _tracked_files(root: Path) -> list[str]:
    output = _git(root, "ls-files", "-z")
    return sorted((item for item in output.split("\0") if item), key=str.casefold)


def _validate_branch_files(root: Path, metadata: dict[str, Any]) -> list[str]:
    tracked = _tracked_files(root)
    required = {"README.md", CHECKPOINT_NAME, INSTANCE_RELEASE_NAME}
    missing = required - set(tracked)
    if missing:
        raise InstanceReleaseError(f"Instance branch is missing required files: {sorted(missing)}")
    glue = set(metadata["glue_paths"])
    for relative in tracked:
        normalized = _safe_relative(relative, field="tracked file")
        path = root.joinpath(*PurePosixPath(normalized).parts)
        if path.is_symlink() or not path.is_file():
            raise InstanceReleaseError(f"Instance branch entries must be regular files: {relative}")
        parts = {part.casefold() for part in PurePosixPath(normalized).parts}
        if parts & _FORBIDDEN_PARTS or path.suffix.casefold() in _FORBIDDEN_SUFFIXES:
            raise InstanceReleaseError(f"Instance branch contains a forbidden artifact: {relative}")
        allowed = normalized in required or (
            len(PurePosixPath(normalized).parts) == 1 and _LEGAL_NAMES.fullmatch(path.name)
        )
        if normalized in glue:
            if path.suffix.casefold() not in _GLUE_SUFFIXES:
                raise InstanceReleaseError(f"Glue file type is not reviewable source/config: {relative}")
            if path.stat().st_size > _MAX_GLUE_BYTES:
                raise InstanceReleaseError(f"Glue file is unexpectedly large: {relative}")
            allowed = True
        if not allowed:
            raise InstanceReleaseError(
                f"Tracked file is outside the lean instance allowlist: {relative}"
            )
    unused_glue = glue - set(tracked)
    if unused_glue:
        raise InstanceReleaseError(f"Declared glue files are not tracked: {sorted(unused_glue)}")
    return tracked


def _validate_readme(root: Path, metadata: dict[str, Any]) -> None:
    base_commit = metadata["readme"]["base_commit"]
    resolved_base = _git(root, "rev-parse", base_commit)
    merge_base = _git(root, "merge-base", "HEAD", base_commit)
    if resolved_base.casefold() != merge_base.casefold():
        raise InstanceReleaseError("readme.base_commit must be an ancestor of the release commit")
    base = _git(root, "show", f"{base_commit}:README.md")
    current = (root / "README.md").read_text(encoding="utf-8-sig").rstrip("\r\n")
    if current != base and not current.startswith(base.rstrip("\r\n") + "\n"):
        raise InstanceReleaseError(
            "README.md does not preserve the base README byte-for-text as an append-only prefix"
        )


def validate_instance_release(
    instance_root: Path,
    package_path: Path,
    *,
    user_confirmed_success: bool,
    user_approved_publication: bool,
) -> dict[str, Any]:
    """Validate a committed instance tree before any push, tag, release, or upload."""

    if not user_confirmed_success:
        raise InstanceReleaseError("Patch success has not been explicitly confirmed")
    if not user_approved_publication:
        raise InstanceReleaseError("This exact instance publication has not been explicitly approved")
    root = instance_root.resolve()
    package = package_path.resolve()
    if not (root / ".git").exists():
        raise InstanceReleaseError("instance_root must be a Git repository")
    try:
        package.relative_to(root)
    except ValueError:
        pass
    else:
        raise InstanceReleaseError("Release assets must stay outside the instance repository")
    if _git(root, "status", "--porcelain"):
        raise InstanceReleaseError("Instance repository must be clean and committed before the gate")

    metadata_path = root / INSTANCE_RELEASE_NAME
    metadata = _validate_metadata(json.loads(metadata_path.read_text(encoding="utf-8-sig")))
    tracked = _validate_branch_files(root, metadata)
    _validate_readme(root, metadata)

    checkpoint_path = root / CHECKPOINT_NAME
    checkpoint, checkpoint_bytes = canonical_checkpoint_bytes(
        checkpoint_path,
        expected_patch_id=metadata["patch_id"],
        expected_release_id=metadata["release_id"],
    )
    if checkpoint["sdk"]["commit"].casefold() != metadata["commits"]["sdk"].casefold():
        raise InstanceReleaseError("Checkpoint SDK commit does not match instance metadata")
    checkpoint_builds = {item["id"] for item in checkpoint["game_builds"]}
    metadata_builds = {item["id"] for item in metadata["supported_game_builds"]}
    if checkpoint_builds != metadata_builds:
        raise InstanceReleaseError("Checkpoint and metadata supported-build sets do not match")
    checkpoint_contract = metadata["checkpoint"]
    if checkpoint_contract != {
        "schema": CHECKPOINT_SCHEMA,
        "bytes": len(checkpoint_bytes),
        "sha256": _sha256(checkpoint_bytes),
    }:
        raise InstanceReleaseError("Tracked checkpoint binding does not match its canonical bytes")

    package_result = validate_steam_package(package)
    with zipfile.ZipFile(package, "r") as archive:
        packaged_checkpoint = archive.read(CHECKPOINT_NAME)
        manifest_bytes = archive.read(MANIFEST_NAME)
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    if packaged_checkpoint != checkpoint_bytes:
        raise InstanceReleaseError("Release asset checkpoint differs from the tracked checkpoint")
    if manifest.get("display_name") != metadata["display_name"]:
        raise InstanceReleaseError("Package display name does not match instance metadata")
    if manifest.get("patch_id") != metadata["patch_id"]:
        raise InstanceReleaseError("Package patch ID does not match instance metadata")
    if manifest.get("release_id") != metadata["release_id"]:
        raise InstanceReleaseError("Package release ID does not match instance metadata")
    asset_contract = metadata["asset"]
    if asset_contract != {
        "format": "parr0t-steam-package/v4",
        "name": package.name,
        "bytes": package_result["bytes"],
        "sha256": package_result["sha256"],
        "manifest_sha256": _sha256(manifest_bytes),
    }:
        raise InstanceReleaseError("Release asset binding does not match the verified package bytes")

    tag_exists = bool(_git(root, "rev-parse", "--verify", f"refs/tags/{metadata['tag']}", check=False))
    if tag_exists:
        raise InstanceReleaseError("The intended release tag already exists locally")
    instance_commit = _git(root, "rev-parse", "HEAD")
    gate_material = {
        "schema": GATE_SCHEMA,
        "status": "ready",
        "instance_commit": instance_commit,
        "tag": metadata["tag"],
        "release_id": metadata["release_id"],
        "patch_version": metadata["patch_version"],
        "language": metadata["language"],
        "release_title": metadata["release_title"],
        "asset": asset_contract,
        "checkpoint": checkpoint_contract,
        "sdk_commit": metadata["commits"]["sdk"],
        "translation_source_commit": metadata["commits"]["translation_source"],
        "tracked_files": tracked,
        "package_validation": package_result["status"],
    }
    gate_material["gate_sha256"] = _sha256(
        (json.dumps(gate_material, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    )
    return gate_material
