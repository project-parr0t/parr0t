from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .privacy import is_within


CONTROL_ENVELOPE_SCHEMA = "parr0t.control-envelope.v2"
REVIEW_CONTROL_CONTRACT_SCHEMA = "parr0t.review-control-contract.v1"
REVIEW_CONTROL_MODE = "deterministic_review_router"
REVIEW_CONTROL_ALLOWED_ACTIONS = (
    "read_status",
    "prepare_validated_manifest",
    "dispatch_isolated_wave_relay",
    "wait_for_active_wave",
    "validate_control_envelope",
    "import_completed_wave",
    "aggregate_completed_pass",
    "route_categorical_status",
    "record_aggregate_usage",
    "checkpoint_and_stop",
)


def review_control_contract() -> dict[str, Any]:
    """Return the immutable, text-free contract for review orchestration."""

    return {
        "schema": REVIEW_CONTROL_CONTRACT_SCHEMA,
        "mode": REVIEW_CONTROL_MODE,
        "allowed_actions": list(REVIEW_CONTROL_ALLOWED_ACTIONS),
        "allowed_decision_inputs": [
            "validated_status.next_actions",
            "validated_native_manifest",
            "validated_control_envelope",
            "aggregate_usage_counters",
            "explicit_user_approval",
        ],
        "semantic_access_allowed": False,
        "free_form_planning_allowed": False,
        "ad_hoc_contracts_allowed": False,
        "autonomous_resume_allowed": False,
        "autonomous_sdk_or_tool_changes_allowed": False,
        "model_choice_allowed": False,
        "batch_shape_change_allowed": False,
        "manifest_declared_review_failure_split": "native_pass_step_only",
        "review_failure_split_strategy": "failed_subset_binary",
        "successful_review_rows_reprocessed": False,
        "budget_inference_allowed": False,
        "reprocess_unchanged_success_allowed": False,
        "status_action_match_required": True,
        "native_step_command_required": True,
        "native_step_command": "game-l10n native-pass-step",
        "root_direct_semantic_worker_allowed": False,
        "semantic_worker_parent": "isolated_wave_relay",
        "relay_authority": "exact_dispatch_only",
        "model_selected_wave_or_recovery_allowed": False,
        "halted_state_allowed_actions": [
            "read_status",
            "record_aggregate_usage",
            "checkpoint_and_stop",
        ],
        "resume_requirements": [
            "explicit_user_approval",
            "complete_budget_contract",
            "regenerated_validated_manifest",
        ],
        "unknown_state_action": "checkpoint_and_stop",
        "next_action_source": "validated_status_or_manifest_only",
        "review_plan_source": "validated_manifest_only",
        "review_stage_order": [
            "deterministic_validation",
            "complete_low_cost_triage",
            "selective_strong_review",
            "flagged_only_rewrite",
            "review_fallback_for_failures_only",
        ],
        "strong_review_selection_inputs": [
            "triage_status",
            "manifest_high_risk_flag",
            "deterministic_sample",
        ],
        "unselected_candidate_requirement": "current_generation_triage_ok",
        "universal_strong_review_required": False,
    }


def validate_review_control_action(action: str) -> dict[str, Any]:
    """Authorize only a fixed review-router action; never invent a recovery step."""

    normalized = str(action).strip()
    if normalized not in REVIEW_CONTROL_ALLOWED_ACTIONS:
        raise ConfigurationError(
            "E_REVIEW_CONTROL_ACTION_DENIED: review control plane actions must be "
            "selected from the immutable allowlist; checkpoint and stop instead"
        )
    return {
        "schema": "parr0t.review-control-permit.v1",
        "mode": REVIEW_CONTROL_MODE,
        "action": normalized,
        "authorized": True,
        "unknown_state_action": "checkpoint_and_stop",
    }


LEGACY_CONTROL_ENVELOPE_SCHEMA = "parr0t.control-envelope.v1"
ROUTE_EVIDENCE_SCHEMA = "parr0t.route-evidence.v1"
CONTROL_STATE_SCHEMA = "parr0t.control-plane-state.v1"
CONTROL_STATE_PATH = ".game-l10n/control-plane-state.json"
CONTROL_ENTRY_SCHEMA = "parr0t.control-plane-entry.v1"
CONTROL_ENTRY_PATH = ".game-l10n/control-plane-entry.json"
CONTROL_HANDOFF_SCHEMA = "parr0t.control-plane-handoff.v1"
CONTROL_HANDOFF_PATH = ".game-l10n/control-plane-handoff.json"

_CONTROL_STATE_FIELDS = {
    "schema",
    "status",
    "generation",
    "incident_id",
    "issue_code",
    "incident_code",
}
_CONTROL_STATE_RESUMED_FIELDS = _CONTROL_STATE_FIELDS | {"resumed_from_incident"}

WORKER_ROLES = {
    "source_probe",
    "primary_inventory",
    "independent_inventory",
    "inventory_reconciler",
    "context_analyzer",
    "translator",
    "review_judge",
    "review_rewriter",
    "artifact_builder",
    "validator",
}

