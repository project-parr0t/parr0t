from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ThirdPartyApprovalError
from .privacy import is_within
from .workspace import load_config


SCHEMA = "parr0t.third-party-approval/v1"
ACTIONS = {
    "download",
    "clone",
    "install",
    "execute",
    "vendor",
    "add-dependency",
    "add-submodule",
    "use-service",
    "send-data",
}
DEFAULT_TTL_MINUTES = 30
MAX_TTL_MINUTES = 24 * 60
_IMMUTABLE_DIGEST = re.compile(r"^(?:sha256:[0-9a-f]{64}|git:[0-9a-f]{40,64})$")
_AMBIGUOUS_VERSIONS = {"*", "head", "latest", "main", "master", "stable", "trunk"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_EXPIRY_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_EXPIRY_INVALID") from exc
    if parsed.tzinfo is None:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_EXPIRY_INVALID")
    return parsed.astimezone(timezone.utc)


def _required_text(value: str, *, field: str, limit: int = 512) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise ThirdPartyApprovalError(f"E_THIRD_PARTY_{field.upper()}_INVALID")
    return normalized


def _normalize_source(value: str) -> str:
    source = _required_text(value, field="source", limit=2048)
    parsed = urlsplit(source)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_SOURCE_INVALID")
    return source


def _normalize_version(value: str) -> str:
    version = _required_text(value, field="version", limit=256)
    if version.casefold() in _AMBIGUOUS_VERSIONS or any(marker in version for marker in ("*", "^", "~", ">", "<")):
        raise ThirdPartyApprovalError("E_THIRD_PARTY_VERSION_NOT_EXACT")
    return version


def _normalize_digest(action: str, value: str) -> str:
    digest = _required_text(value, field="digest", limit=128).casefold()
    if action in {"use-service", "send-data"} and digest == "not-applicable":
        return digest
    if not _IMMUTABLE_DIGEST.fullmatch(digest):
        raise ThirdPartyApprovalError("E_THIRD_PARTY_DIGEST_NOT_IMMUTABLE")
    return digest


def create_third_party_approval(
    work_dir: Path,
    *,
    action: str,
    name: str,
    publisher: str,
    source: str,
    version: str,
    digest: str,
    license_name: str,
    purpose: str,
    data_scope: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    risk_disclosed: bool,
    user_confirmed: bool,
) -> tuple[Path, dict[str, Any]]:
    """Record exact, short-lived consent without acquiring or using the component."""
    work_dir = work_dir.resolve()
    load_config(work_dir)
    if not user_confirmed:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_USER_CONFIRMATION_REQUIRED")
    if not risk_disclosed:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_RISK_DISCLOSURE_REQUIRED")
    if action not in ACTIONS:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_ACTION_INVALID")
    if ttl_minutes < 1 or ttl_minutes > MAX_TTL_MINUTES:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_TTL_INVALID")
    normalized = {
        "action": action,
        "name": _required_text(name, field="name", limit=256),
        "publisher": _required_text(publisher, field="publisher", limit=256),
        "source": _normalize_source(source),
        "version": _normalize_version(version),
        "digest": _normalize_digest(action, digest),
        "license": _required_text(license_name, field="license", limit=256),
        "purpose": _required_text(purpose, field="purpose", limit=1024),
        "data_scope": _required_text(data_scope, field="data_scope", limit=1024),
    }
    created = _utc_now()
    approval_id = secrets.token_hex(16)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "approval_id": approval_id,
        **normalized,
        "created_at_utc": _timestamp(created),
        "expires_at_utc": _timestamp(created + timedelta(minutes=ttl_minutes)),
        "one_shot": True,
        "consumed_at_utc": None,
        "authorization_source": "explicit_user_instruction",
        "risk_disclosed": True,
    }
    approval_root = work_dir / ".game-l10n" / "third-party-approvals"
    approval_root.mkdir(parents=True, exist_ok=True)
    path = approval_root / f"{approval_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, payload


def require_third_party_approval(
    work_dir: Path,
    approval_path: Path,
    *,
    action: str,
    name: str,
    publisher: str,
    source: str,
    version: str,
    digest: str,
    consume: bool = False,
) -> dict[str, Any]:
    """Validate exact consent and optionally consume it immediately before the external action."""
    work_dir = work_dir.resolve()
    load_config(work_dir)
    if action not in ACTIONS:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_ACTION_INVALID")
    approval_root = (work_dir / ".game-l10n" / "third-party-approvals").resolve()
    approval = approval_path.resolve()
    if not approval.is_file() or not is_within(approval, approval_root):
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_INVALID_PATH")
    try:
        payload = json.loads(approval.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_INVALID") from exc
    if payload.get("schema") != SCHEMA or payload.get("one_shot") is not True:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_SCHEMA")
    if payload.get("authorization_source") != "explicit_user_instruction" or payload.get("risk_disclosed") is not True:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_SOURCE")
    if payload.get("consumed_at_utc") is not None:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_CONSUMED")
    if _parse_timestamp(payload.get("expires_at_utc")) <= _utc_now():
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_EXPIRED")
    expected = {
        "action": action,
        "name": _required_text(name, field="name", limit=256),
        "publisher": _required_text(publisher, field="publisher", limit=256),
        "source": _normalize_source(source),
        "version": _normalize_version(version),
        "digest": _normalize_digest(action, digest),
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise ThirdPartyApprovalError(f"E_THIRD_PARTY_APPROVAL_{field.upper()}_MISMATCH")
    if consume:
        payload["consumed_at_utc"] = _timestamp(_utc_now())
        temporary = approval.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(approval)
    return payload
