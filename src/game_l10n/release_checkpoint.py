from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .errors import GameL10nError


CHECKPOINT_NAME = "parr0t-release-checkpoint.json"
CHECKPOINT_SCHEMA = "parr0t.release-checkpoint/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")
_STATUS = {"reviewed", "source_preserved"}
_FALLBACK_MODES = {"fallback_locale", "placeholder", "source_text", "none"}


class ReleaseCheckpointError(GameL10nError):
    pass


def _required_keys(payload: dict[str, Any], expected: set[str], *, scope: str) -> None:
    missing = expected - set(payload)
    extra = set(payload) - expected
    if missing or extra:
        raise ReleaseCheckpointError(
            f"{scope} fields do not match the checkpoint contract "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )


def _text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseCheckpointError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, *, field: str) -> str:
    value = _text(value, field=field)
    if not _SHA256.fullmatch(value):
        raise ReleaseCheckpointError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _validate_locale_fallback(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReleaseCheckpointError("locale_fallback must be an object")
    _required_keys(
        payload,
        {"status", "mode", "fallback_locale", "placeholder", "verified", "reason_code"},
        scope="locale_fallback",
    )
    status = _text(payload["status"], field="locale_fallback.status")
    mode = _text(payload["mode"], field="locale_fallback.mode")
    if mode not in _FALLBACK_MODES:
        raise ReleaseCheckpointError("locale_fallback.mode is unsupported")
    if not isinstance(payload["verified"], bool):
        raise ReleaseCheckpointError("locale_fallback.verified must be boolean")
    fallback_locale = payload["fallback_locale"]
    placeholder = payload["placeholder"]
    reason_code = payload["reason_code"]
    if not all(isinstance(item, str) for item in (fallback_locale, placeholder, reason_code)):
        raise ReleaseCheckpointError("locale fallback optional fields must be strings")
    if status == "implemented":
        if mode == "none" or payload["verified"] is not True:
            raise ReleaseCheckpointError(
                "implemented locale fallback requires a verified non-none mode"
            )
        if mode == "fallback_locale" and not fallback_locale.strip():
            raise ReleaseCheckpointError(
                "fallback_locale mode requires an explicit locale such as en"
            )
        if mode == "placeholder" and not placeholder:
            raise ReleaseCheckpointError("placeholder mode requires placeholder text")
        if reason_code:
            raise ReleaseCheckpointError("implemented locale fallback cannot have a reason code")
    elif status == "unsupported":
        if mode != "none" or payload["verified"] is not False or not reason_code.strip():
            raise ReleaseCheckpointError(
                "unsupported locale fallback requires mode none, verified false, and a reason code"
            )
        if fallback_locale or placeholder:
            raise ReleaseCheckpointError(
                "unsupported locale fallback cannot claim a locale or placeholder"
            )
    else:
        raise ReleaseCheckpointError("locale_fallback.status must be implemented or unsupported")
    return payload


def validate_release_checkpoint(
    payload: Any,
    *,
    expected_patch_id: str | None = None,
    expected_release_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReleaseCheckpointError("Release checkpoint must be a JSON object")
    _required_keys(
        payload,
        {
            "schema",
            "patch_id",
            "release_id",
            "sdk",
            "game_builds",
            "source_inventory",
            "context",
            "entries",
            "locale_fallback",
        },
        scope="checkpoint",
    )
    if payload["schema"] != CHECKPOINT_SCHEMA:
        raise ReleaseCheckpointError("Release checkpoint schema is unsupported")
    patch_id = _text(payload["patch_id"], field="patch_id")
    release_id = _text(payload["release_id"], field="release_id")
    if expected_patch_id is not None and patch_id != expected_patch_id:
        raise ReleaseCheckpointError("Release checkpoint patch_id does not match the package")
    if expected_release_id is not None and release_id != expected_release_id:
        raise ReleaseCheckpointError("Release checkpoint release_id does not match the package")

    sdk = payload["sdk"]
    if not isinstance(sdk, dict):
        raise ReleaseCheckpointError("sdk must be an object")
    _required_keys(sdk, {"branch", "commit", "adapter_fingerprint"}, scope="sdk")
    _text(sdk["branch"], field="sdk.branch")
    commit = _text(sdk["commit"], field="sdk.commit")
    if not _GIT_COMMIT.fullmatch(commit):
        raise ReleaseCheckpointError("sdk.commit must be a Git commit ID")
    _digest(sdk["adapter_fingerprint"], field="sdk.adapter_fingerprint")

    builds = payload["game_builds"]
    if not isinstance(builds, list) or not builds:
        raise ReleaseCheckpointError("game_builds must contain at least one verified build")
    build_ids: set[str] = set()
    for index, build in enumerate(builds):
        if not isinstance(build, dict):
            raise ReleaseCheckpointError(f"game_builds[{index}] must be an object")
        _required_keys(build, {"id", "binding_fingerprint"}, scope=f"game_builds[{index}]")
        build_id = _text(build["id"], field=f"game_builds[{index}].id")
        if build_id in build_ids:
            raise ReleaseCheckpointError("game build IDs must be unique")
        build_ids.add(build_id)
        _digest(
            build["binding_fingerprint"],
            field=f"game_builds[{index}].binding_fingerprint",
        )

    inventory = payload["source_inventory"]
    if not isinstance(inventory, dict):
        raise ReleaseCheckpointError("source_inventory must be an object")
    _required_keys(
        inventory,
        {"method", "fingerprint", "target_count"},
        scope="source_inventory",
    )
    _text(inventory["method"], field="source_inventory.method")
    _digest(inventory["fingerprint"], field="source_inventory.fingerprint")
    if not isinstance(inventory["target_count"], int) or inventory["target_count"] < 1:
        raise ReleaseCheckpointError("source_inventory.target_count must be positive")

    context = payload["context"]
    if not isinstance(context, dict):
        raise ReleaseCheckpointError("context must be an object")
    _required_keys(
        context,
        {"fingerprint", "review_policy_fingerprint"},
        scope="context",
    )
    _digest(context["fingerprint"], field="context.fingerprint")
    _digest(
        context["review_policy_fingerprint"],
        field="context.review_policy_fingerprint",
    )

    entries = payload["entries"]
    if not isinstance(entries, list) or len(entries) != inventory["target_count"]:
        raise ReleaseCheckpointError(
            "entries must match source_inventory.target_count exactly"
        )
    ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ReleaseCheckpointError(f"entries[{index}] must be an object")
        _required_keys(
            entry,
            {
                "id",
                "source_sha256",
                "target",
                "target_sha256",
                "location_fingerprint",
                "dependency_fingerprint",
                "review_fingerprint",
                "status",
                "dependencies",
            },
            scope=f"entries[{index}]",
        )
        target_id = _text(entry["id"], field=f"entries[{index}].id")
        if target_id in ids:
            raise ReleaseCheckpointError("checkpoint target IDs must be unique")
        ids.add(target_id)
        source_digest = _digest(
            entry["source_sha256"], field=f"entries[{index}].source_sha256"
        )
        target = entry["target"]
        if not isinstance(target, str):
            raise ReleaseCheckpointError(f"entries[{index}].target must be a string")
        target_digest = _digest(
            entry["target_sha256"], field=f"entries[{index}].target_sha256"
        )
        calculated = hashlib.sha256(target.encode("utf-8")).hexdigest()
        if target_digest != calculated:
            raise ReleaseCheckpointError(
                f"entries[{index}].target_sha256 does not match target"
            )
        for key in (
            "location_fingerprint",
            "dependency_fingerprint",
            "review_fingerprint",
        ):
            _digest(entry[key], field=f"entries[{index}].{key}")
        status = entry["status"]
        if status not in _STATUS:
            raise ReleaseCheckpointError(
                f"entries[{index}].status must be reviewed or source_preserved"
            )
        if status == "source_preserved" and source_digest != target_digest:
            raise ReleaseCheckpointError(
                "source_preserved checkpoint targets must hash to the source digest"
            )
        refs = entry["dependencies"]
        if not isinstance(refs, list) or not all(
            isinstance(item, str) and item for item in refs
        ):
            raise ReleaseCheckpointError(
                f"entries[{index}].dependencies must be target ID strings"
            )
        if len(refs) != len(set(refs)) or target_id in refs:
            raise ReleaseCheckpointError(
                f"entries[{index}].dependencies must be unique and cannot include itself"
            )
        dependencies[target_id] = refs
    unknown_dependencies = {
        dependency
        for refs in dependencies.values()
        for dependency in refs
        if dependency not in ids
    }
    if unknown_dependencies:
        raise ReleaseCheckpointError("checkpoint dependencies reference unknown targets")

    _validate_locale_fallback(payload["locale_fallback"])
    return payload


def canonical_checkpoint_bytes(
    path: Path,
    *,
    expected_patch_id: str | None = None,
    expected_release_id: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    validated = validate_release_checkpoint(
        payload,
        expected_patch_id=expected_patch_id,
        expected_release_id=expected_release_id,
    )
    data = (
        json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    return validated, data