ROLE_STAGES = {
    "source_probe": {"discovery"},
    "primary_inventory": {"inventory"},
    "independent_inventory": {"inventory"},
    "inventory_reconciler": {"inventory"},
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
    "sdk_identity_mismatch",
}

DISCOVERY_ISSUE_CODES = {
    "existing_target_locale_present",
    "source_identity_unresolved",
    "sdk_selection_incomplete",
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
    "discovery": COMMON_ISSUE_CODES | DISCOVERY_ISSUE_CODES,
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


def initial_control_plane_entry_state() -> dict[str, Any]:
    return {
        "schema": CONTROL_ENTRY_SCHEMA,
        "status": "pending",
        "issue_code": "control_plane_entry_required",
    }


def control_plane_entry_state(work_dir: Path) -> dict[str, Any]:
    path = work_dir / CONTROL_ENTRY_PATH
    if not path.is_file():
        return initial_control_plane_entry_state()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "status",
        "issue_code",
    }:
        raise ConfigurationError(
            "E_CONTROL_PLANE_ENTRY_STATE: state contains unsupported fields"
        )
    if payload.get("schema") != CONTROL_ENTRY_SCHEMA:
        raise ConfigurationError("E_CONTROL_PLANE_ENTRY_STATE: unsupported schema")
    status = payload.get("status")
    issue_code = payload.get("issue_code")
    if status not in {"pending", "ready"}:
        raise ConfigurationError("E_CONTROL_PLANE_ENTRY_STATE: invalid status")
    if issue_code != ("control_plane_entry_required" if status == "pending" else ""):
        raise ConfigurationError("E_CONTROL_PLANE_ENTRY_STATE: invalid issue code")
    return dict(payload)


def public_control_plane_entry_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": state["schema"],
        "status": state["status"],
        "issue_code": state["issue_code"],
    }


def require_control_plane_entry(work_dir: Path) -> dict[str, Any]:
    state = control_plane_entry_state(work_dir)
    if state["status"] != "ready":
        raise ConfigurationError(
            "E_CONTROL_PLANE_ENTRY_REQUIRED: read the required control-plane contracts "
            "and establish a text-free control plane before any game-content operation"
        )
    return state


def establish_control_plane_entry(work_dir: Path) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    state = {
        "schema": CONTROL_ENTRY_SCHEMA,
        "status": "ready",
        "issue_code": "",
    }
    _atomic_json(work_dir / CONTROL_ENTRY_PATH, state)
    return state


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
    if not isinstance(payload, dict):
        raise ConfigurationError("E_CONTROL_PLANE_STATE: state must be an object")
    status = payload.get("status")
    allowed_shapes = [_CONTROL_STATE_FIELDS]
    if status == "ready":
        allowed_shapes.append(_CONTROL_STATE_RESUMED_FIELDS)
    if not any(set(payload) == shape for shape in allowed_shapes):
        raise ConfigurationError("E_CONTROL_PLANE_STATE: state contains unsupported fields")
    if payload.get("schema") != CONTROL_STATE_SCHEMA:
        raise ConfigurationError("E_CONTROL_PLANE_STATE: unsupported state schema")
    if status not in {"ready", "rotation_required"}:
        raise ConfigurationError("E_CONTROL_PLANE_STATE: invalid state status")
    if not isinstance(payload.get("generation"), int) or payload["generation"] < 1:
        raise ConfigurationError("E_CONTROL_PLANE_STATE: invalid generation")
    for field in ("incident_id", "issue_code", "incident_code"):
        if not isinstance(payload.get(field), str):
            raise ConfigurationError("E_CONTROL_PLANE_STATE: invalid categorical state field")
    if status == "rotation_required":
        if (
            len(payload["incident_id"]) != 32
            or any(character not in "0123456789abcdef" for character in payload["incident_id"])
            or payload["issue_code"] != "control_plane_leak"
            or payload["incident_code"] not in LEAK_INCIDENT_CODES
        ):
            raise ConfigurationError("E_CONTROL_PLANE_STATE: invalid rotation marker")
    elif payload["incident_id"] or payload["issue_code"] or payload["incident_code"]:
        raise ConfigurationError("E_CONTROL_PLANE_STATE: ready state contains an incident")
    if "resumed_from_incident" in payload:
        resumed = payload["resumed_from_incident"]
        if (
            not isinstance(resumed, str)
            or len(resumed) != 32
            or any(character not in "0123456789abcdef" for character in resumed)
        ):
            raise ConfigurationError("E_CONTROL_PLANE_STATE: invalid resumed incident marker")
    return dict(payload)


