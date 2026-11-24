"""Withdraw localization-only instructions without treating diagnostics as translation."""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import stat
import tempfile
import zlib
from pathlib import Path
from typing import Any

from .control_plane import (
    _atomic_json, _record_control_plane_incident, resume_clean_control_plane, suspend_control_plane,
)
from .errors import ConfigurationError
from .framework_approval import consume_framework_approval, require_framework_approval_proof
from .store import exclusive_file_lock

FRAMEWORK_SESSION_SCHEMA = "parr0t.framework-session.v1"
FRAMEWORK_SESSION_PATH = ".game-l10n/framework-session.json"
DIAGNOSTIC_LOG_DIRECTORY = ".game-l10n/diagnostics/logs"


def _framework_session_lock(work_dir: Path):
    return exclusive_file_lock(
        work_dir / ".game-l10n/framework-session.lock",
        issue_code="E_FRAMEWORK_TRANSITION_BUSY",
    )


def framework_session_state(work_dir: Path) -> dict[str, Any]:
    path = work_dir / FRAMEWORK_SESSION_PATH
    try:
        path.lstat()
    except FileNotFoundError:
        return {
            "schema": FRAMEWORK_SESSION_SCHEMA, "mode": "localization", "revision": 0,
            "instructions_active": True, "user_requested": False, "logs": [],
        }
    except OSError as exc:
        raise ConfigurationError("E_FRAMEWORK_SESSION_INVALID: unreadable session state") from exc
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError("E_FRAMEWORK_SESSION_INVALID: unreadable session state") from exc
    fields = {"schema", "mode", "revision", "instructions_active", "user_requested", "logs"}
    if (
        not isinstance(state, dict) or set(state) != fields
        or state.get("schema") != FRAMEWORK_SESSION_SCHEMA
        or not isinstance(state.get("mode"), str)
        or state["mode"] not in {"localization", "pause", "analysis", "shutdown"}
        or type(state.get("revision")) is not int or state["revision"] < 1
        or state.get("instructions_active") is not (state["mode"] == "localization")
        or state.get("user_requested") is not True
        or not isinstance(state.get("logs"), list)
        or (state["mode"] != "analysis" and state["logs"])
    ):
        raise ConfigurationError("E_FRAMEWORK_SESSION_INVALID: invalid session state")
    seen = set()
    for item in state["logs"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes"}:
            raise ConfigurationError("E_FRAMEWORK_SESSION_INVALID: invalid log binding")
        digest = item["sha256"]
        if (
            not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or item["path"] != f"{DIAGNOSTIC_LOG_DIRECTORY}/{digest}.log"
            or type(item["bytes"]) is not int or item["bytes"] < 0
            or digest in seen
        ):
            raise ConfigurationError("E_FRAMEWORK_SESSION_INVALID: invalid log binding")
        seen.add(digest)
    return state


def require_localization_mode(work_dir: Path) -> None:
    if framework_session_state(work_dir)["mode"] != "localization":
        raise ConfigurationError(
            "E_FRAMEWORK_INACTIVE: localization instructions and execution are suspended"
        )


def _snapshot_log(work_dir: Path, source: Path, *, compressed: bool = False) -> dict[str, Any]:
    directory = work_dir / DIAGNOSTIC_LOG_DIRECTORY
    temporary = None
    try:
        if not directory.resolve().is_relative_to(work_dir.resolve()):
            raise OSError("snapshot directory escaped workspace")
        directory.mkdir(parents=True, exist_ok=True)
        source = source.resolve()
        if not source.is_file():
            raise OSError("not a regular log file")
        open_source = gzip.open if compressed else open
        with open_source(source, "rb") as original, tempfile.NamedTemporaryFile(
            mode="w+b", dir=directory, delete=False,
        ) as snapshot:
            temporary = Path(snapshot.name)
            shutil.copyfileobj(original, snapshot)
            size = snapshot.tell()
            snapshot.seek(0)
            digest = hashlib.file_digest(snapshot, "sha256").hexdigest()
        temporary.replace(directory / f"{digest}.log")
    except (OSError, RuntimeError, EOFError, zlib.error) as exc:
        raise ConfigurationError("E_DIAGNOSTIC_LOG_UNAVAILABLE: selected log cannot be snapshotted") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"path": f"{DIAGNOSTIC_LOG_DIRECTORY}/{digest}.log", "sha256": digest, "bytes": size}


def _omp_transcript_path(session_file: Path, subagent_id: str) -> Path:
    """Resolve only the approved OMP artifact; never enumerate session files."""
    try:
        if not stat.S_ISREG(session_file.lstat().st_mode):
            raise OSError("invalid root session")
        artifacts = session_file.resolve(strict=True).with_suffix("")
        if not stat.S_ISDIR(artifacts.lstat().st_mode) or artifacts.resolve(strict=True) != artifacts:
            raise OSError("redirected OMP artifact directory")
        parts = subagent_id.split(".")
        for depth in range(1, len(parts)):
            artifacts /= ".".join(parts[:depth])
            if not stat.S_ISDIR(artifacts.lstat().st_mode) or artifacts.resolve(strict=True) != artifacts:
                raise OSError("redirected OMP child artifact directory")
        found = None
        for suffix in (".jsonl", ".jsonl.gz"):
            candidate = artifacts / (subagent_id + suffix)
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if (found is not None or not stat.S_ISREG(metadata.st_mode)
                    or candidate.resolve(strict=True) != candidate):
                raise OSError("ambiguous or redirected OMP transcript")
            found = candidate
        if found is None:
            raise FileNotFoundError("OMP transcript is absent")
        return found
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError(
            "E_OMP_DIAGNOSTIC_TRANSCRIPT_UNAVAILABLE: exact approved subagent transcript unavailable"
        ) from exc


