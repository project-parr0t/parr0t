from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ThirdPartyApprovalError
from .privacy import is_within
from .workspace import load_config
from .store import stable_hash


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
    workflow_reuse: bool = False,
    risk_disclosed: bool,
    user_confirmed: bool,
) -> tuple[Path, dict[str, Any]]:
    """Record exact consent, reusable within its expiry only when explicitly requested."""
    work_dir = work_dir.resolve()
    load_config(work_dir)
    if not user_confirmed:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_USER_CONFIRMATION_REQUIRED")
    if not risk_disclosed:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_RISK_DISCLOSURE_REQUIRED")
    if action not in ACTIONS:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_ACTION_INVALID")
    if isinstance(ttl_minutes, bool) or not isinstance(ttl_minutes, int) or ttl_minutes < 1:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_TTL_INVALID")
    if not isinstance(workflow_reuse, bool):
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_REUSE_INVALID")
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
    try:
        expires = created + timedelta(minutes=ttl_minutes)
    except OverflowError as exc:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_TTL_INVALID") from exc
    approval_id = secrets.token_hex(16)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "approval_id": approval_id,
        **normalized,
        "created_at_utc": _timestamp(created),
        "expires_at_utc": _timestamp(expires),
        "one_shot": not workflow_reuse,
        "consumed_at_utc": None,
        "authorization_source": "explicit_user_instruction",
        "risk_disclosed": True,
    }
    approval_root = work_dir / ".game-l10n" / "third-party-approvals"
    approval_root.mkdir(parents=True, exist_ok=True)
    path = approval_root / f"{approval_id}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path, payload