def public_control_plane_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return the fixed parent-safe projection of an already validated state."""

    return {
        "schema": state["schema"],
        "status": state["status"],
        "generation": state["generation"],
        "issue_code": state["issue_code"],
        "incident_code": state["incident_code"],
    }


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
        if not (work_dir / CONTROL_HANDOFF_PATH).is_file():
            _write_control_plane_handoff(work_dir, state)
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
    _atomic_json(work_dir / CONTROL_ENTRY_PATH, initial_control_plane_entry_state())
    _write_control_plane_handoff(work_dir, contaminated)
    return contaminated


def _control_handoff_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _write_control_plane_handoff(
    work_dir: Path, state: dict[str, Any]
) -> dict[str, Any]:
    payload = {
        "schema": CONTROL_HANDOFF_SCHEMA,
        "status": "rotation_required",
        "from_generation": state["generation"],
        "to_generation": state["generation"] + 1,
        "incident_id": state["incident_id"],
        "issue_code": "control_plane_leak",
    }
    receipt = {**payload, "digest": _control_handoff_digest(payload)}
    _atomic_json(work_dir / CONTROL_HANDOFF_PATH, receipt)
    return receipt


def _validated_control_plane_handoff(
    work_dir: Path, handoff_path: Path
) -> dict[str, Any]:
    expected = (work_dir / CONTROL_HANDOFF_PATH).resolve()
    if handoff_path.resolve() != expected:
        raise ConfigurationError(
            "E_CONTROL_PLANE_HANDOFF_REQUIRED: resume accepts only the canonical "
            "core-generated metadata receipt"
        )
    try:
        receipt = json.loads(expected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "E_CONTROL_PLANE_HANDOFF_REQUIRED: canonical handoff receipt is unavailable"
        ) from exc
    fields = {
        "schema",
        "status",
        "from_generation",
        "to_generation",
        "incident_id",
        "issue_code",
        "digest",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise ConfigurationError(
            "E_CONTROL_PLANE_HANDOFF_INVALID: handoff receipt shape is invalid"
        )
    unsigned = {key: receipt[key] for key in fields if key != "digest"}
    if (
        receipt.get("schema") != CONTROL_HANDOFF_SCHEMA
        or receipt.get("status") != "rotation_required"
        or receipt.get("issue_code") != "control_plane_leak"
        or not isinstance(receipt.get("from_generation"), int)
        or isinstance(receipt.get("from_generation"), bool)
        or receipt["from_generation"] < 1
        or receipt.get("to_generation") != receipt["from_generation"] + 1
        or not isinstance(receipt.get("incident_id"), str)
        or len(receipt["incident_id"]) != 32
        or any(
            character not in "0123456789abcdef"
            for character in receipt["incident_id"]
        )
        or receipt.get("digest") != _control_handoff_digest(unsigned)
    ):
        raise ConfigurationError(
            "E_CONTROL_PLANE_HANDOFF_INVALID: handoff receipt binding is invalid"
        )
    return receipt


def _reset_native_control_observations(work_dir: Path) -> None:
    """Reset only generation-local polling state after a validated rotation."""

    control_dir = work_dir / ".game-l10n" / "native" / "control"
    if not control_dir.is_dir():
        return
    for path in control_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("schema") not in {
            "parr0t.native-control-state.v1",
            "parr0t.native-control-state.v2",
            "parr0t.native-control-state.v3",
        }:
            continue
        required = {
            "control_steps_in_wave",
            "last_progress_marker",
            "unchanged_observations",
            "last_action",
        }
        if not required.issubset(payload):
            continue
        payload["control_steps_in_wave"] = 0
        payload["last_progress_marker"] = ""
        payload["unchanged_observations"] = 0
        payload["last_action"] = ""
        _atomic_json(path, payload)


def acknowledge_control_plane_rotation(
    work_dir: Path, handoff_path: Path
) -> dict[str, Any]:
    state = control_plane_state(work_dir)
    if state["status"] != "rotation_required":
        raise ConfigurationError("E_CONTROL_PLANE_STATE: no rotation is pending")
    if not (work_dir / CONTROL_HANDOFF_PATH).is_file():
        _write_control_plane_handoff(work_dir, state)
    receipt = _validated_control_plane_handoff(work_dir, handoff_path)
    if (
        receipt["incident_id"] != state["incident_id"]
        or receipt["from_generation"] != state["generation"]
        or receipt["to_generation"] != state["generation"] + 1
    ):
        raise ConfigurationError(
            "E_CONTROL_PLANE_HANDOFF_INVALID: receipt does not match pending rotation"
        )
    incident_id = receipt["incident_id"]
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
    _atomic_json(work_dir / CONTROL_ENTRY_PATH, initial_control_plane_entry_state())
    _reset_native_control_observations(work_dir)
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


def control_target_id(target_id: str) -> str:
    """Create a deterministic parent-safe alias without exposing the data-plane ID."""

    return "ctl_" + hashlib.sha256(target_id.encode("utf-8")).hexdigest()


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
        "target_ids": [control_target_id(target_id) for target_id in target_ids],
        "counts": normalized_counts,
        "issue_codes": normalized_issue_codes,
        "artifact_paths": artifact_paths,
        "queue_transitions": transitions,
        "route_evidence_path": route_evidence_path,
    }