def change_framework_mode(
    work_dir: Path, mode: str, *, user_requested: bool = False,
    logs: list[Path] | None = None, omp_subagents: list[str] | None = None,
    approval: dict | None = None,
) -> dict[str, Any]:
    if user_requested is not True:
        raise ConfigurationError("E_FRAMEWORK_USER_REQUEST_REQUIRED: explicit user request required")
    require_framework_approval_proof(approval)
    if not isinstance(mode, str) or mode not in {"localization", "pause", "analysis", "shutdown"}:
        raise ConfigurationError("E_FRAMEWORK_MODE_INVALID: select localization, pause, analysis or shutdown")
    if mode != "analysis" and (logs or omp_subagents):
        raise ConfigurationError("E_FRAMEWORK_ANALYSIS_REQUIRED: log access requires analysis mode")
    if not (work_dir / "game-l10n.json").is_file():
        raise ConfigurationError("E_FRAMEWORK_WORKSPACE_REQUIRED: initialized private workspace required")
    with _framework_session_lock(work_dir):
        previous = framework_session_state(work_dir)
        session_file = consume_framework_approval(
            work_dir, mode, revision=previous["revision"], logs=logs,
            omp_subagents=omp_subagents, approval=approval,
        )
        if mode == "localization":
            resume_clean_control_plane(work_dir)
        state = {
            "schema": FRAMEWORK_SESSION_SCHEMA, "mode": mode,
            "revision": previous["revision"] + 1, "instructions_active": mode == "localization",
            "user_requested": True, "logs": [],
        }
        # Suspend first, but do not hold the transaction lock while copying logs:
        # a later shutdown must be able to revoke this request immediately.
        _atomic_json(work_dir / FRAMEWORK_SESSION_PATH, state)
        control_issue = ""
        try:
            if logs or session_file is not None:
                _record_control_plane_incident(work_dir, "parent_opened_data_plane", "control_plane_leak")
            else:
                suspend_control_plane(work_dir)
        except (ConfigurationError, OSError, ValueError):
            control_issue = "control_state_repair_required"
    snapshots = {}
    for selected in logs or []:
        source = selected if selected.is_absolute() else work_dir / selected
        binding = _snapshot_log(work_dir, source)
        snapshots[binding["sha256"]] = binding
    omp_subagent_logs = {}
    if session_file is not None:
        for subagent_id in omp_subagents or []:
            source = _omp_transcript_path(session_file, subagent_id)
            binding = _snapshot_log(work_dir, source, compressed=source.suffix == ".gz")
            snapshots[binding["sha256"]] = binding
            omp_subagent_logs[subagent_id] = binding
    with _framework_session_lock(work_dir):
        if framework_session_state(work_dir) != state:
            raise ConfigurationError(
                "E_FRAMEWORK_MODE_CHANGED: a newer mode request superseded this log grant"
            )
        state = {**state, "logs": list(snapshots.values())}
        _atomic_json(work_dir / FRAMEWORK_SESSION_PATH, state)
    return {
        "framework_session": state, "control_plane_issue": control_issue,
        **({"omp_subagent_logs": omp_subagent_logs} if omp_subagent_logs else {}),
    }


def diagnostic_log_bytes(work_dir: Path, relative: Path) -> bytes:
    state = framework_session_state(work_dir)
    if state["mode"] != "analysis":
        raise ConfigurationError("E_FRAMEWORK_ANALYSIS_REQUIRED: log reads require analysis mode")
    binding = next((item for item in state["logs"] if item["path"] == relative.as_posix()), None)
    if binding is None:
        raise ConfigurationError("E_DIAGNOSTIC_LOG_NOT_APPROVED: log is outside this analysis grant")
    path = work_dir / relative
    if not path.resolve().is_relative_to(work_dir.resolve()):
        raise ConfigurationError("E_DIAGNOSTIC_LOG_NOT_APPROVED: snapshot escaped workspace")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError("E_DIAGNOSTIC_LOG_UNAVAILABLE: approved snapshot is unavailable") from exc
    if len(data) != binding["bytes"] or hashlib.sha256(data).hexdigest() != binding["sha256"]:
        raise ConfigurationError("E_DIAGNOSTIC_LOG_CHANGED: approved snapshot binding changed")
    with _framework_session_lock(work_dir):
        if framework_session_state(work_dir) != state:
            raise ConfigurationError(
                "E_DIAGNOSTIC_LOG_NOT_APPROVED: log access was revoked while reading"
            )
    return data


def _restore_localization_after_rotation(work_dir: Path) -> None:
    """Called only by canonical generation recovery, never by the mode command."""
    state = framework_session_state(work_dir)
    if state["mode"] == "localization":
        return
    _atomic_json(work_dir / FRAMEWORK_SESSION_PATH, {
        **state, "mode": "localization", "revision": state["revision"] + 1,
        "instructions_active": True, "logs": [],
    })
