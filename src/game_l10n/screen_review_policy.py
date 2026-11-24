"""Workspace-level permission for the additional text screen review only."""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from .errors import ConfigurationError


_PATH = Path(".game-l10n/screen-review-policy.json")
_SCHEMA = "parr0t.screen-review-policy.v1"


def _policy_record(work_dir: Path, mode: str) -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "scope": "workspace_text",
        "workspace_sha256": sha256(str(work_dir.resolve()).encode("utf-8")).hexdigest(),
        "mode": mode,
        "user_requested": True,
    }


def screen_review_skip_allowed(work_dir: Path) -> bool:
    try:
        record = json.loads((work_dir / _PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return record == _policy_record(work_dir, "skip") and record["user_requested"] is True


def record_screen_review_policy(work_dir: Path, *, mode: str, user_requested: bool) -> dict[str, Any]:
    if user_requested is not True or mode not in {"required", "skip"}:
        raise ConfigurationError("E_SCREEN_REVIEW_POLICY_APPROVAL: an explicit workspace screen review choice is required")
    if not (work_dir / "game-l10n.json").is_file():
        raise ConfigurationError("E_SCREEN_REVIEW_POLICY_WORKSPACE: initialize the workspace first")
    from .coverage import _atomic_json

    record = _policy_record(work_dir, mode)
    _atomic_json(work_dir / _PATH, record)
    return record
