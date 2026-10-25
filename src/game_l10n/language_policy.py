from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .store import stable_hash


LANGUAGE_POLICY_SCHEMA = "parr0t.target-language-policy.v1"
LANGUAGE_POLICY_PATH = Path(".game-l10n/target-language-policy.json")
DEFAULT_TARGET_LANGUAGE = "ko"
_LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*")


def normalize_language_tag(value: Any, *, field: str) -> str:
    normalized = str(value).strip().casefold().replace("_", "-")
    if not _LANGUAGE_TAG.fullmatch(normalized):
        raise ConfigurationError(f"E_LANGUAGE_POLICY_INVALID: {field} is invalid")
    return normalized


def _default_policy() -> dict[str, Any]:
    return {
        "schema": LANGUAGE_POLICY_SCHEMA,
        "target_language": DEFAULT_TARGET_LANGUAGE,
        "revision": 0,
        "authorization": "default_korean",
    }


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def target_language_policy(work_dir: Path) -> dict[str, Any]:
    path = work_dir / LANGUAGE_POLICY_PATH
    if not path.is_file():
        return _default_policy()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("E_LANGUAGE_POLICY_INVALID: policy is unreadable") from exc
    required = {"schema", "target_language", "revision", "authorization"}
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload.get("schema") != LANGUAGE_POLICY_SCHEMA
        or not isinstance(payload.get("revision"), int)
        or isinstance(payload.get("revision"), bool)
        or payload["revision"] < 0
        or payload.get("authorization")
        not in {"default_korean", "explicit_user_request"}
    ):
        raise ConfigurationError("E_LANGUAGE_POLICY_INVALID: policy shape is invalid")
    payload["target_language"] = normalize_language_tag(
        payload.get("target_language"), field="target_language"
    )
    if payload["authorization"] == "default_korean" and (
        payload["target_language"] != DEFAULT_TARGET_LANGUAGE
        or payload["revision"] != 0
    ):
        raise ConfigurationError(
            "E_LANGUAGE_POLICY_INVALID: default authorization may select only Korean"
        )
    return payload


def language_policy_digest(policy: dict[str, Any]) -> str:
    return stable_hash(
        {
            "target_language": policy["target_language"],
            "revision": policy["revision"],
            "authorization": policy["authorization"],
        }
    )


def require_target_language_policy(
    work_dir: Path, config: dict[str, Any]
) -> dict[str, Any]:
    project = config.get("project")
    if not isinstance(project, dict):
        raise ConfigurationError("E_LANGUAGE_POLICY_INVALID: project is missing")
    configured = normalize_language_tag(
        project.get("target_language"), field="project.target_language"
    )
    policy = target_language_policy(work_dir)
    if configured != policy["target_language"]:
        raise ConfigurationError(
            "E_TARGET_LANGUAGE_DRIFT: project target language differs from the locked "
            "default or explicit user policy"
        )
    return policy


def language_binding(work_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    project = config.get("project")
    if (
        not (work_dir / LANGUAGE_POLICY_PATH).is_file()
        and (
            not isinstance(project, dict)
            or not project.get("target_language")
            or not project.get("source_language")
        )
    ):
        policy = _default_policy()
        return {
            "source_language": "und",
            "target_language": policy["target_language"],
            "policy_revision": policy["revision"],
            "policy_digest": language_policy_digest(policy),
        }
    policy = require_target_language_policy(work_dir, config)
    project = config["project"]
    return {
        "source_language": normalize_language_tag(
            project.get("source_language"), field="project.source_language"
        ),
        "target_language": policy["target_language"],
        "policy_revision": policy["revision"],
        "policy_digest": language_policy_digest(policy),
    }


def record_target_language_policy(
    work_dir: Path,
    *,
    target_language: str,
    user_requested: bool,
) -> dict[str, Any]:
    if not user_requested:
        raise ConfigurationError(
            "E_TARGET_LANGUAGE_AUTHORIZATION_REQUIRED: changing the target language "
            "requires an explicit user request"
        )
    config_path = work_dir / "game-l10n.json"
    if not config_path.is_file():
        raise ConfigurationError("E_LANGUAGE_POLICY_INVALID: workspace config is missing")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("E_LANGUAGE_POLICY_INVALID: workspace config is unreadable") from exc
    if not isinstance(config, dict) or not isinstance(config.get("project"), dict):
        raise ConfigurationError("E_LANGUAGE_POLICY_INVALID: project is missing")
    current = target_language_policy(work_dir)
    normalized = normalize_language_tag(target_language, field="target_language")
    policy = {
        "schema": LANGUAGE_POLICY_SCHEMA,
        "target_language": normalized,
        "revision": int(current["revision"]) + 1,
        "authorization": "explicit_user_request",
    }
    config["project"]["target_language"] = normalized
    _atomic_json(config_path, config)
    _atomic_json(work_dir / LANGUAGE_POLICY_PATH, policy)
    return {**policy, "policy_digest": language_policy_digest(policy)}


def public_target_language_policy(
    work_dir: Path, config: dict[str, Any]
) -> dict[str, Any]:
    try:
        binding = language_binding(work_dir, config)
    except ConfigurationError:
        return {"status": "invalid", "issue_codes": ["TARGET_LANGUAGE_POLICY_INVALID"]}
    return {
        "status": "locked",
        "target_language": binding["target_language"],
        "policy_revision": binding["policy_revision"],
        "policy_digest": binding["policy_digest"],
        "issue_codes": [],
    }
