from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .privacy import is_within


CONTROL_ENVELOPE_SCHEMA = "parr0t.control-envelope.v1"
CONTROL_STATE_SCHEMA = "parr0t.control-plane-state.v1"
CONTROL_STATE_PATH = ".game-l10n/control-plane-state.json"

WORKER_ROLES = {
    "primary_inventory",
    "independent_inventory",
    "context_analyzer",
    "translator",
    "review_judge",
    "review_rewriter",
    "artifact_builder",
    "validator",
}

ROLE_STAGES = {
    "primary_inventory": {"inventory"},
    "independent_inventory": {"inventory"},
    "context_analyzer": {"context"},
    "translator": {"translation"},
    "review_judge": {"review"},
    "review_rewriter": {"rewrite"},
    "artifact_builder": {"application"},
    "validator": {"validation"},
}

CONTROL_STATUSES = {"success", "partial", "refused", "error", "invalid"}

COUNT_KEYS = {
    "total",
    "records",
    "success",
    "partial",
    "refused",
    "error",
    "invalid",
    "pending",
    "missing",
    "reviewed",
    "rewrite",
    "source_preserved",
    "blocking",
}

QUEUE_NAMES = {
    "inventory_pending",
    "context_pending",
    "primary_pending",
    "translation_fallback_pending",
    "review_pending",
    "rewrite_pending",
    "review_fallback_pending",
    "operational_failure",
    "source_preserved",
    "held",
    "complete",
}

COMMON_ISSUE_CODES = {
    "malformed_response",
    "provider_error",
    "rate_limited",
    "quota_exhausted",
    "authentication_error",
    "timeout",
    "integrity_invalid",
    "budget_exhausted",
    "budget_accounting_uncertain",
    "task_incomplete",
    "missing_artifact",
    "target_mismatch",
}

PROVIDER_ISSUE_CODES = {"provider_refusal"}

INVENTORY_ISSUE_CODES = {
    "independent_inventory_found_unextracted_targets",
    "independent_inventory_missed_primary_targets",
    "inventory_source_mismatch",
    "inventory_ui_composition_mismatch",
    "inventory_order_mismatch",
    "user_visible_scope_excluded",
    "inventory_not_declared_complete",
}

CONTEXT_ISSUE_CODES = {
    "context_coverage_incomplete",
    "context_execution_incomplete",
    "context_inventory_mismatch",
}

TRANSLATION_ISSUE_CODES = {
    "placeholder_mismatch",
    "control_code_mismatch",
    "source_preserved",
}

APPLICATION_ISSUE_CODES = {
    "application_incomplete",
    "artifact_integrity_invalid",
    "format_preservation_invalid",
}

VALIDATION_ISSUE_CODES = {
    "result_inventory_mismatch",
    "semantic_review_incomplete",
    "partial_translation_scope",
    "source_preserved_after_route_exhaustion",
    "runtime_coverage_incomplete",
    "real_runtime_not_executed",
    "runtime_coverage_stale",
}

ISSUE_CODES_BY_STAGE = {
    "inventory": COMMON_ISSUE_CODES | INVENTORY_ISSUE_CODES,
    "context": COMMON_ISSUE_CODES | PROVIDER_ISSUE_CODES | CONTEXT_ISSUE_CODES,
    "translation": COMMON_ISSUE_CODES | PROVIDER_ISSUE_CODES | TRANSLATION_ISSUE_CODES,
    # Detailed review findings (meaning, voice, omission, tone, and similar
    # judgments) stay in the private data-plane artifact. The parent needs only
    # the target IDs and queue transition that route a rewrite.
    "review": COMMON_ISSUE_CODES | PROVIDER_ISSUE_CODES,
    "rewrite": COMMON_ISSUE_CODES | PROVIDER_ISSUE_CODES | TRANSLATION_ISSUE_CODES,
    "application": COMMON_ISSUE_CODES | APPLICATION_ISSUE_CODES,
    "validation": COMMON_ISSUE_CODES | VALIDATION_ISSUE_CODES,
}

