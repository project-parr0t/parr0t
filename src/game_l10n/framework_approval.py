"""Consume root-host, operation-bound framework-mode approvals.

The host boundary binds an explicit user-request assertion and root identity;
it does not require a second interactive dialog. These records are
not a sandbox against arbitrary native code running as the same OS user.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from .errors import ConfigurationError

FRAMEWORK_APPROVAL_SCHEMA = "parr0t.framework-mode-approval.v1"
FRAMEWORK_APPROVAL_DIRECTORY = ".game-l10n/framework-approvals"
_PROOF_LIMIT = 1024
_RECORD_LIMIT = 65536
_FIELDS = {
    "schema", "workspace", "session_id", "agent_id", "agent_kind", "agent_depth",
    "mode", "revision", "logs", "token_sha256", "expires_at",
}
_OMP_TRANSCRIPT_FIELD = "omp_transcripts"
_OMP_SUBAGENT_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_][A-Za-z0-9_-]*)*")


def _deny() -> ConfigurationError:
    return ConfigurationError("E_FRAMEWORK_APPROVAL_INVALID: valid root user approval required")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _proof_valid(proof: Any) -> bool:
    return (
        isinstance(proof, dict) and set(proof) == {"id", "token"}
        and isinstance(proof["id"], str)
        and re.fullmatch(r"[0-9a-f]{32}", proof["id"]) is not None
        and isinstance(proof["token"], str)
        and re.fullmatch(r"[0-9a-f]{64}", proof["token"]) is not None
    )


def require_framework_approval_proof(approval: dict | None) -> None:
    """Reject absent/malformed capabilities without even creating a lock file."""
    if approval is None:
        raise ConfigurationError("E_FRAMEWORK_APPROVAL_REQUIRED: explicit root user approval required")
    if not _proof_valid(approval):
        raise _deny()


def read_framework_approval(stream: BinaryIO | TextIO) -> dict[str, str]:
    """Read a bounded UTF-8 proof, never reflecting input in diagnostics."""
    try:
        raw = stream.read(_PROOF_LIMIT + 1)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes) or len(raw) > _PROOF_LIMIT:
            raise ValueError("invalid proof size")
        proof = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not _proof_valid(proof):
            raise ValueError("invalid proof")
        return proof
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise _deny() from exc


def consume_framework_approval(
    work_dir: Path, mode: str, *, revision: int, logs: list[Path] | None,
    omp_subagents: list[str] | None = None, approval: dict | None,
) -> Path | None:
    """Consume one approval and return its OMP session anchor, if selected."""
    require_framework_approval_proof(approval)
    try:
        workspace = work_dir.resolve(strict=True)
        directory = workspace / FRAMEWORK_APPROVAL_DIRECTORY
        # Reject redirected metadata, directory and record paths, not just a
        # final symlink. The host uses exclusive creation with mode 0600.
        for parent in (workspace / ".game-l10n", directory):
            metadata = parent.lstat()
            if (not stat.S_ISDIR(metadata.st_mode) or parent.is_symlink()
                    or parent.resolve(strict=True) != parent):
                raise ValueError("redirected approval directory")
        path = directory / f"{approval['id']}.json"
        consumed = directory / f"{approval['id']}.consumed.json"
        if os.path.lexists(consumed):
            raise ValueError("approval already consumed")
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or path.is_symlink()
                or path.resolve(strict=True) != path):
            raise ValueError("invalid approval file")
        with path.open("rb") as handle:
            raw = handle.read(_RECORD_LIMIT + 1)
        if len(raw) > _RECORD_LIMIT:
            raise ValueError("invalid approval size")
        record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        selected = [os.path.abspath(workspace / value) for value in logs or []]
        subagents = omp_subagents or []
        fields = _FIELDS | {_OMP_TRANSCRIPT_FIELD} if subagents else _FIELDS
        now = int(time.time())
        if (
            not isinstance(record, dict) or set(record) != fields
            or record["schema"] != FRAMEWORK_APPROVAL_SCHEMA
            or not isinstance(record["workspace"], str)
            or not Path(record["workspace"]).is_absolute()
            or os.path.normpath(record["workspace"]) != record["workspace"]
            or Path(record["workspace"]).resolve(strict=True) != workspace
            or not isinstance(record["session_id"], str) or not record["session_id"].strip()
            or not isinstance(record["agent_id"], str) or not record["agent_id"].strip()
            or record["agent_kind"] != "main"
            or type(record["agent_depth"]) is not int or record["agent_depth"] != 0
            or record["mode"] != mode
            or type(record["revision"]) is not int or record["revision"] != revision
            or not isinstance(record["logs"], list) or record["logs"] != selected
            or type(record["expires_at"]) is not int
            or not now < record["expires_at"] <= now + 120
            or not isinstance(record["token_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", record["token_sha256"]) is None
            or not hmac.compare_digest(
                record["token_sha256"], hashlib.sha256(approval["token"].encode("ascii")).hexdigest(),
            )
        ):
            raise ValueError("approval binding mismatch")
        session_file = None
        if subagents:
            selection = record[_OMP_TRANSCRIPT_FIELD]
            if (
                mode != "analysis"
                or not isinstance(selection, dict) or set(selection) != {"session_file", "subagents"}
                or selection["subagents"] != subagents
                or any(not isinstance(item, str) or len(item) > 200
                       or _OMP_SUBAGENT_ID.fullmatch(item) is None for item in subagents)
                or len(set(subagents)) != len(subagents)
                or not isinstance(selection["session_file"], str) or "\0" in selection["session_file"]
            ):
                raise ValueError("transcript approval binding mismatch")
            session_file = Path(selection["session_file"])
            if not session_file.is_absolute() or session_file.suffix != ".jsonl":
                raise ValueError("invalid OMP session anchor")
        path.rename(consumed)
        return session_file
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError) as exc:
        raise _deny() from exc