def _require_third_party_approval(
    work_dir: Path,
    approval_path: Path,
    *,
    action: str,
    name: str,
    publisher: str,
    source: str,
    version: str,
    digest: str,
    purpose: str | None = None,
    data_scope: str | None = None,
    consume: bool = False,
    binding_path: Path | None = None,
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
    if payload.get("schema") != SCHEMA or not isinstance(payload.get("one_shot"), bool):
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_SCHEMA")
    if payload.get("authorization_source") != "explicit_user_instruction" or payload.get("risk_disclosed") is not True:
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_SOURCE")
    binding = _offline_binding(work_dir, binding_path) if binding_path is not None else None
    if payload.get("consumed_at_utc") is not None and (
        binding is None or payload.get("consumption_binding") != binding
    ):
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_CONSUMED")
    if _parse_timestamp(payload.get("expires_at_utc")) <= _utc_now():
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_EXPIRED")
    if not payload["one_shot"] and (
        purpose is None or data_scope is None
        or payload.get("purpose") != _required_text(purpose, field="purpose", limit=1024)
        or payload.get("data_scope") != _required_text(data_scope, field="data_scope", limit=1024)
    ):
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_WORKFLOW_SCOPE_MISMATCH")
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
    if consume and payload["one_shot"] and payload.get("consumed_at_utc") is None:
        payload["consumed_at_utc"] = _timestamp(_utc_now())
        if binding is not None:
            payload["consumption_binding"] = binding
        temporary = approval.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(approval)
    return payload


def _offline_binding(work_dir: Path, binding_path: Path) -> dict[str, str]:
    path = binding_path.resolve()
    if not is_within(path, work_dir.resolve()) or not path.is_file():
        raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_BINDING_REQUIRED")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@contextmanager
def _approval_lock(approval: Path):
    # Keep a stable lock inode; the OS releases the region when a process exits.
    # Existence-only locks would strand valid consent after an interrupted bind.
    with approval.with_suffix(".lock").open("a+b") as stream:
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_BUSY") from exc
        yield


def require_third_party_approval(
    work_dir: Path, approval_path: Path, *,
    action: str, name: str, publisher: str, source: str, version: str, digest: str,
    purpose: str | None = None, data_scope: str | None = None,
    consume: bool = False, binding_path: Path | None = None,
) -> dict[str, Any]:
    arguments = dict(
        action=action, name=name, publisher=publisher, source=source,
        version=version, digest=digest, purpose=purpose, data_scope=data_scope,
        consume=consume, binding_path=binding_path,
    )
    if not consume:
        return _require_third_party_approval(work_dir, approval_path, **arguments)
    approval = approval_path.resolve()
    root = (work_dir / ".game-l10n" / "third-party-approvals").resolve()
    if not is_within(approval, root) or not approval.is_file():
        raise ThirdPartyApprovalError("E_THIRD_PARTY_APPROVAL_INVALID_PATH")
    with _approval_lock(approval):
        return _require_third_party_approval(work_dir, approval, **arguments)


def validated_offline_processing(
    work_dir: Path, entries: list[dict[str, Any]],
    *, binding_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Bind exact Python script argv; never acquire or execute a component."""
    if not isinstance(entries, list):
        raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_INVALID")
    validated = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) - {"argv", "sha256", "approval_path"}
            or not {"argv", "sha256"} <= set(entry)
            or not isinstance(entry["argv"], list)
            or not entry["argv"]
            or any(not isinstance(arg, str) or "\x00" in arg for arg in entry["argv"])
        ):
            raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_INVALID")
        argv = list(entry["argv"])
        script = Path(argv[0])
        if (
            not script.is_absolute() or not script.is_file()
            or script.suffix.casefold() != ".py"
            or str(script.resolve()) != argv[0]
            or hashlib.sha256(script.read_bytes()).hexdigest() != entry["sha256"]
        ):
            raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_SCRIPT_MISMATCH")
        bound = {"argv": argv, "sha256": entry["sha256"]}
        if not is_within(script, Path(__file__).resolve().parent):
            approval = Path(str(entry.get("approval_path", ""))).resolve()
            approval_root = (work_dir / ".game-l10n" / "third-party-approvals").resolve()
            if not is_within(approval, approval_root) or not approval.is_file():
                raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_APPROVAL_REQUIRED")
            try:
                identity = json.loads(approval.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_APPROVAL_REQUIRED") from exc
            if (
                not isinstance(identity, dict)
                or identity.get("purpose") != "Bound offline image processing"
                or identity.get("data_scope") != "offline_processing:" + stable_hash(argv)
            ):
                raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_SCOPE_MISMATCH")
            arguments = {
                "action": "execute", "name": identity.get("name", ""),
                "publisher": identity.get("publisher", ""),
                "source": identity.get("source", ""),
                "version": identity.get("version", ""),
                "digest": "sha256:" + entry["sha256"],
                "purpose": "Bound offline image processing",
                "data_scope": "offline_processing:" + stable_hash(argv),
            }
            receipt = require_third_party_approval(
                work_dir, approval, **arguments, binding_path=binding_path,
            )
            bound["expires_at_utc"] = receipt["expires_at_utc"]
            if not receipt["one_shot"]:
                bound["reusable"] = True
        else:
            bound["reusable"] = True
        validated.append(bound)
    return validated


def consume_offline_processing(
    work_dir: Path, entries: list[dict[str, Any]], *, binding_path: Path,
) -> list[dict[str, Any]]:
    """Consume only after exact processing is durably bound into a valid task."""
    binding = _offline_binding(work_dir, binding_path)
    validated = validated_offline_processing(work_dir, entries, binding_path=binding_path)
    try:
        task = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_BINDING_INVALID") from exc
    unsigned = {key: value for key, value in task.items() if key != "task_id"} if isinstance(task, dict) else {}
    if (
        not isinstance(task, dict)
        or task.get("schema") != "parr0t.data-plane-task.v1"
        or task.get("worker_role") not in {"image_ui_inventory", "image_localizer"}
        or task.get("task_id") != stable_hash(unsigned)
        or task.get("offline_processing") != validated
    ):
        raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_BINDING_MISMATCH")
    approvals = {}
    for entry in entries:
        if is_within(Path(entry["argv"][0]), Path(__file__).resolve().parent):
            continue
        approval = Path(entry["approval_path"]).resolve()
        identity = json.loads(approval.read_text(encoding="utf-8"))
        approvals[approval] = dict(
            action="execute", name=identity["name"], publisher=identity["publisher"],
            source=identity["source"], version=identity["version"],
            digest="sha256:" + entry["sha256"],
            purpose="Bound offline image processing",
            data_scope="offline_processing:" + stable_hash(entry["argv"]),
        )
    # Lock the whole exact approval set before writing any consumption receipt.
    with ExitStack() as locks:
        for approval in sorted(approvals):
            locks.enter_context(_approval_lock(approval))
        for approval, arguments in approvals.items():
            _require_third_party_approval(
                work_dir, approval, **arguments, binding_path=binding_path,
            )
        if _offline_binding(work_dir, binding_path) != binding:
            raise ThirdPartyApprovalError("E_OFFLINE_PROCESSING_BINDING_MISMATCH")
        for approval, arguments in approvals.items():
            _require_third_party_approval(
                work_dir, approval, **arguments, binding_path=binding_path, consume=True,
            )
    return validated