LEAK_INCIDENT_CODES = {
    "unvalidated_worker_response",
    "parent_opened_data_plane",
    "semantic_judgment_performed",
    "prose_refusal_explanation_received",
    "source_or_candidate_received",
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def initial_control_plane_state() -> dict[str, Any]:
    return {
        "schema": CONTROL_STATE_SCHEMA,
        "status": "ready",
        "generation": 1,
        "incident_id": "",
        "issue_code": "",
        "incident_code": "",
    }


def control_plane_state(work_dir: Path) -> dict[str, Any]:
    path = work_dir / CONTROL_STATE_PATH
    if not path.is_file():
        return initial_control_plane_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != CONTROL_STATE_SCHEMA:
        raise ConfigurationError("E_CONTROL_PLANE_STATE: unsupported state schema")
    if payload.get("status") not in {"ready", "rotation_required"}:
        raise ConfigurationError("E_CONTROL_PLANE_STATE: invalid state status")
    if not isinstance(payload.get("generation"), int) or payload["generation"] < 1:
        raise ConfigurationError("E_CONTROL_PLANE_STATE: invalid generation")
    return payload


def require_control_plane_ready(work_dir: Path) -> dict[str, Any]:
    state = control_plane_state(work_dir)
    if state["status"] != "ready":
        raise ConfigurationError(
            "E_CONTROL_PLANE_ROTATION_REQUIRED: stop this top-level agent and resume "
            "from metadata-only checkpoints in a fresh control plane"
        )
    return state


def record_control_plane_leak(work_dir: Path, incident_code: str) -> dict[str, Any]:
    if incident_code not in LEAK_INCIDENT_CODES:
        raise ConfigurationError("E_CONTROL_PLANE_LEAK: unsupported incident code")
    state = control_plane_state(work_dir)
    if state["status"] == "rotation_required":
        return state
    contaminated = {
        "schema": CONTROL_STATE_SCHEMA,
        "status": "rotation_required",
        "generation": state["generation"],
        "incident_id": uuid.uuid4().hex,
        "issue_code": "control_plane_leak",
        "incident_code": incident_code,
    }
    _atomic_json(work_dir / CONTROL_STATE_PATH, contaminated)
    return contaminated


def acknowledge_control_plane_rotation(work_dir: Path, incident_id: str) -> dict[str, Any]:
    state = control_plane_state(work_dir)
    if state["status"] != "rotation_required":
        raise ConfigurationError("E_CONTROL_PLANE_STATE: no rotation is pending")
    if not incident_id or state["incident_id"] != incident_id:
        raise ConfigurationError("E_CONTROL_PLANE_STATE: incident ID does not match")
    resumed = {
        "schema": CONTROL_STATE_SCHEMA,
        "status": "ready",
        "generation": state["generation"] + 1,
        "incident_id": "",
        "issue_code": "",
        "incident_code": "",
        "resumed_from_incident": incident_id,
    }
    _atomic_json(work_dir / CONTROL_STATE_PATH, resumed)
    return resumed


def _validate_target_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 2000:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: target_ids must be a bounded list")
    for target_id in value:
        if (
            not isinstance(target_id, str)
            or not target_id
            or len(target_id) > 512
            or any(character.isspace() or ord(character) < 32 for character in target_id)
        ):
            raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: target_ids must be opaque IDs")
    if len(value) != len(set(value)):
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: target_ids contain duplicates")
    return value


def _validate_artifact_paths(work_dir: Path, value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: artifact_paths must be a bounded list")
    validated: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 1024:
            raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: artifact path is invalid")
        relative = Path(item)
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: artifact paths must be workspace-relative")
        resolved = (work_dir / relative).resolve()
        if not is_within(resolved, work_dir) or not resolved.is_file():
            raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: artifact path is unavailable")
        validated.append(relative.as_posix())
    return validated


def _validate_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or not set(value).issubset(COUNT_KEYS):
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: counts contain unsupported fields")
    if not all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in value.values()):
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: counts must be non-negative integers")
    return dict(value)


def _validate_transitions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 32:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: queue_transitions must be a bounded list")
    validated: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"from", "to", "count"}:
            raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: queue transition shape is invalid")
        if item["from"] not in QUEUE_NAMES or item["to"] not in QUEUE_NAMES:
            raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: queue transition is unsupported")
        if not isinstance(item["count"], int) or isinstance(item["count"], bool) or item["count"] < 0:
            raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: queue transition count is invalid")
        validated.append(dict(item))
    return validated


def validate_control_envelope(
    work_dir: Path,
    source: Path,
    *,
    expected_role: str | None = None,
    expected_stage: str | None = None,
) -> dict[str, Any]:
    """Validate and return only the parent-safe worker result fields."""

    require_control_plane_ready(work_dir)
    resolved = source.resolve()
    if not is_within(resolved, work_dir) or not resolved.is_file():
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: envelope must be a workspace file")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    required = {
        "schema",
        "worker_role",
        "stage",
        "status",
        "target_ids",
        "counts",
        "issue_codes",
        "artifact_paths",
        "queue_transitions",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: envelope contains missing or parent-unsafe fields"
        )
    if payload["schema"] != CONTROL_ENVELOPE_SCHEMA:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: unsupported envelope schema")
    role = payload["worker_role"]
    stage = payload["stage"]
    status = payload["status"]
    if role not in WORKER_ROLES or stage not in ROLE_STAGES.get(role, set()):
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: worker role/stage mismatch")
    if expected_role is not None and role != expected_role:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: unexpected worker role")
    if expected_stage is not None and stage != expected_stage:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: unexpected worker stage")
    if status not in CONTROL_STATUSES:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: unsupported categorical status")
    target_ids = _validate_target_ids(payload["target_ids"])
    counts = _validate_counts(payload["counts"])
    issue_codes = payload["issue_codes"]
    if (
        not isinstance(issue_codes, list)
        or len(issue_codes) > 16
        or not all(isinstance(code, str) for code in issue_codes)
        or not set(issue_codes).issubset(ISSUE_CODES_BY_STAGE[stage])
    ):
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: issue_codes contain prose or an unauthorized semantic category"
        )
    if status == "refused" and "provider_refusal" not in issue_codes:
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: refused status requires provider_refusal"
        )
    artifact_paths = _validate_artifact_paths(work_dir, payload["artifact_paths"])
    transitions = _validate_transitions(payload["queue_transitions"])
    return {
        "schema": CONTROL_ENVELOPE_SCHEMA,
        "worker_role": role,
        "stage": stage,
        "status": status,
        "target_ids": target_ids,
        "counts": counts,
        "issue_codes": list(issue_codes),
        "artifact_paths": artifact_paths,
        "queue_transitions": transitions,
    }
