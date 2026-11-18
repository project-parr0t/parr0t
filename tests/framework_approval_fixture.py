"""Synthetic trusted-host records for real framework-mode verifier tests."""
import hashlib
import itertools
import json
import os
from pathlib import Path
import time

from game_l10n.framework_approval import FRAMEWORK_APPROVAL_DIRECTORY, FRAMEWORK_APPROVAL_SCHEMA
from game_l10n.framework_session import framework_session_state

_IDS = itertools.count(1)


def issue_approval(workspace: Path, mode: str, *, logs=None, record_overrides=None) -> dict[str, str]:
    number = next(_IDS)
    proof = {"id": f"{number:032x}", "token": f"{number:064x}"}
    workspace = workspace.resolve()
    record = {
        "schema": FRAMEWORK_APPROVAL_SCHEMA,
        "workspace": str(workspace),
        "session_id": "synthetic-root-session", "agent_id": "synthetic-main-agent",
        "agent_kind": "main", "agent_depth": 0, "mode": mode,
        "revision": framework_session_state(workspace)["revision"],
        "logs": [os.path.abspath(workspace / value) for value in logs or []],
        "token_sha256": hashlib.sha256(proof["token"].encode("ascii")).hexdigest(),
        "expires_at": int(time.time()) + 120,
        **(record_overrides or {}),
    }
    directory = workspace / FRAMEWORK_APPROVAL_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{proof['id']}.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    return proof
