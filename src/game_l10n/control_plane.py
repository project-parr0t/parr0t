from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .privacy import is_within


CONTROL_ENVELOPE_SCHEMA = "parr0t.control-envelope.v2"
LEGACY_CONTROL_ENVELOPE_SCHEMA = "parr0t.control-envelope.v1"
ROUTE_EVIDENCE_SCHEMA = "parr0t.route-evidence.v1"
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

CONTROL_STATUSES = {
    "success",
    "partial",
    "fallback_required",
    "error",
    "invalid",
}
LEGACY_CONTROL_STATUSES = CONTROL_STATUSES | {"refused"}

COUNT_KEYS = {
    "total",
    "records",
    "success",
    "partial",
    "refused",
    "fallback_required",
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

ROUTING_ISSUE_CODES = {"fallback_required"}
LEGACY_PROVIDER_ISSUE_CODES = {"provider_refusal"}

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
    "context": COMMON_ISSUE_CODES | ROUTING_ISSUE_CODES | CONTEXT_ISSUE_CODES,
    "translation": COMMON_ISSUE_CODES | ROUTING_ISSUE_CODES | TRANSLATION_ISSUE_CODES,
    # Detailed review findings (meaning, voice, omission, tone, and similar
    # judgments) stay in the private data-plane artifact. The parent needs only
    # the target IDs and queue transition that route a rewrite.
    "review": COMMON_ISSUE_CODES | ROUTING_ISSUE_CODES,
    "rewrite": COMMON_ISSUE_CODES | ROUTING_ISSUE_CODES | TRANSLATION_ISSUE_CODES,
    "application": COMMON_ISSUE_CODES | APPLICATION_ISSUE_CODES,
    "validation": COMMON_ISSUE_CODES | VALIDATION_ISSUE_CODES,
}

ROUTES_BY_STAGE = {
    "context": "context_analyzers",
    "translation": "translators",
    "review": "reviewers",
    "rewrite": "rewriters",
}

TERMINAL_ROUTE_STATUSES = {
    "provider_refusal",
    "malformed_response",
    "provider_error",
    "rate_limited",
    "quota_exhausted",
    "authentication_error",
    "timeout",
    "integrity_invalid",
    "budget_exhausted",
    "budget_accounting_uncertain",
}

