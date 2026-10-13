from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .errors import RuntimeApprovalError
from .privacy import is_within
from .steam import SteamPackageError, validate_steam_package
from .workspace import load_config


SCHEMA = "parr0t.runtime-approval/v1"
ACTIONS = {"install", "update", "remove", "launch"}
DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 24 * 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise RuntimeApprovalError(f"E_RUNTIME_APPROVAL_{field.upper()}_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeApprovalError(f"E_RUNTIME_APPROVAL_{field.upper()}_INVALID") from exc
    if parsed.tzinfo is None:
        raise RuntimeApprovalError(f"E_RUNTIME_APPROVAL_{field.upper()}_INVALID")
    return parsed.astimezone(timezone.utc)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_artifact(path: Path) -> tuple[str, str]:
    if path.is_file():
        return "file", _hash_file(path)
    if path.is_dir():
        rows: list[tuple[str, int, str]] = []
        for item in sorted(path.rglob("*"), key=lambda value: value.as_posix().casefold()):
            if item.is_symlink():
                raise RuntimeApprovalError("E_RUNTIME_ARTIFACT_SYMLINK")
            if item.is_file():
                rows.append((item.relative_to(path).as_posix(), item.stat().st_size, _hash_file(item)))
        if not rows:
            raise RuntimeApprovalError("E_RUNTIME_ARTIFACT_EMPTY")
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return "directory", hashlib.sha256(payload).hexdigest()
    raise RuntimeApprovalError("E_RUNTIME_ARTIFACT_MISSING")


def _normalize_target(work_dir: Path, target_path: Path) -> Path:
    target = target_path.resolve()
    if not target.is_dir():
        raise RuntimeApprovalError("E_RUNTIME_TARGET_INVALID")
    config = load_config(work_dir)
    configured = [Path(item).resolve() for item in config.get("game_roots", [])]
    if target not in configured:
        raise RuntimeApprovalError("E_RUNTIME_TARGET_NOT_CONFIGURED")
    return target


def _normalize_executable(action: str, target: Path, executable_path: Path | None) -> Path | None:
    if action != "launch":
        if executable_path is not None:
            raise RuntimeApprovalError("E_RUNTIME_EXECUTABLE_NOT_ALLOWED")
        return None
    if executable_path is None:
        raise RuntimeApprovalError("E_RUNTIME_EXECUTABLE_REQUIRED")
    executable = executable_path.resolve()
    if not executable.is_file() or not is_within(executable, target):
        raise RuntimeApprovalError("E_RUNTIME_EXECUTABLE_INVALID")
    return executable


def _artifact_descriptor(artifact_path: Path) -> dict[str, Any]:
    artifact = artifact_path.resolve()
    kind, digest = _hash_artifact(artifact)
    descriptor: dict[str, Any] = {
        "path": str(artifact),
        "kind": kind,
        "sha256": digest,
    }
    if kind == "file" and artifact.suffix.casefold() == ".zip":
        try:
            package = validate_steam_package(artifact)
        except SteamPackageError:
            pass
        else:
            descriptor["fingerprint"] = package["artifact_fingerprint"]
            descriptor["installer_sha256"] = package["installer_sha256"]
    return descriptor


def create_runtime_approval(
    work_dir: Path,
    *,
    action: str,
    artifact_path: Path,
    target_path: Path,
    executable_path: Path | None = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    user_confirmed: bool,
) -> tuple[Path, dict[str, Any]]:
    """Record one explicit, short-lived user approval without performing the action."""
    work_dir = work_dir.resolve()
    if not user_confirmed:
        raise RuntimeApprovalError("E_RUNTIME_USER_CONFIRMATION_REQUIRED")
    if action not in ACTIONS:
        raise RuntimeApprovalError("E_RUNTIME_ACTION_INVALID")
    if ttl_minutes < 1 or ttl_minutes > MAX_TTL_MINUTES:
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_TTL_INVALID")
    artifact = artifact_path.resolve()
    if not is_within(artifact, work_dir):
        raise RuntimeApprovalError("E_RUNTIME_ARTIFACT_OUTSIDE_WORKSPACE")
    target = _normalize_target(work_dir, target_path)
    executable = _normalize_executable(action, target, executable_path)
    created = _utc_now()
    approval_id = secrets.token_hex(16)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "approval_id": approval_id,
        "action": action,
        "artifact": _artifact_descriptor(artifact),
        "target": str(target),
        "executable": str(executable) if executable else None,
        "created_at_utc": _timestamp(created),
        "expires_at_utc": _timestamp(created + timedelta(minutes=ttl_minutes)),
        "one_shot": True,
        "consumed_at_utc": None,
        "authorization_source": "explicit_user_instruction",
    }
    approval_root = work_dir / ".game-l10n" / "runtime-approvals"
    approval_root.mkdir(parents=True, exist_ok=True)
    path = approval_root / f"{approval_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, payload


def require_runtime_approval(
    work_dir: Path,
    approval_path: Path,
    *,
    action: str,
    artifact_path: Path,
    target_path: Path,
    executable_path: Path | None = None,
    consume: bool = False,
) -> dict[str, Any]:
    """Validate an exact approval and optionally consume it immediately before an external effect."""
    work_dir = work_dir.resolve()
    if action not in ACTIONS:
        raise RuntimeApprovalError("E_RUNTIME_ACTION_INVALID")
    approval_root = (work_dir / ".game-l10n" / "runtime-approvals").resolve()
    approval = approval_path.resolve()
    if not approval.is_file() or not is_within(approval, approval_root):
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_INVALID_PATH")
    payload = json.loads(approval.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA or payload.get("one_shot") is not True:
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_SCHEMA")
    if payload.get("authorization_source") != "explicit_user_instruction":
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_SOURCE")
    if payload.get("consumed_at_utc") is not None:
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_CONSUMED")
    if payload.get("action") != action:
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_ACTION_MISMATCH")
    if _parse_timestamp(payload.get("expires_at_utc"), field="expiry") <= _utc_now():
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_EXPIRED")
    target = _normalize_target(work_dir, target_path)
    executable = _normalize_executable(action, target, executable_path)
    if Path(str(payload.get("target", ""))).resolve() != target:
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_TARGET_MISMATCH")
    expected_executable = str(executable) if executable else None
    if payload.get("executable") != expected_executable:
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_EXECUTABLE_MISMATCH")
    artifact = artifact_path.resolve()
    if not is_within(artifact, work_dir):
        raise RuntimeApprovalError("E_RUNTIME_ARTIFACT_OUTSIDE_WORKSPACE")
    current_artifact = _artifact_descriptor(artifact)
    recorded_artifact = payload.get("artifact")
    if not isinstance(recorded_artifact, dict):
        raise RuntimeApprovalError("E_RUNTIME_APPROVAL_ARTIFACT_INVALID")
    for field in ("path", "kind", "sha256", "fingerprint", "installer_sha256"):
        if recorded_artifact.get(field) != current_artifact.get(field):
            raise RuntimeApprovalError("E_RUNTIME_APPROVAL_ARTIFACT_MISMATCH")
    if consume:
        payload["consumed_at_utc"] = _timestamp(_utc_now())
        temporary = approval.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(approval)
    return payload