FALLBACK_QUEUES = {
    "context_pending",
    "translation_fallback_pending",
    "review_fallback_pending",
    "rewrite_pending",
    "operational_failure",
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


def _validate_route_evidence(
    work_dir: Path,
    relative_path: str,
    *,
    stage: str,
    envelope_target_ids: list[str],
    source_preserved_count: int,
    artifact_paths: list[str],
) -> str:
    if not isinstance(relative_path, str) or not relative_path:
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_REQUIRED: source preservation requires "
            "metadata-only route evidence"
        )
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: route evidence path must be workspace-relative"
        )
    normalized = relative.as_posix()
    if normalized not in artifact_paths:
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: route evidence must be an envelope artifact"
        )
    resolved = (work_dir / relative).resolve()
    if not is_within(resolved, work_dir) or not resolved.is_file():
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: route evidence is unavailable"
        )
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "stage",
        "target_ids",
        "attempts",
    }:
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: route evidence shape is invalid"
        )
    if payload["schema"] != ROUTE_EVIDENCE_SCHEMA or payload["stage"] != stage:
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: route evidence schema or stage is invalid"
        )
    target_ids = _validate_target_ids(payload["target_ids"])
    if len(target_ids) != source_preserved_count:
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: terminal target count does not match source preservation"
        )
    if not set(target_ids).issubset(envelope_target_ids):
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: terminal targets are outside the envelope"
        )

    config_path = work_dir / "game-l10n.json"
    if not config_path.is_file():
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: workspace routing configuration is missing"
        )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    route_key = ROUTES_BY_STAGE.get(stage)
    expected_routes = config.get("routing", {}).get(route_key, []) if route_key else []
    if (
        not isinstance(expected_routes, list)
        or not expected_routes
        or not all(isinstance(route, str) and route for route in expected_routes)
        or len(expected_routes) != len(set(expected_routes))
    ):
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: configured route chain is unavailable"
        )

    attempts = payload["attempts"]
    if not isinstance(attempts, list) or len(attempts) > 16000:
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: attempts must be a bounded list"
        )
    by_target: dict[str, dict[str, dict[str, Any]]] = {
        target_id: {} for target_id in target_ids
    }
    for attempt in attempts:
        if not isinstance(attempt, dict) or set(attempt) != {
            "target_id",
            "route",
            "status",
            "attempts",
        }:
            raise ConfigurationError(
                "E_CONTROL_ROUTE_EVIDENCE_INVALID: attempt shape is invalid"
            )
        target_id = attempt["target_id"]
        route = attempt["route"]
        status = attempt["status"]
        attempt_count = attempt["attempts"]
        if target_id not in by_target or route not in expected_routes:
            raise ConfigurationError(
                "E_CONTROL_ROUTE_EVIDENCE_INVALID: attempt target or route is unexpected"
            )
        if route in by_target[target_id]:
            raise ConfigurationError(
                "E_CONTROL_ROUTE_EVIDENCE_INVALID: route outcome is duplicated"
            )
        if status not in TERMINAL_ROUTE_STATUSES:
            raise ConfigurationError(
                "E_CONTROL_ROUTE_EVIDENCE_INVALID: route did not end in a terminal failure"
            )
        if (
            not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 1
        ):
            raise ConfigurationError(
                "E_CONTROL_ROUTE_EVIDENCE_INVALID: every route requires an actual attempt"
            )
        by_target[target_id][route] = attempt

    required = set(expected_routes)
    if any(set(route_outcomes) != required for route_outcomes in by_target.values()):
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INCOMPLETE: every configured route must be exhausted"
        )
    return normalized


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
    if not isinstance(payload, dict):
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: envelope contains missing or parent-unsafe fields"
        )
    schema = payload.get("schema")
    if schema == CONTROL_ENVELOPE_SCHEMA:
        required.add("route_evidence_path")
    elif schema != LEGACY_CONTROL_ENVELOPE_SCHEMA:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: unsupported envelope schema")
    if set(payload) != required:
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: envelope contains missing or parent-unsafe fields"
        )
    role = payload["worker_role"]
    stage = payload["stage"]
    status = payload["status"]
    if role not in WORKER_ROLES or stage not in ROLE_STAGES.get(role, set()):
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: worker role/stage mismatch")
    if expected_role is not None and role != expected_role:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: unexpected worker role")
    if expected_stage is not None and stage != expected_stage:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: unexpected worker stage")
    if schema == CONTROL_ENVELOPE_SCHEMA and status == "refused":
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: parent-facing refusals must be normalized to fallback_required"
        )
    allowed_statuses = (
        CONTROL_STATUSES if schema == CONTROL_ENVELOPE_SCHEMA else LEGACY_CONTROL_STATUSES
    )
    if status not in allowed_statuses:
        raise ConfigurationError("E_CONTROL_ENVELOPE_INVALID: unsupported categorical status")
    target_ids = _validate_target_ids(payload["target_ids"])
    counts = _validate_counts(payload["counts"])
    if schema == CONTROL_ENVELOPE_SCHEMA and "refused" in counts:
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: parent-facing refusal counts must use fallback_required"
        )
    issue_codes = payload["issue_codes"]
    if (
        not isinstance(issue_codes, list)
        or len(issue_codes) > 16
        or not all(isinstance(code, str) for code in issue_codes)
        or not set(issue_codes).issubset(
            ISSUE_CODES_BY_STAGE[stage]
            | (LEGACY_PROVIDER_ISSUE_CODES if schema == LEGACY_CONTROL_ENVELOPE_SCHEMA else set())
        )
    ):
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: issue_codes contain prose or an unauthorized semantic category"
        )
    if schema == CONTROL_ENVELOPE_SCHEMA and "provider_refusal" in issue_codes:
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: parent-facing refusals must be normalized to fallback_required"
        )
    if status == "fallback_required" and "fallback_required" not in issue_codes:
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: fallback_required status requires fallback_required"
        )
    if status == "refused" and "provider_refusal" not in issue_codes:
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: refused status requires provider_refusal"
        )
    artifact_paths = _validate_artifact_paths(work_dir, payload["artifact_paths"])
    transitions = _validate_transitions(payload["queue_transitions"])
    if status == "fallback_required":
        if not transitions or not any(
            transition["to"] in FALLBACK_QUEUES for transition in transitions
        ):
            raise ConfigurationError(
                "E_CONTROL_ENVELOPE_INVALID: fallback_required must advance to a fallback queue"
            )
        if any(transition["to"] == "source_preserved" for transition in transitions):
            raise ConfigurationError(
                "E_CONTROL_ENVELOPE_INVALID: fallback_required cannot preserve source"
            )

    source_preserved_count = sum(
        transition["count"]
        for transition in transitions
        if transition["to"] == "source_preserved"
    )
    declared_source_preserved = counts.get("source_preserved", 0)
    if (source_preserved_count or stage in ROUTES_BY_STAGE) and (
        source_preserved_count != declared_source_preserved
    ):
        raise ConfigurationError(
            "E_CONTROL_ENVELOPE_INVALID: source-preserved counts and transitions disagree"
        )
    route_evidence_path = ""
    if source_preserved_count:
        if schema != CONTROL_ENVELOPE_SCHEMA:
            raise ConfigurationError(
                "E_CONTROL_ROUTE_EVIDENCE_REQUIRED: legacy envelopes cannot terminate routes"
            )
        route_evidence_path = _validate_route_evidence(
            work_dir,
            payload["route_evidence_path"],
            stage=stage,
            envelope_target_ids=target_ids,
            source_preserved_count=source_preserved_count,
            artifact_paths=artifact_paths,
        )
    elif schema == CONTROL_ENVELOPE_SCHEMA and payload["route_evidence_path"]:
        raise ConfigurationError(
            "E_CONTROL_ROUTE_EVIDENCE_INVALID: nonterminal envelope must not claim route evidence"
        )

    normalized_status = "fallback_required" if status == "refused" else status
    normalized_issue_codes = [
        "fallback_required" if code == "provider_refusal" else code
        for code in issue_codes
    ]
    normalized_counts = dict(counts)
    if schema == LEGACY_CONTROL_ENVELOPE_SCHEMA and "refused" in normalized_counts:
        normalized_counts["fallback_required"] = normalized_counts.pop("refused")
    return {
        "schema": CONTROL_ENVELOPE_SCHEMA,
        "worker_role": role,
        "stage": stage,
        "status": normalized_status,
        "target_ids": target_ids,
        "counts": normalized_counts,
        "issue_codes": normalized_issue_codes,
        "artifact_paths": artifact_paths,
        "queue_transitions": transitions,
        "route_evidence_path": route_evidence_path,
    }
