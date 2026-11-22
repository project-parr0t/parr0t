from __future__ import annotations

import json
import hashlib
import math
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from .control_plane import (
    CONTROL_ENVELOPE_SCHEMA,
    require_control_plane_ready,
    review_control_contract,
)
from .content_source_policy import (
    content_source_policy,
    validate_content_source_policy,
    validate_output_provenance,
)
from .language_policy import DEFAULT_TARGET_LANGUAGE, language_policy_digest
from .models import Segment
from .native_followup import CANDIDATE_METADATA_KEY
from .native_runtime import (
    RUNTIME_ISSUES,
    _load_launch_evidence,
    _preserve_legacy_launch_fields,
    _relay_command_argv,
    _relay_worker_dispatches,
    _unstarted_launch_ledger,
    launch_contract,
    native_launch_summary,
    native_runtime,
    require_native_workers_ended,
)
from .omp_isolation import ISOLATION_ISSUE, runtime_isolation, tool_transport_contract
from .store import stable_hash, stable_hash_sequence
from .review_policy import _additional_fallback_review_budget, judge_calls, review_call_limit, review_worker_limit
from .validators import validate_integrity


ALLOWED_STATUSES = {"success", "refused", "error", "invalid"}
SAFE_NAME = re.compile(r"[^a-z0-9._-]+")
TASK_MANIFEST_VERSION = 7
LEGACY_WORKER_SPLIT_MANIFEST_VERSION = 6
SUPPORTED_TASK_MANIFEST_VERSIONS = {
    4,
    5,
    LEGACY_WORKER_SPLIT_MANIFEST_VERSION,
    TASK_MANIFEST_VERSION,
}
TASK_PAYLOAD_SCHEMA = "parr0t.native-task-payload.v1"
DEFAULT_MAX_RECORDS = 64
DEFAULT_MIN_RECORDS = 8
DEFAULT_MAX_MATERIAL_TOKENS = 8_000
DEFAULT_REVIEW_MAX_RECORDS = 256
DEFAULT_REVIEW_MIN_RECORDS = 64
DEFAULT_REVIEW_MAX_MATERIAL_TOKENS = 65_536
DEFAULT_REVIEW_SPLIT_MAX_DEPTH = 11
DEFAULT_REVIEW_SPLIT_MAX_CALLS = 16
DEFAULT_MAX_WORKERS = 8
PASS_KINDS = {"translation", "review", "rewrite"}
NATIVE_CONTROL_STATE_SCHEMA = "parr0t.native-control-state.v3"
LEGACY_NATIVE_CONTROL_STATE_SCHEMA = "parr0t.native-control-state.v1"
LEGACY_NATIVE_CONTROL_STATE_SCHEMA_V2 = "parr0t.native-control-state.v2"
NATIVE_CONTROL_STEP_SCHEMA = "parr0t.native-control-step.v1"
NATIVE_WAVE_RECEIPT_SCHEMA = "parr0t.native-wave-receipt.v1"
NATIVE_WAVE_RELAY_SCHEMA = "parr0t.native-wave-relay.v1"
NATIVE_WAVE_RELAY_RECEIPT_SCHEMA = "parr0t.native-wave-relay-receipt.v1"
NATIVE_REVIEW_SPLIT_CHAIN_SCHEMA = "parr0t.native-review-split-chain.v1"
INTERRUPTED_REWRITE_RECOVERY = "operator_confirmed_workers_stopped"
USAGE_EVENT = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
RESULT_VALIDATION_REVISION = 5
RELAY_DISPATCH_REVISION = 2
LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*")
OPERATIONAL_RESULT_ISSUES = {
    "worker_ended_without_result",
    "worker_result_binding_invalid",
    "worker_result_contract_invalid",
    "worker_result_missing_target",
    "worker_result_provider_mismatch",
    "worker_result_schema_invalid",
    "worker_not_started",
    "worker_interrupted",
}


def _normalized_language_binding(binding: dict[str, Any] | None) -> dict[str, Any]:
    if binding is None:
        default_policy = {
            "target_language": DEFAULT_TARGET_LANGUAGE,
            "revision": 0,
            "authorization": "default_korean",
        }
        return {
            "source_language": "und",
            "target_language": DEFAULT_TARGET_LANGUAGE,
            "policy_revision": 0,
            "policy_digest": language_policy_digest(default_policy),
        }
    required = {
        "source_language",
        "target_language",
        "policy_revision",
        "policy_digest",
    }
    if not isinstance(binding, dict) or set(binding) != required:
        raise ValueError("E_TARGET_LANGUAGE_BINDING_INVALID")
    source = str(binding.get("source_language", "")).strip().casefold().replace("_", "-")
    target = str(binding.get("target_language", "")).strip().casefold().replace("_", "-")
    revision = binding.get("policy_revision")
    digest = binding.get("policy_digest")
    if (
        not LANGUAGE_TAG.fullmatch(source)
        or not LANGUAGE_TAG.fullmatch(target)
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError("E_TARGET_LANGUAGE_BINDING_INVALID")
    return {
        "source_language": source,
        "target_language": target,
        "policy_revision": revision,
        "policy_digest": digest,
    }


def _failure_split_contract(
    pass_kind: str, *, manifest_version: int = TASK_MANIFEST_VERSION,
    max_depth: int = DEFAULT_REVIEW_SPLIT_MAX_DEPTH,
) -> dict[str, Any]:
    enabled = pass_kind == "review"
    core_owned = enabled and manifest_version >= TASK_MANIFEST_VERSION
    contract = {
        "enabled": enabled,
        "strategy": "failed_subset_binary" if enabled else "none",
        "triggers": (
            [
                "batch_refused",
                "batch_error",
                "malformed_or_incomplete_result",
                "target_refused_or_error",
            ]
            if enabled
            else []
        ),
        "successful_rows_locked": enabled,
        "retry_successful_rows": False,
        "max_depth": max_depth if enabled else 0,
        "max_semantic_calls": (
            1
            if core_owned
            else DEFAULT_REVIEW_SPLIT_MAX_CALLS if enabled else 1
        ),
        "terminal_action": (
            "emit_failed_rows_for_configured_review_fallback"
            if enabled
            else "emit_failed_rows"
        ),
    }
    if manifest_version >= TASK_MANIFEST_VERSION:
        contract["owner"] = "deterministic_core" if enabled else "none"
    return contract


def _worker_contract(
    pass_kind: str = "translation", *, manifest_version: int = TASK_MANIFEST_VERSION,
    max_depth: int = DEFAULT_REVIEW_SPLIT_MAX_DEPTH,
) -> dict[str, Any]:
    split_contract = _failure_split_contract(
        pass_kind, manifest_version=manifest_version, max_depth=max_depth
    )
    worker_owned_split = split_contract["enabled"] and (
        split_contract.get("owner", "semantic_worker") == "semantic_worker"
    )
    return {
        "pass_kind": pass_kind,
        "history_mode": "none",
        "fresh_worker_per_task": True,
        "max_followup_tasks": 0,
        "max_agent_depth": 1,
        "delegation_allowed": False,
        "model_controller_role_allowed": False,
        "max_semantic_model_calls_per_task": split_contract["max_semantic_calls"],
        "per_record_semantic_call_loop_allowed": False,
        "max_prompt_artifacts_per_task": 1,
        "task_payload_schema": TASK_PAYLOAD_SCHEMA,
        "task_payload_contains_bound_segments": True,
        "target_language_source": "task_row.language_binding.target_language",
        "target_language_change_allowed": False,
        "full_source_artifact_access": "validation_only",
        "single_batched_result_required": True,
        "max_parent_progress_messages": 0,
        "task_message_contains_game_text": False,
        "parent_result": "metadata_only",
        "control_envelope_schema": CONTROL_ENVELOPE_SCHEMA,
        "worker_final_response": "workspace_relative_envelope_path_only",
        "free_form_parent_response_allowed": False,
        "direct_parent_role": "isolated_wave_relay",
        "root_direct_parent_allowed": False,
        "failure_split": split_contract,
        "failure_result_proof_fields": (
            ["split_depth", "semantic_attempts"] if worker_owned_split else []
        ),
    }


def _result_contract() -> dict[str, Any]:
    """Canonical worker output contract, kept free of game text."""

    return {
        "schema": "parr0t.native-result-jsonl.v1",
        "format": "jsonl",
        "one_row_per_bound_target": True,
        "ordered_like_task": True,
        "canonical_fields": [
            "target_id",
            "status",
            "text",
            "provider",
            "issues",
            "content_provenance",
        ],
        "required_fields": ["target_id", "status", "text"],
        "optional_fields": [
            "provider",
            "issues",
            "content_provenance",
            "split_depth",
            "semantic_attempts",
        ],
        "status_values": sorted(ALLOWED_STATUSES),
        "success_text_required_unless_source_authorizes_empty": True,
        "non_success_text_must_be_empty": True,
        "route_bound_success_provider": "route_binding.provider",
        "content_provenance_schema": "parr0t.content-provenance.v1",
        "missing_content_provenance_action": "reject",
        "exact_runtime_model_field": "route_binding.model",
        "provider_route_alias_field": "route_binding.provider",
        "unauthorized_external_provenance_action": "reject",
        "alternate_text_fields_allowed": False,
    }


def _detail_contract(
    pass_kind: str = "translation", *, manifest_version: int = TASK_MANIFEST_VERSION,
    max_depth: int = DEFAULT_REVIEW_SPLIT_MAX_DEPTH,
) -> dict[str, Any]:
    split_contract = _failure_split_contract(
        pass_kind, manifest_version=manifest_version, max_depth=max_depth
    )
    return {
        "review_control": review_control_contract(),
        "schema": "parr0t.detail-contract.v1",
        "authority": "supplemental_only",
        "canonical_source": "native_task_manifest",
        "free_form_execution_contract_allowed": False,
        "incident_history_allowed": False,
        "may_narrow": [
            "bound_target_ids",
            "input_paths",
            "output_paths",
            "acceptance_checks",
            "limits_within_parent_budget",
        ],
        "must_not_override": [
            "user_scope",
            "core_contracts",
            "control_plane_boundary",
            "provider_mode",
            "pipeline_order",
            "manifest_budgets",
            "authorization_boundaries",
            "checkpoint_reuse",
            "target_language_binding",
        ],
        "conflict_action": "reject_before_dispatch",
        "pass_kind": pass_kind,
        "one_initial_semantic_batch_per_task": True,
        "additional_semantic_calls": (
            "none"
            if split_contract.get("owner") == "deterministic_core"
            else "failed_subset_binary_split_only"
            if split_contract["enabled"]
            else "none"
        ),
        "per_record_semantic_call_loop_allowed": False,
        "shared_context_loads_per_task": 1,
        "task_input": "bound_segment_payload_only",
        "candidate_binding": {
            "required": pass_kind in {"review", "rewrite"},
            "metadata_key": (
                CANDIDATE_METADATA_KEY
                if pass_kind in {"review", "rewrite"}
                else ""
            ),
            "text_field": "text",
            "generation_field": "generation",
            "digest_field": "digest",
            "source_role": "semantic_reference",
            "candidate_role": (
                "review_subject"
                if pass_kind == "review"
                else "rewrite_subject" if pass_kind == "rewrite" else "none"
            ),
        },
        "full_source_artifact_access": "validation_only",
        "result_cardinality": "one_status_per_bound_target",
        "partial_failure_isolation": (
            "core_locks_successes_then_emits_failed_subset_manifest"
            if split_contract.get("owner") == "deterministic_core"
            else "lock_successes_then_binary_split_failed_subset"
            if split_contract["enabled"]
            else "per_target_status_in_same_batched_result"
        ),
        "failure_split": split_contract,
        "unchanged_success_reprocessing_allowed": False,
        "budget_exhaustion_action": "checkpoint_and_return_control",
    }


def _execution_contract(
    task_count: int,
    max_workers: int,
    record_count: int,
    *,
    pass_kind: str,
    max_elapsed_minutes: int | None,
    max_input_tokens: int | None,
    max_provider_cost_usd: float | None,
    provider_cost_unmetered: bool,
    user_approved_budget: bool,
    max_root_model_calls_per_wave: int | None,
    max_root_model_calls_per_turn: int | None,
    max_root_input_tokens_per_turn: int | None,
    control_turn_budget_user_approved: bool,
    review_split_max_depth: int = DEFAULT_REVIEW_SPLIT_MAX_DEPTH,
    review_split_max_record_amplification: float | None = None,
    review_tier: str | None = None,
) -> dict[str, Any]:
    budget_approved = bool(
        user_approved_budget and any(
            value is not None
            for value in (max_elapsed_minutes, max_input_tokens, max_provider_cost_usd)
        )
    )
    control_turn_budget_active = bool(
        control_turn_budget_user_approved
        and any(
            value is not None
            for value in (
                max_root_model_calls_per_wave,
                max_root_model_calls_per_turn,
                max_root_input_tokens_per_turn,
            )
        )
    )
    return {
        "pass_kind": pass_kind,
        "review_tier": review_tier,
        "strategy": "direct_single_task" if task_count == 1 else "bounded_slot_queue",
        "orchestration_owner": "deterministic_core_via_isolated_wave_relay",
        "model_controller_allowed": False,
        "max_agent_depth": 2,
        "worker_delegation_allowed": False,
        "root_direct_semantic_worker_allowed": False,
        "root_dispatch_role": "isolated_wave_relay",
        "semantic_worker_parent": "isolated_wave_relay",
        "relay_history_mode": "none",
        "relay_delegation_allowed": True,
        "relay_model_controller_allowed": False,
        "relay_planning_allowed": False,
        "relay_retry_allowed": False,
        "relay_route_change_allowed": False,
        "relay_final_response": "workspace_relative_relay_receipt_path_only",
        "max_parallel_workers": max_workers,
        "max_tasks_per_wave": max_workers,
        "estimated_waves": (task_count + max_workers - 1) // max_workers,
        "control_turn_budget_user_approved": control_turn_budget_active,
        "max_root_model_calls_per_wave": (
            max_root_model_calls_per_wave if control_turn_budget_active else None
        ),
        "max_root_model_calls_per_turn": (
            max_root_model_calls_per_turn if control_turn_budget_active else None
        ),
        "max_root_input_tokens_per_turn": (
            max_root_input_tokens_per_turn if control_turn_budget_active else None
        ),
        "max_root_progress_messages_per_wave": 0,
        "budget_exhaustion_action": (
            "checkpoint_and_return_control"
            if control_turn_budget_active
            else "continue_until_terminal_or_actionable_blocker"
        ),
        "self_inferred_control_turn_limit_allowed": False,
        "control_turn_limit_signal_authority": (
            "deterministic_core_with_user_approved_limit"
        ),
        "contract_reload_policy": "digest_change_or_new_control_generation_only",
        "job_local_controller_allowed": False,
        "task_replanning_allowed": False,
        "new_proof_layers_allowed": False,
        "aggregate_once_per_wave": True,
        "progress_reporting": "completion_checkpoint_or_blocker_only",
        "capacity_failure_action": "defer_task_and_end_worker_attempt",
        "automatic_root_turn_restart_allowed": False,
        "detail_contract_schema": "parr0t.detail-contract.v1",
        "bulk_budget_gate": {
            "active": budget_approved,
            "pilot_waves_without_approved_budget": 0,
            "user_approved": budget_approved,
            "max_elapsed_minutes": max_elapsed_minutes,
            "max_input_tokens": max_input_tokens,
            "max_provider_cost_usd": max_provider_cost_usd,
            "provider_cost_unmetered": provider_cost_unmetered,
            "missing_budget_action": "continue_without_inferred_limit",
        },
        "review_optimization_policy": {
            "max_depth": review_split_max_depth,
            "max_record_amplification": review_split_max_record_amplification,
        },
    }


def _batch_fits(
    batch: list[tuple[int, Segment, int]],
    *,
    max_records: int,
    max_material_tokens: int,
) -> bool:
    if not batch or len(batch) > max_records:
        return False
    oversized = [item for item in batch if item[2] > max_material_tokens]
    if oversized:
        return len(batch) == 1
    return sum(item[2] for item in batch) <= max_material_tokens


def _rebalance_small_tail(
    batches: list[list[tuple[int, Segment, int]]],
    *,
    min_records: int,
    max_records: int,
    max_material_tokens: int,
) -> None:
    """Avoid a tiny final worker task when two bounded tasks can be balanced."""

    if len(batches) < 2 or len(batches[-1]) >= min_records:
        return
    combined = batches[-2] + batches[-1]
    choices: list[tuple[int, int, int, int]] = []
    for split in range(1, len(combined)):
        left = combined[:split]
        right = combined[split:]
        if not _batch_fits(
            left,
            max_records=max_records,
            max_material_tokens=max_material_tokens,
        ) or not _batch_fits(
            right,
            max_records=max_records,
            max_material_tokens=max_material_tokens,
        ):
            continue
        smallest = min(len(left), len(right))
        choices.append(
            (
                0 if smallest >= min_records else 1,
                abs(len(left) - len(right)),
                -smallest,
                split,
            )
        )
    if choices:
        split = min(choices)[-1]
        batches[-2:] = [combined[:split], combined[split:]]


def _atomic_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Native shard row {line_number} must be an object")
            rows.append(value)
    return rows


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_contract_payload(
    manifest: dict[str, Any], *, include_content_source_policy: bool
) -> dict[str, Any]:
    payload = {
        "worker_contract": manifest.get("worker_contract"),
        "detail_contract": manifest.get("detail_contract"),
        "execution_contract": manifest.get("execution_contract"),
    }
    if manifest.get("result_contract") is not None:
        payload["result_contract"] = manifest["result_contract"]
    if manifest.get("route_binding") is not None:
        payload["route_binding"] = manifest["route_binding"]
    for field in ("translation_route_plan", "translation_predecessor"):
        if manifest.get(field) is not None:
            payload[field] = manifest[field]
    if "execution_namespace" in manifest:
        payload["execution_namespace"] = manifest["execution_namespace"]
    if manifest.get("language_binding") is not None:
        payload["language_binding"] = manifest["language_binding"]
    if (
        include_content_source_policy
        and manifest.get("content_source_policy") is not None
    ):
        payload["content_source_policy"] = manifest["content_source_policy"]
    return payload


def _bound_manifest_content_source_policy(
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    policy = manifest.get("content_source_policy")
    if (
        manifest.get("version", 0) < TASK_MANIFEST_VERSION
        or not isinstance(policy, dict)
        or manifest.get("contract_fingerprint")
        != stable_hash(
            _manifest_contract_payload(
                manifest, include_content_source_policy=True
            )
        )
    ):
        return None
    return policy


def _load_task_manifest(work_dir: Path, manifest_path: Path) -> dict[str, Any]:
    try:
        manifest_path.resolve().relative_to(work_dir.resolve())
    except ValueError as exc:
        raise ValueError("Native task manifest must stay inside the private workspace") from exc
    if not manifest_path.is_file():
        raise ValueError("Native task manifest does not exist")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(
            f"Native task manifest must be a supported version: {sorted(SUPPORTED_TASK_MANIFEST_VERSIONS)}"
        )
    tasks = payload.get("tasks")
    version = payload.get("version")
    if version not in SUPPORTED_TASK_MANIFEST_VERSIONS or not isinstance(tasks, list) or not tasks:
        raise ValueError(
            f"Native task manifest must be a supported version: {sorted(SUPPORTED_TASK_MANIFEST_VERSIONS)}"
        )
    if not isinstance(payload.get("source_fingerprint"), str) or not payload["source_fingerprint"]:
        raise ValueError("Native task manifest source fingerprint is missing")
    pass_kind = payload.get("pass_kind", "translation")
    if pass_kind not in PASS_KINDS:
        raise ValueError("Native task manifest pass_kind is invalid")
    if version >= LEGACY_WORKER_SPLIT_MANIFEST_VERSION:
        if payload.get("task_payload_schema") != TASK_PAYLOAD_SCHEMA:
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: task payload schema is missing")
        worker_contract = payload.get("worker_contract")
        detail_contract = payload.get("detail_contract")
        execution_contract = payload.get("execution_contract")
        result_contract = payload.get("result_contract")
        route_binding = payload.get("route_binding")
        language_binding = (
            _normalized_language_binding(payload.get("language_binding"))
            if version >= TASK_MANIFEST_VERSION
            else None
        )
        if route_binding is not None and (
            not isinstance(route_binding, dict)
            or set(route_binding) not in (
                {"provider", "attempt"},
                {"provider", "model", "attempt"},
            )
            or not isinstance(route_binding.get("provider"), str)
            or not route_binding["provider"]
            or (
                "model" in route_binding
                and (
                    not isinstance(route_binding.get("model"), str)
                    or not route_binding["model"]
                )
            )
            or not isinstance(route_binding.get("attempt"), int)
            or isinstance(route_binding["attempt"], bool)
            or route_binding["attempt"] < 1
        ):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: route binding is invalid")
        contract_fingerprint = payload.get("contract_fingerprint")
        fingerprint_payload = {
            "worker_contract": worker_contract,
            "detail_contract": detail_contract,
            "execution_contract": execution_contract,
        }
        # Manifests prepared before the explicit result contract remain
        # resumable. Newly prepared manifests bind the canonical schema into
        # the same immutable contract fingerprint.
        previous_result_contract = dict(_result_contract())
        previous_result_contract["missing_content_provenance_action"] = (
            "normalize_from_bound_direct_model_transport"
        )
        previous_result_contract.pop("exact_runtime_model_field")
        previous_result_contract.pop("provider_route_alias_field")
        legacy_result_contract = dict(_result_contract())
        legacy_result_contract["canonical_fields"] = [
            "target_id", "status", "text", "provider", "issues"
        ]
        legacy_result_contract["optional_fields"] = [
            "provider", "issues", "split_depth", "semantic_attempts"
        ]
        for field in (
            "content_provenance_schema",
            "missing_content_provenance_action",
            "exact_runtime_model_field",
            "provider_route_alias_field",
            "unauthorized_external_provenance_action",
        ):
            legacy_result_contract.pop(field)
        if result_contract is not None:
            if result_contract not in (
                _result_contract(),
                previous_result_contract,
                legacy_result_contract,
            ):
                raise ValueError(
                    "E_DETAIL_CONTRACT_CONFLICT: native result contract was changed"
                )
            fingerprint_payload["result_contract"] = result_contract
        if route_binding is not None:
            fingerprint_payload["route_binding"] = route_binding
        for field in ("translation_route_plan", "translation_predecessor"):
            if payload.get(field) is not None:
                fingerprint_payload[field] = payload[field]
        if payload.get("translation_route_plan") is not None:
            from .translation_exchange import _validated_translation_route_plan
            plan = _validated_translation_route_plan(payload["translation_route_plan"])
            selected = next((i for i, item in enumerate(plan) if item["provider"] == (route_binding or {}).get("provider")), None)
            if (
                pass_kind != "translation" or selected is None
                or plan[selected]["transport"] != "native"
                or plan[selected]["model"] != route_binding.get("model")
            ):
                raise ValueError("E_TRANSLATION_ROUTE_PLAN_INVALID: native manifest binding differs from the route graph")
            predecessor = payload.get("translation_predecessor")
            if predecessor is not None and (
                not isinstance(predecessor, dict)
                or set(predecessor) != {"receipt", "receipt_sha256", "contract_fingerprint"}
                or not isinstance(predecessor["receipt"], str) or not predecessor["receipt"]
                or any(not isinstance(predecessor[field], str) or re.fullmatch(r"[0-9a-f]{64}", predecessor[field]) is None
                       for field in ("receipt_sha256", "contract_fingerprint"))
            ):
                raise ValueError("E_TRANSLATION_PREDECESSOR_INVALID")
            if predecessor is None and any(item["transport"] == "external" for item in plan[:selected]):
                raise ValueError("E_TRANSLATION_PREDECESSOR_REQUIRED")
        elif payload.get("translation_predecessor") is not None:
            raise ValueError("E_TRANSLATION_ROUTE_PLAN_INVALID: predecessor lacks its full route graph")
        if "execution_namespace" in payload:
            namespace = manifest_path.parent.resolve().relative_to(work_dir.resolve()).as_posix()
            if payload["execution_namespace"] != namespace:
                raise ValueError("E_DETAIL_CONTRACT_CONFLICT: execution namespace changed")
            fingerprint_payload["execution_namespace"] = namespace
        if language_binding is not None:
            fingerprint_payload["language_binding"] = language_binding
        source_policy = (
            payload.get("content_source_policy")
            if version >= TASK_MANIFEST_VERSION
            else None
        )
        if source_policy is not None:
            try:
                validate_content_source_policy(source_policy)
            except ValueError:
                raise ValueError(
                    "E_DETAIL_CONTRACT_CONFLICT: content-source policy was changed"
                ) from None
            if (
                source_policy["task_kind"]
                not in (
                    {"review", "triage", "strong_review"}
                    if pass_kind == "review"
                    else {pass_kind}
                )
                or source_policy["route"] != f"native-pass:{pass_kind}"
            ):
                raise ValueError(
                    "E_DETAIL_CONTRACT_CONFLICT: content-source policy scope changed"
                )
            if (
                pass_kind == "review"
                and source_policy["task_kind"] == "review"
                and source_policy["authorization"] != "default_deny"
            ):
                raise ValueError(
                    "E_DETAIL_CONTRACT_CONFLICT: authorized review policy "
                    "must bind exact triage or strong_review scope"
                )
        if not isinstance(execution_contract, dict):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: execution contract is missing")
        optimization = execution_contract.get("review_optimization_policy", {})
        if not isinstance(optimization, dict):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: review optimization policy is invalid")
        max_depth = optimization.get("max_depth", DEFAULT_REVIEW_SPLIT_MAX_DEPTH)
        amplification = optimization.get("max_record_amplification")
        if (
            not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0
            or amplification is not None and (
                not isinstance(amplification, (int, float)) or isinstance(amplification, bool)
                or not math.isfinite(amplification) or amplification < 1
            )
            or execution_contract.get("review_tier") not in {None, "triage", "strong"}
        ):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: review optimization policy is invalid")
        expected_detail = _detail_contract(pass_kind, manifest_version=version, max_depth=max_depth)
        legacy_detail = json.loads(json.dumps(expected_detail))
        # Host repair is execution-only. Preserve the immutable fingerprints and
        # successes of manifests prepared before that control action was added.
        legacy_detail["review_control"]["allowed_actions"].remove("repair_native_runtime")
        expected_worker = _worker_contract(pass_kind, manifest_version=version, max_depth=max_depth)
        legacy_worker = dict(expected_worker)
        legacy_worker["max_model_calls_per_task"] = (
            24 if version < TASK_MANIFEST_VERSION and pass_kind == "review" else 16
        )
        if (
            worker_contract not in (expected_worker, legacy_worker)
            or detail_contract not in (expected_detail, legacy_detail)
        ):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: native task contract was changed")
        if not isinstance(execution_contract, dict) or (
            execution_contract.get("detail_contract_schema")
            != "parr0t.detail-contract.v1"
        ):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: execution contract is missing")
        budget_gate = execution_contract.get("bulk_budget_gate", {})
        pilot_waves = budget_gate.get("pilot_waves_without_approved_budget", 0)
        legacy_contract_shape = (
            version < TASK_MANIFEST_VERSION
            or result_contract is None
            or result_contract == legacy_result_contract
            or detail_contract == legacy_detail
            or "control_turn_budget_user_approved" not in execution_contract
            or "execution_namespace" not in payload
            or pilot_waves != 0
        )
        if version >= TASK_MANIFEST_VERSION and source_policy is None:
            if not legacy_contract_shape:
                raise ValueError(
                    "E_DETAIL_CONTRACT_CONFLICT: content-source policy is missing"
                )
        legacy_fingerprint = stable_hash(fingerprint_payload)
        if source_policy is not None:
            fingerprint_payload["content_source_policy"] = source_policy
        expected_fingerprint = stable_hash(fingerprint_payload)
        legacy_policy_is_safe = (
            source_policy is None
            or source_policy["authorization"] == "default_deny"
        )
        if contract_fingerprint != expected_fingerprint and not (
            legacy_contract_shape
            and legacy_policy_is_safe
            and contract_fingerprint == legacy_fingerprint
        ):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: contract fingerprint is invalid")
        if version >= TASK_MANIFEST_VERSION:
            review_split = payload.get("review_split")
            if pass_kind == "review":
                if (
                    not isinstance(review_split, dict)
                    or set(review_split)
                    != {
                        "depth",
                        "semantic_passes",
                        "parent_manifest",
                        "root_source_fingerprint",
                    }
                    or not isinstance(review_split.get("depth"), int)
                    or isinstance(review_split.get("depth"), bool)
                    or review_split["depth"] < 0
                    or not isinstance(review_split.get("semantic_passes"), int)
                    or isinstance(review_split.get("semantic_passes"), bool)
                    or review_split["semantic_passes"] < 1
                    or not isinstance(review_split.get("parent_manifest"), str)
                    or not isinstance(
                        review_split.get("root_source_fingerprint"), str
                    )
                    or not review_split["root_source_fingerprint"]
                ):
                    raise ValueError(
                        "E_DETAIL_CONTRACT_CONFLICT: review split binding is invalid"
                    )
            elif review_split is not None:
                raise ValueError(
                    "E_DETAIL_CONTRACT_CONFLICT: non-review manifest has split state"
                )
    seen_files: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("Native task manifest contains an invalid task")
        name = task.get("file")
        wave = task.get("wave")
        records = task.get("records")
        digest = task.get("sha256")
        dispatch_authorized = task.get("dispatch_authorized")
        if (
            not isinstance(name, str)
            or not name
            or Path(name).name != name
            or name in seen_files
            or not isinstance(wave, int)
            or isinstance(wave, bool)
            or wave < 1
            or not isinstance(records, int)
            or isinstance(records, bool)
            or records < 1
            or not isinstance(digest, str)
            or not digest
            or (
                version >= LEGACY_WORKER_SPLIT_MANIFEST_VERSION
                and dispatch_authorized
                != (
                    budget_gate.get("active") is not True
                    or budget_gate.get("user_approved") is True
                    or wave <= pilot_waves
                )
            )
        ):
            raise ValueError("Native task manifest contains an invalid task binding")
        seen_files.add(name)
    return payload


def _validate_manifest_task_file(
    manifest_path: Path,
    manifest: dict[str, Any],
    task: dict[str, Any],
) -> list[str]:
    """Validate only a task that is about to be dispatched or imported."""

    task_path = manifest_path.parent / task["file"]
    if not task_path.is_file() or _file_sha256(task_path) != task["sha256"]:
        raise ValueError("Native task manifest references a missing or changed task")
    target_ids = _task_target_ids(
        task_path,
        require_payload=(
            manifest.get("version", 0) >= LEGACY_WORKER_SPLIT_MANIFEST_VERSION
        ),
        language_binding=(
            manifest.get("language_binding")
            if manifest.get("version", 0) >= TASK_MANIFEST_VERSION
            else None
        ),
        content_source_policy=_bound_manifest_content_source_policy(manifest),
    )
    if len(target_ids) != task["records"]:
        raise ValueError("Native task record count does not match its manifest binding")
    return target_ids


def _manifest_shard_path(
    work_dir: Path, manifest: dict[str, Any], task: dict[str, Any]
) -> Path:
    binding_source = {
        "source_fingerprint": manifest["source_fingerprint"],
        "task_file": task["file"],
    }
    if manifest.get("version", 0) >= TASK_MANIFEST_VERSION:
        binding_source["task_sha256"] = task["sha256"]
    if "execution_namespace" in manifest:
        binding_source.update(
            execution_namespace=manifest["execution_namespace"],
            pass_kind=manifest.get("pass_kind", "translation"),
            route_binding=manifest.get("route_binding"),
        )
    binding = stable_hash(binding_source)[:16]
    stem = Path(task["file"]).stem
    return work_dir / ".game-l10n" / "native" / "shards" / f"{binding}-{stem}.jsonl"


def _wave_receipt_path(
    work_dir: Path, manifest: dict[str, Any], tasks: list[dict[str, Any]], wave: int
) -> Path:
    binding = stable_hash(
        {
            "source_fingerprint": manifest["source_fingerprint"],
            **({"execution_namespace": manifest["execution_namespace"],
                "pass_kind": manifest.get("pass_kind", "translation"),
                "route_binding": manifest.get("route_binding")}
               if "execution_namespace" in manifest else {}),
            "tasks": [
                {"file": task["file"], "sha256": task["sha256"]} for task in tasks
            ],
        }
    )[:20]
    return (
        work_dir
        / ".game-l10n"
        / "native"
        / "waves"
        / f"{binding}-wave-{wave:04d}.json"
    )


def _write_wave_receipt(
    work_dir: Path,
    manifest: dict[str, Any],
    tasks: list[dict[str, Any]],
    wave: int,
) -> Path:
    path = _wave_receipt_path(work_dir, manifest, tasks, wave)
    _atomic_json(
        path,
        {
            "schema": NATIVE_WAVE_RECEIPT_SCHEMA,
            "source_fingerprint": manifest["source_fingerprint"],
            "wave": wave,
            "tasks": [
                {
                    "task_file": task["file"],
                    "task_sha256": task["sha256"],
                    "shard": _manifest_shard_path(work_dir, manifest, task)
                    .resolve()
                    .relative_to(work_dir.resolve())
                    .as_posix(),
                    "shard_size": _manifest_shard_path(
                        work_dir, manifest, task
                    ).stat().st_size,
                    "records": task["records"],
                }
                for task in tasks
            ],
        },
    )
    return path


def _task_target_ids(
    task_path: Path,
    *,
    require_payload: bool = False,
    language_binding: dict[str, Any] | None = None,
    content_source_policy: dict[str, Any] | None = None,
) -> list[str]:
    rows = _read_jsonl(task_path)
    ids: list[str] = []
    for row in rows:
        target_id = row.get("target_id")
        ordinal = row.get("ordinal")
        if (
            not isinstance(target_id, str)
            or not target_id
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 0
        ):
            raise ValueError("Native task file contains an invalid target binding")
        if require_payload:
            segment_payload = row.get("segment")
            if not isinstance(segment_payload, dict):
                raise ValueError("Native task file is missing its bound segment payload")
            try:
                segment = Segment(**segment_payload)
            except (TypeError, ValueError):
                raise ValueError("Native task file contains an invalid segment payload") from None
            if segment.id != target_id:
                raise ValueError("Native task segment payload does not match its target binding")
        if language_binding is not None and row.get("language_binding") != language_binding:
            raise ValueError("Native task target language binding is missing or changed")
        if (
            content_source_policy is not None
            and row.get("content_source_policy") != content_source_policy
        ):
            raise ValueError("Native task content-source policy is missing or changed")
        ids.append(target_id)
    if len(ids) != len(set(ids)):
        raise ValueError("Native task file contains duplicate target bindings")
    return ids


def _estimated_material_tokens(segment: Segment) -> int:
    """Estimate private input material for shard sizing, not provider billing."""

    payload = json.dumps(
        asdict(segment),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, (len(payload) + 2) // 3)


def _require_candidate_bindings(segments: list[Segment], pass_kind: str) -> None:
    if pass_kind not in {"review", "rewrite"}:
        return
    for segment in segments:
        candidate = segment.metadata.get(CANDIDATE_METADATA_KEY)
        if not isinstance(candidate, dict):
            raise ValueError(
                f"{pass_kind} target {segment.id!r} is missing its candidate binding"
            )
        text = candidate.get("text")
        generation = candidate.get("generation")
        digest = candidate.get("digest")
        valid_nonprinting = (
            isinstance(text, str)
            and not text.strip()
            and not validate_integrity(segment, text, [])
        )
        if (
            not isinstance(text, str)
            or (not text.strip() and not valid_nonprinting)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or digest != stable_hash(text)
        ):
            raise ValueError(
                f"{pass_kind} target {segment.id!r} has an invalid candidate binding"
            )


def _native_translation_predecessor(
    work_dir: Path, plan: list[dict[str, Any]], provider: str,
    receipt_path: Path | None, segments: list[Segment],
) -> dict[str, Any] | None:
    from .translation_exchange import verify_translation_predecessor
    from .formats import read_segments
    selected = next((i for i, item in enumerate(plan) if item["provider"] == provider), None)
    if selected is None or plan[selected]["transport"] != "native":
        raise ValueError("E_NATIVE_ROUTE_INVALID: selected route is not a configured native translator")
    requires_predecessor = any(item["transport"] == "external" for item in plan[:selected])
    if receipt_path is None:
        if requires_predecessor:
            raise ValueError("E_TRANSLATION_PREDECESSOR_REQUIRED: use the preceding external block's native_continuation receipt")
        return None
    verified = verify_translation_predecessor(work_dir, receipt_path, full_route_plan=plan)
    if provider not in {item["provider"] for item in verified["native_route_plan"]}:
        raise ValueError("E_TRANSLATION_PREDECESSOR_INVALID: wrong native successor block")
    authorized = {item.id: asdict(item) for item in read_segments(work_dir / verified["source"])}
    if not segments or any(authorized.get(item.id) != asdict(item) for item in segments):
        raise ValueError("E_TRANSLATION_PREDECESSOR_INVALID: native input is not an unchanged failure-only subset")
    return {key: verified[key] for key in ("receipt", "receipt_sha256", "contract_fingerprint")}


def prepare_native_tasks(
    work_dir: Path,
    source_path: Path,
    segments: list[Segment],
    output_dir: Path,
    *,
    pass_kind: str = "translation",
    max_records: int | None = None,
    min_records: int | None = None,
    max_material_tokens: int | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    max_elapsed_minutes: int | None = None,
    max_input_tokens: int | None = None,
    max_provider_cost_usd: float | None = None,
    provider_cost_unmetered: bool = False,
    user_approved_budget: bool = False,
    max_root_model_calls_per_wave: int | None = None,
    max_root_model_calls_per_turn: int | None = None,
    max_root_input_tokens_per_turn: int | None = None,
    control_turn_budget_user_approved: bool = False,
    review_split_max_depth: int = DEFAULT_REVIEW_SPLIT_MAX_DEPTH,
    review_tier: str | None = None,
    review_split_max_record_amplification: float | None = None,
    route_binding: dict[str, Any] | None = None,
    language_binding: dict[str, Any] | None = None,
    predecessor_receipt: Path | None = None,
    translation_route_plan: list[dict[str, Any]] | None = None,
    _review_split: dict[str, Any] | None = None,
    content_source_grant: Path | None = None,
    content_source_scope: str = "",
    content_source_task_kind: str | None = None,
    _content_source_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    explicit_content_source_policy = (
        _content_source_policy is not None
        or content_source_grant is not None
        or bool(content_source_scope)
    )
    if pass_kind not in PASS_KINDS:
        raise ValueError(f"pass_kind must be one of: {', '.join(sorted(PASS_KINDS))}")
    predecessor_context = None
    if pass_kind == "translation" and predecessor_receipt is not None:
        from .translation_exchange import verify_translation_predecessor
        predecessor_context = verify_translation_predecessor(work_dir, predecessor_receipt)
        if not explicit_content_source_policy:
            _content_source_policy = predecessor_context.get("content_source_policy")
            explicit_content_source_policy = _content_source_policy is not None
    language_binding = _normalized_language_binding(language_binding)
    if route_binding is not None and (
        set(route_binding) not in (
            {"provider", "attempt"},
            {"provider", "model", "attempt"},
        )
        or not isinstance(route_binding.get("provider"), str)
        or not route_binding["provider"]
        or (
            "model" in route_binding
            and (
                not isinstance(route_binding.get("model"), str)
                or not route_binding["model"]
            )
        )
        or not isinstance(route_binding.get("attempt"), int)
        or isinstance(route_binding["attempt"], bool)
        or route_binding["attempt"] < 1
    ):
        raise ValueError(
            "route_binding requires an exact provider, runtime model, and positive attempt"
        )
    fingerprint = stable_hash_sequence(asdict(segment) for segment in segments)
    if (
        _content_source_policy is None
        and content_source_grant is None
        and not content_source_scope
    ):
        resume_manifest_path = output_dir.resolve() / "manifest.json"
        resume_manifest_within_workspace = True
        try:
            resume_manifest_path.relative_to(work_dir.resolve())
        except ValueError:
            resume_manifest_within_workspace = False
        if (
            resume_manifest_within_workspace
            and resume_manifest_path.is_file()
        ):
            resume_manifest = _load_task_manifest(
                work_dir, resume_manifest_path
            )
            if (
                resume_manifest.get("source_fingerprint") == fingerprint
                and resume_manifest.get("pass_kind", "translation") == pass_kind
            ):
                _content_source_policy = (
                    _bound_manifest_content_source_policy(resume_manifest)
                )
    policy_route = f"native-pass:{pass_kind}"
    semantic_task_kind = (
        content_source_task_kind
        or (
            str(_content_source_policy.get("task_kind"))
            if isinstance(_content_source_policy, dict)
            else ""
        )
        or pass_kind
    )
    allowed_policy_kinds = (
        {"review", "triage", "strong_review"}
        if pass_kind == "review"
        else {pass_kind}
    )
    if semantic_task_kind not in allowed_policy_kinds:
        raise ValueError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    if review_tier is not None and (pass_kind != "review" or review_tier not in {"triage", "strong"}):
        raise ValueError("review_tier must be triage or strong for a review pass")
    if pass_kind == "review" and review_tier is None:
        review_tier = {"triage": "triage", "strong_review": "strong"}.get(semantic_task_kind)
    if review_tier is not None and semantic_task_kind in {"triage", "strong_review"} and (
        review_tier != ("triage" if semantic_task_kind == "triage" else "strong")
    ):
        raise ValueError("E_CONTENT_SOURCE_POLICY_IMMUTABLE: review tier conflicts with bound scope")
    if _content_source_policy is None:
        source_policy = content_source_policy(
            work_dir,
            task_kind=semantic_task_kind,
            route=policy_route,
            scope_id=content_source_scope,
            grant_path=content_source_grant,
        )
    else:
        source_policy = validate_content_source_policy(_content_source_policy)
        if (
            source_policy["task_kind"] != semantic_task_kind
            or source_policy["route"] != policy_route
        ):
            raise ValueError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    if (
        pass_kind == "review"
        and source_policy["task_kind"] == "review"
        and source_policy["authorization"] != "default_deny"
    ):
        raise ValueError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    if max_records is None:
        max_records = (
            DEFAULT_REVIEW_MAX_RECORDS
            if pass_kind == "review"
            else DEFAULT_MAX_RECORDS
        )
    if max_material_tokens is None:
        max_material_tokens = (
            DEFAULT_REVIEW_MAX_MATERIAL_TOKENS
            if pass_kind == "review"
            else DEFAULT_MAX_MATERIAL_TOKENS
        )
    if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records < 1:
        raise ValueError("max_records must be a positive integer")
    if min_records is None:
        min_records = min(
            DEFAULT_REVIEW_MIN_RECORDS if pass_kind == "review" else DEFAULT_MIN_RECORDS,
            max_records,
        )
    if not isinstance(min_records, int) or isinstance(min_records, bool) or not 1 <= min_records <= max_records:
        raise ValueError("min_records must be between 1 and max_records")
    if not isinstance(max_material_tokens, int) or isinstance(max_material_tokens, bool) or max_material_tokens < 1:
        raise ValueError("max_material_tokens must be a positive integer")
    if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
        raise ValueError("max_workers must be a positive integer")
    from .native_runtime import native_worker_ceiling
    config_path = work_dir / "game-l10n.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    routing = config.get("routing", {})
    role = "translators" if pass_kind == "translation" else "rewriters" if pass_kind == "rewrite" else (
        "review_triagers" if review_tier == "triage" else "reviewers"
    )
    provider = (route_binding or {}).get("provider") or next(iter(routing.get(role, [])), "")
    if predecessor_context is not None and route_binding is None:
        provider = predecessor_context["native_route_plan"][0]["provider"]
    if pass_kind == "review":
        max_workers = review_worker_limit(
            routing, max_workers, tier=review_tier or "strong"
        )
    if provider:
        max_workers = native_worker_ceiling(work_dir, provider, max_workers)
    translation_predecessor = None
    if pass_kind == "translation":
        from .translation_exchange import configured_translation_route_plan, _validated_translation_route_plan
        configured_plan = configured_translation_route_plan(work_dir, config)
        if translation_route_plan is not None:
            translation_route_plan = _validated_translation_route_plan(translation_route_plan)
            if configured_plan and configured_plan != translation_route_plan:
                raise ValueError("E_TRANSLATION_ROUTE_PLAN_CHANGED: use a fresh namespace for changed routes")
        else:
            translation_route_plan = configured_plan or None
        if translation_route_plan is not None:
            translation_predecessor = _native_translation_predecessor(
                work_dir, translation_route_plan, provider, predecessor_receipt, segments,
            )
            if predecessor_context is not None and translation_predecessor != {
                key: predecessor_context[key] for key in ("receipt", "receipt_sha256", "contract_fingerprint")
            }:
                raise ValueError("E_TRANSLATION_PREDECESSOR_INVALID: predecessor changed during preparation")
            selected_model = next(item["model"] for item in translation_route_plan if item["provider"] == provider)
            if route_binding is not None and route_binding.get("model", selected_model) != selected_model:
                raise ValueError("E_MODEL_POLICY_BINDING_CONFLICT: selected model differs from configured translation route")
            route_binding = {**(route_binding or {"provider": provider, "attempt": 1}), "model": selected_model}
        elif predecessor_receipt is not None:
            raise ValueError("E_TRANSLATION_ROUTE_PLAN_INVALID: predecessor requires the complete configured route graph")
    elif predecessor_receipt is not None or translation_route_plan is not None:
        raise ValueError("E_TRANSLATION_ROUTE_PLAN_INVALID: translation continuation is not a review policy")
    if not isinstance(review_split_max_depth, int) or isinstance(review_split_max_depth, bool) or review_split_max_depth < 0:
        raise ValueError("review_split_max_depth must be a non-negative integer")
    if review_split_max_record_amplification is not None and (
        not isinstance(review_split_max_record_amplification, (int, float))
        or isinstance(review_split_max_record_amplification, bool)
        or not math.isfinite(review_split_max_record_amplification)
        or review_split_max_record_amplification < 1
    ):
        raise ValueError("review_split_max_record_amplification must be finite and at least one")
    if _review_split is not None and pass_kind != "review":
        raise ValueError("Review split metadata is valid only for review passes")
    review_split = _review_split or (
        {
            "depth": 0,
            "semantic_passes": 1,
            "parent_manifest": "",
            "root_source_fingerprint": "",
        }
        if pass_kind == "review"
        else None
    )
    if review_split is not None and (
        set(review_split)
        != {
            "depth",
            "semantic_passes",
            "parent_manifest",
            "root_source_fingerprint",
        }
        or not isinstance(review_split["depth"], int)
        or isinstance(review_split["depth"], bool)
        or review_split["depth"] < 0
        or not isinstance(review_split["semantic_passes"], int)
        or isinstance(review_split["semantic_passes"], bool)
        or review_split["semantic_passes"] < 1
        or not isinstance(review_split["parent_manifest"], str)
        or not isinstance(review_split["root_source_fingerprint"], str)
    ):
        raise ValueError("Review split metadata is invalid")
    if max_elapsed_minutes is not None and (
        not isinstance(max_elapsed_minutes, int) or isinstance(max_elapsed_minutes, bool) or max_elapsed_minutes < 1
    ):
        raise ValueError("max_elapsed_minutes must be a positive integer")
    if max_input_tokens is not None and (
        not isinstance(max_input_tokens, int) or isinstance(max_input_tokens, bool) or max_input_tokens < 1
    ):
        raise ValueError("max_input_tokens must be a positive integer")
    if max_provider_cost_usd is not None and (
        not isinstance(max_provider_cost_usd, (int, float))
        or isinstance(max_provider_cost_usd, bool)
        or not math.isfinite(max_provider_cost_usd) or max_provider_cost_usd <= 0
    ):
        raise ValueError("max_provider_cost_usd must be positive")
    if provider_cost_unmetered and max_provider_cost_usd is not None:
        raise ValueError(
            "provider_cost_unmetered and max_provider_cost_usd are mutually exclusive"
        )
    if user_approved_budget and not any(
        value is not None
        for value in (max_elapsed_minutes, max_input_tokens, max_provider_cost_usd)
    ):
        raise ValueError("A user-approved budget requires at least one explicit limit")
    control_turn_limits = (
        max_root_model_calls_per_wave,
        max_root_model_calls_per_turn,
        max_root_input_tokens_per_turn,
    )
    if control_turn_budget_user_approved:
        if not any(value is not None for value in control_turn_limits):
            raise ValueError(
                "A user-approved control-turn budget requires at least one explicit limit"
            )
        if any(
            value is not None
            and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            )
            for value in control_turn_limits
        ):
            raise ValueError("Control-turn budget limits must be positive integers")
    elif any(value is not None for value in control_turn_limits):
        raise ValueError("Control-turn budget limits require explicit user approval")
    if not segments:
        raise ValueError("Ordered source contains no segments")
    _require_candidate_bindings(segments, pass_kind)
    ids = [segment.id for segment in segments]
    if len(ids) != len(set(ids)):
        raise ValueError("Ordered source contains duplicate target IDs")
    try:
        source_artifact = source_path.resolve().relative_to(work_dir.resolve()).as_posix()
        output_dir.resolve().relative_to(work_dir.resolve())
    except ValueError as exc:
        raise ValueError("Native task source and output must stay inside the private workspace") from exc

    if review_split is not None and not review_split["root_source_fingerprint"]:
        review_split = {**review_split, "root_source_fingerprint": fingerprint}
    task_max_records = max_records
    task_max_material_tokens = max_material_tokens
    task_min_records = min(min_records, task_max_records)
    manifest_path = output_dir / "manifest.json"
    execution_namespace = output_dir.resolve().relative_to(work_dir.resolve()).as_posix()
    if manifest_path.is_file():
        manifest = _load_task_manifest(work_dir, manifest_path)
        preserve_legacy_unbound_policy = (
            _bound_manifest_content_source_policy(manifest) is None
            and not explicit_content_source_policy
        )
        if (
            manifest.get("version") == TASK_MANIFEST_VERSION
            and manifest.get("source_fingerprint") == fingerprint
            and manifest.get("pass_kind", "translation") == pass_kind
            and manifest.get("content_source_policy") != source_policy
            and not preserve_legacy_unbound_policy
        ):
            raise ValueError(
                "E_CONTENT_SOURCE_POLICY_IMMUTABLE: prepare a new task namespace"
            )
        if (
            manifest.get("version") == TASK_MANIFEST_VERSION
            and manifest.get("source_fingerprint") == fingerprint
            and manifest.get("pass_kind", "translation") == pass_kind
            and manifest.get("max_records") == max_records
            and manifest.get("min_records") == min_records
            and manifest.get("max_material_tokens") == max_material_tokens
            and manifest.get("max_workers") == max_workers
            and manifest.get("task_max_records") == task_max_records
            and manifest.get("task_max_material_tokens")
            == task_max_material_tokens
            and manifest.get("review_split") == review_split
            and manifest.get("route_binding") == route_binding
            and manifest.get("translation_route_plan") == translation_route_plan
            and manifest.get("translation_predecessor") == translation_predecessor
            and manifest.get("execution_namespace", execution_namespace) == execution_namespace
            and manifest.get("language_binding") == language_binding
            and manifest.get("task_payload_schema") == TASK_PAYLOAD_SCHEMA
            and (
                manifest.get("content_source_policy") == source_policy
                or preserve_legacy_unbound_policy
            )
        ):
            task_rows = manifest.get("tasks", [])
            expected_execution_contract = _execution_contract(
                len(task_rows),
                max_workers,
                len(segments),
                pass_kind=pass_kind,
                max_elapsed_minutes=max_elapsed_minutes,
                max_input_tokens=max_input_tokens,
                max_provider_cost_usd=max_provider_cost_usd,
                provider_cost_unmetered=provider_cost_unmetered,
                user_approved_budget=user_approved_budget,
                max_root_model_calls_per_wave=max_root_model_calls_per_wave,
                max_root_model_calls_per_turn=max_root_model_calls_per_turn,
                max_root_input_tokens_per_turn=max_root_input_tokens_per_turn,
                control_turn_budget_user_approved=control_turn_budget_user_approved,
                review_split_max_depth=review_split_max_depth,
                review_split_max_record_amplification=review_split_max_record_amplification,
                review_tier=review_tier,
            )
            valid = (
                manifest["execution_contract"] == expected_execution_contract
                and all(
                    (output_dir / item["file"]).is_file()
                    and isinstance(item.get("size"), int)
                    and (output_dir / item["file"]).stat().st_size == item["size"]
                    for item in task_rows
                )
            )
            if valid:
                execution_contract = expected_execution_contract
                return {
                    "cached": True,
                    "records": len(segments),
                    "tasks": len(task_rows),
                    "pass_kind": pass_kind,
                    "max_records": max_records,
                    "min_records": min_records,
                    "max_material_tokens": max_material_tokens,
                    "task_max_records": task_max_records,
                    "task_max_material_tokens": task_max_material_tokens,
                    "max_workers": max_workers,
                    "fresh_worker_per_task": True,
                    "history_mode": "none",
                    "task_payload_schema": TASK_PAYLOAD_SCHEMA,
                    "contract_fingerprint": manifest["contract_fingerprint"],
                    "content_source_policy": manifest.get("content_source_policy"),
                    "execution_contract": execution_contract,
                    "manifest": str(manifest_path),
                    "artifacts": [
                        str(output_dir / item["file"])
                        for item in task_rows
                        if item["dispatch_authorized"]
                    ],
                    "deferred_tasks": sum(
                        not item["dispatch_authorized"] for item in task_rows
                    ),
                }
        raise ValueError("E_NATIVE_TASK_MANIFEST_IMMUTABLE: prepare changed execution policy in a new task namespace")

    output_dir.mkdir(parents=True, exist_ok=True)
    expected: set[str] = set()
    tasks: list[dict[str, Any]] = []
    batches: list[list[tuple[int, Segment, int]]] = []
    current: list[tuple[int, Segment, int]] = []
    current_tokens = 0
    for ordinal, segment in enumerate(segments):
        estimated_tokens = _estimated_material_tokens(segment)
        if estimated_tokens > task_max_material_tokens:
            raise ValueError(
                "E_NATIVE_INPUT_CAPACITY_EXCEEDED: compact the bound target context "
                "or configure a larger max_material_tokens within the selected model's capacity"
            )
        # A mostly full translation batch ends at a scene change so a scene is translated
        # by one worker with one shared context; hard caps are unchanged.
        scene_break = (
            pass_kind == "translation"
            and bool(current)
            and bool(segment.scene)
            and bool(current[-1][1].scene)
            and segment.scene != current[-1][1].scene
            and (
                len(current) * 4 >= task_max_records * 3
                or current_tokens * 4 >= task_max_material_tokens * 3
            )
        )
        if current and (
            scene_break
            or len(current) >= task_max_records
            or current_tokens + estimated_tokens > task_max_material_tokens
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append((ordinal, segment, estimated_tokens))
        current_tokens += estimated_tokens
    if current:
        batches.append(current)
    _rebalance_small_tail(
        batches,
        min_records=task_min_records,
        max_records=task_max_records,
        max_material_tokens=task_max_material_tokens,
    )

    for batch in batches:
        number = len(tasks) + 1
        name = f"task-{number:04d}.jsonl"
        path = output_dir / name
        rows = [
            {
                "target_id": segment.id,
                "ordinal": ordinal,
                "segment": asdict(segment),
                "language_binding": language_binding,
                "content_source_policy": source_policy,
            }
            for ordinal, segment, _ in batch
        ]
        _atomic_jsonl(path, rows)
        expected.add(name)
        tasks.append(
            {
                "file": name,
                "wave": (number - 1) // max_workers + 1,
                "slot": (number - 1) % max_workers + 1,
                "records": len(rows),
                "estimated_material_tokens": sum(item[2] for item in batch),
                "oversized_target_count": sum(
                    item[2] > task_max_material_tokens for item in batch
                ),
                "sha256": _file_sha256(path),
                "size": path.stat().st_size,
            }
        )
    for stale in output_dir.glob("task-*.jsonl"):
        if stale.name not in expected:
            stale.unlink()
    execution_contract = _execution_contract(
        len(tasks),
        max_workers,
        len(segments),
        pass_kind=pass_kind,
        max_elapsed_minutes=max_elapsed_minutes,
        max_input_tokens=max_input_tokens,
        max_provider_cost_usd=max_provider_cost_usd,
        provider_cost_unmetered=provider_cost_unmetered,
        user_approved_budget=user_approved_budget,
        max_root_model_calls_per_wave=max_root_model_calls_per_wave,
        max_root_model_calls_per_turn=max_root_model_calls_per_turn,
        max_root_input_tokens_per_turn=max_root_input_tokens_per_turn,
        control_turn_budget_user_approved=control_turn_budget_user_approved,
        review_split_max_depth=review_split_max_depth,
        review_tier=review_tier,
        review_split_max_record_amplification=review_split_max_record_amplification,
    )
    budget_gate = execution_contract["bulk_budget_gate"]
    for task in tasks:
        task["dispatch_authorized"] = (
            budget_gate["active"] is not True
            or budget_gate["user_approved"] is True
            or task["wave"] <= budget_gate["pilot_waves_without_approved_budget"]
        )
    worker_contract = _worker_contract(pass_kind, max_depth=review_split_max_depth)
    detail_contract = _detail_contract(pass_kind, max_depth=review_split_max_depth)
    result_contract = _result_contract()
    contract_fingerprint = stable_hash(
        {
            "worker_contract": worker_contract,
            "detail_contract": detail_contract,
            "execution_contract": execution_contract,
            "result_contract": result_contract,
            **({"route_binding": route_binding} if route_binding is not None else {}),
            **({"translation_route_plan": translation_route_plan} if translation_route_plan is not None else {}),
            **({"translation_predecessor": translation_predecessor} if translation_predecessor is not None else {}),
            "language_binding": language_binding,
            "content_source_policy": source_policy,
            "execution_namespace": execution_namespace,
        }
    )
    manifest = {
        "version": TASK_MANIFEST_VERSION,
        "task_payload_schema": TASK_PAYLOAD_SCHEMA,
        "pass_kind": pass_kind,
        "source_artifact": source_artifact,
        "source_fingerprint": fingerprint,
        "max_records": max_records,
        "min_records": min_records,
        "max_material_tokens": max_material_tokens,
        "task_max_records": task_max_records,
        "task_max_material_tokens": task_max_material_tokens,
        "max_workers": max_workers,
        "records": len(segments),
        "review_split": review_split,
        "route_binding": route_binding,
        **({"translation_route_plan": translation_route_plan} if translation_route_plan is not None else {}),
        **({"translation_predecessor": translation_predecessor} if translation_predecessor is not None else {}),
        "execution_namespace": execution_namespace,
        "language_binding": language_binding,
        "content_source_policy": source_policy,
        "worker_contract": worker_contract,
        "detail_contract": detail_contract,
        "execution_contract": execution_contract,
        "result_contract": result_contract,
        "contract_fingerprint": contract_fingerprint,
        "tasks": tasks,
    }
    temporary = manifest_path.with_name(f"{manifest_path.name}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return {
        "cached": False,
        "records": len(segments),
        "tasks": len(tasks),
        "pass_kind": pass_kind,
        "max_records": max_records,
        "min_records": min_records,
        "max_material_tokens": max_material_tokens,
        "task_max_records": task_max_records,
        "task_max_material_tokens": task_max_material_tokens,
        "max_workers": max_workers,
        "fresh_worker_per_task": True,
        "history_mode": "none",
        "task_payload_schema": TASK_PAYLOAD_SCHEMA,
        "contract_fingerprint": contract_fingerprint,
        "content_source_policy": source_policy,
        "execution_contract": execution_contract,
        "manifest": str(manifest_path),
        "artifacts": [
            str(output_dir / item["file"])
            for item in tasks
            if item["dispatch_authorized"]
        ],
        "deferred_tasks": sum(not item["dispatch_authorized"] for item in tasks),
    }


def _validate_row(
    row: dict[str, Any],
    *,
    segment: Segment | None = None,
    content_policy: dict[str, Any] | None = None,
    provenance_model: str = "",
    provenance_route: str = "",
) -> dict[str, Any]:
    target_id = row.get("target_id")
    status = row.get("status")
    text = row.get("text", "")
    if not isinstance(target_id, str) or not target_id:
        raise ValueError("Native shard target_id must be a non-empty string")
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Native shard {target_id!r} has an invalid status")
    if not isinstance(text, str):
        raise ValueError(f"Native shard {target_id!r} text must be a string")
    if status == "success" and not text.strip():
        if (
            segment is None or segment.id != target_id
            or validate_integrity(segment, text, [])
        ):
            raise ValueError(f"Native shard {target_id!r} reports success without text")
    issues = row.get("issues", [])
    if not isinstance(issues, list) or not all(isinstance(item, str) for item in issues):
        raise ValueError(f"Native shard {target_id!r} issues must be strings")
    result = {
        "target_id": target_id,
        "status": status,
        "text": text if status == "success" else "",
        "provider": str(row.get("provider", "native-worker")),
        "issues": issues,
    }
    if content_policy is not None and not (
        status == "error"
        and result["provider"] == "native-control"
        and len(issues) == 1
        and issues[0] in OPERATIONAL_RESULT_ISSUES
        and "content_provenance" not in row
    ):
        result["content_provenance"] = validate_output_provenance(
            row.get("content_provenance"),
            policy=content_policy,
            model=provenance_model,
            route=provenance_route,
        )
    elif "content_provenance" in row:
        provenance = row["content_provenance"]
        if (
            not isinstance(provenance, dict)
            or set(provenance)
            != {
                "schema",
                "mode",
                "model",
                "route",
                "policy_digest",
                "external_sources_used",
            }
            or provenance.get("schema") != "parr0t.content-provenance.v1"
            or provenance.get("mode") != "direct_model"
            or not isinstance(provenance.get("model"), str)
            or not provenance["model"]
            or not isinstance(provenance.get("route"), str)
            or not provenance["route"]
            or not isinstance(provenance.get("policy_digest"), str)
            or not isinstance(provenance.get("external_sources_used"), list)
        ):
            raise ValueError("E_OUTPUT_PROVENANCE_INVALID")
        result["content_provenance"] = dict(provenance)
    for field in ("split_depth", "semantic_attempts"):
        if field in row:
            value = row[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"Native shard {target_id!r} {field} must be non-negative")
            result[field] = value
    return result


def _review_failure_has_split_proof(
    row: dict[str, Any], *, max_depth: int, max_calls: int
) -> bool:
    split_depth = row.get("split_depth")
    semantic_attempts = row.get("semantic_attempts")
    return bool(
        isinstance(split_depth, int)
        and not isinstance(split_depth, bool)
        and 1 <= split_depth <= max_depth
        and isinstance(semantic_attempts, int)
        and not isinstance(semantic_attempts, bool)
        and 2 <= semantic_attempts <= max_calls
    )


def _normalize_review_failure(
    row: dict[str, Any], *, max_depth: int, max_calls: int
) -> dict[str, Any]:
    """Keep valid review failures while isolating worker/relay contract failures."""

    if row["status"] == "success":
        return row
    if (
        len(row.get("issues", [])) == 1
        and row["issues"][0] in OPERATIONAL_RESULT_ISSUES
    ):
        return row
    if _review_failure_has_split_proof(
        row, max_depth=max_depth, max_calls=max_calls
    ):
        return row
    normalized = {
        "target_id": row["target_id"],
        "status": "error",
        "text": "",
        "provider": "native-control",
        "issues": ["review_split_proof_missing"],
    }
    if "content_provenance" in row:
        normalized["content_provenance"] = dict(row["content_provenance"])
    return normalized


class _NativeSourceBindingError(ValueError):
    """Shared source authority failed, invalidating dependent task imports."""


def _validate_manifest_rows(
    work_dir: Path,
    manifest: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    bound_segments: dict[str, Segment],
) -> list[dict[str, Any]]:
    # Ordinary results keep the existing fast path. Optional empty results must
    # join the exact prepared source, including its immutable adapter metadata.
    if not bound_segments and any(
        row.get("status") == "success" and row.get("text") == "" for row in rows
    ):
        from .formats import read_segments

        artifact = manifest.get("source_artifact")
        if not isinstance(artifact, str) or not artifact.strip():
            raise _NativeSourceBindingError("Optional-slot source artifact is invalid")
        try:
            if Path(artifact).is_absolute():
                raise ValueError("Optional-slot source artifact must be relative")
            source = (work_dir / artifact).resolve()
            source.relative_to(work_dir.resolve())
            if not source.is_file():
                raise ValueError("Optional-slot source artifact must be a file")
            ordered = read_segments(source)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            raise _NativeSourceBindingError("Optional-slot source artifact is invalid") from None
        if (
            len(ordered) != manifest.get("records")
            or stable_hash_sequence(asdict(segment) for segment in ordered)
            != manifest.get("source_fingerprint")
        ):
            raise _NativeSourceBindingError("Optional-slot source no longer matches its task manifest")
        source_ids = {segment.id for segment in ordered}
        for segment in ordered:
            if segment.metadata.get("ui_empty_target") is not None:
                related = segment.metadata.get("ui_composed_with_ids")
                if not isinstance(related, list) or not all(
                    isinstance(target_id, str) and target_id in source_ids
                    for target_id in related
                ):
                    raise _NativeSourceBindingError("Optional-slot composition references an unbound target")
        # Cache only a fully validated source; reuse once per wave/import scan.
        bound_segments.update((segment.id, segment) for segment in ordered)
    content_policy = _bound_manifest_content_source_policy(manifest)
    route_binding = manifest.get("route_binding") or {}
    runtime_models = (native_runtime(work_dir) or {}).get("models", {})
    if route_binding:
        expected_provider = route_binding["provider"]
        for row in rows:
            if (
                row.get("status") != "error"
                and row.get("provider", "native-worker") != expected_provider
            ):
                raise ValueError(
                    "Native result provider does not match its route binding"
                )
    validated = [
        _validate_row(
            row,
            segment=bound_segments.get(row["target_id"])
            if isinstance(row.get("target_id"), str) else None,
            content_policy=content_policy,
            provenance_model=route_binding.get("model") or runtime_models.get(
                route_binding.get("provider") or row.get("provider", "native-worker"),
                "",
            ),
            provenance_route=route_binding.get("provider") or row.get("provider", "native-worker"),
        )
        for row in rows
    ]
    if route_binding:
        expected_provider = route_binding["provider"]
        for row in validated:
            if row["status"] != "error" and row.get("provider") != expected_provider:
                raise ValueError("Native result provider does not match its route binding")
    if (
        manifest.get("pass_kind", "translation") == "review"
        and manifest.get("version") == LEGACY_WORKER_SPLIT_MANIFEST_VERSION
        and len(validated) > 1
    ):
        split_contract = manifest.get("worker_contract", {}).get("failure_split", {})
        max_depth = int(split_contract.get("max_depth", 0))
        max_calls = int(split_contract.get("max_semantic_calls", 1))
        validated = [
            _normalize_review_failure(
                row, max_depth=max_depth, max_calls=max_calls
            )
            for row in validated
        ]
    return validated


def import_native_shard(work_dir: Path, source: Path, shard_name: str, *, model: str | None = None) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    safe_name = SAFE_NAME.sub("-", shard_name.casefold()).strip("-.")
    if not safe_name:
        raise ValueError("Native shard name is empty after normalization")
    source_policy = content_source_policy(
        work_dir,
        task_kind="translation",
        route="native-pass:translation",
    )
    runtime_models = (native_runtime(work_dir) or {}).get("models", {})
    rows = []
    for row in _read_jsonl(source):
        provider = str(row.get("provider", "native-worker"))
        exact_model = model or runtime_models.get(provider)
        if not isinstance(exact_model, str) or not exact_model:
            raise ValueError("E_NATIVE_MODEL_BINDING_REQUIRED: import with an exact model or configured runtime binding")
        rows.append(
            _validate_row(
                row,
                content_policy=source_policy,
                provenance_model=exact_model,
                provenance_route=provider,
            )
        )
    ids = [row["target_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Native shard contains duplicate target IDs")
    destination = work_dir / ".game-l10n" / "native" / "shards" / f"{safe_name}.jsonl"
    _atomic_jsonl(destination, rows)
    return {
        "shard": safe_name,
        "records": len(rows),
        "success": sum(row["status"] == "success" for row in rows),
        "failed": sum(row["status"] != "success" for row in rows),
        "artifact": str(destination),
    }


def import_native_wave(
    work_dir: Path,
    manifest_path: Path,
    source_dir: Path,
    wave: int,
) -> dict[str, Any]:
    """Import each fully validated task atomically; preserve independent peers."""

    require_control_plane_ready(work_dir)
    if not isinstance(wave, int) or isinstance(wave, bool) or wave < 1:
        raise ValueError("Native wave must be a positive integer")
    try:
        source_dir.resolve().relative_to(work_dir.resolve())
    except ValueError as exc:
        raise ValueError("Native wave results must stay inside the private workspace") from exc
    if not source_dir.is_dir():
        raise ValueError("Native wave result directory does not exist")

    manifest = _load_task_manifest(work_dir, manifest_path)
    plan_fingerprint = _native_control_plan_fingerprint(manifest)
    dispatch = _wave_relay_dispatch_path(work_dir, plan_fingerprint, wave)
    runtime = native_runtime(work_dir) or {}
    guarded_host = runtime.get("host") == "omp" and runtime.get("transport", "collaboration") == "collaboration"
    if dispatch.is_file() and (guarded_host or "launch_contract" in json.loads(dispatch.read_text(encoding="utf-8"))):
        isolation_summary = native_launch_summary(work_dir, dispatch)
        if ISOLATION_ISSUE in isolation_summary["issue_codes"]:
            raise ValueError(ISOLATION_ISSUE)
        receipt = _validated_native_wave_relay_receipt(work_dir, plan_fingerprint, wave, manifest=manifest)
        if receipt["issue_codes"]:
            raise ValueError("E_NATIVE_WORKER_NOT_STARTED: runtime repair must precede import")
        if receipt.get("recovery") == INTERRUPTED_REWRITE_RECOVERY:
            payload = json.loads(dispatch.read_text(encoding="utf-8"))
            if any((work_dir / item["result"]).resolve().parent != source_dir.resolve()
                   for item in payload["task_bindings"]):
                raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID: results directory changed")
    tasks = [task for task in manifest["tasks"] if task["wave"] == wave]
    if not tasks:
        raise ValueError("Native task manifest does not contain the requested wave")

    planned: list[tuple[Path, list[dict[str, Any]]]] = []
    wave_ids: set[str] = set()
    bound_segments: dict[str, Segment] = {}
    errors: list[ValueError] = []
    source_binding_invalid = False
    for task in tasks:
        try:
            expected_ids = _validate_manifest_task_file(manifest_path, manifest, task)
            destination = _manifest_shard_path(work_dir, manifest, task)
            result_path = source_dir / task["file"]
            if not result_path.is_file():
                if not destination.is_file():
                    raise ValueError("Native wave result is incomplete")
                result_path = destination
            raw_rows = _read_jsonl(result_path)
            result_ids = [row.get("target_id") for row in raw_rows]
            if result_ids != expected_ids:
                raise ValueError("Native wave result does not match its exclusive task binding")
            if wave_ids.intersection(result_ids):
                raise ValueError("Native wave results overlap")
            try:
                rows = _validate_manifest_rows(
                    work_dir, manifest, raw_rows, bound_segments=bound_segments
                )
            except _NativeSourceBindingError:
                raise
            except (ValueError, json.JSONDecodeError) as exc:
                if str(exc) in {
                    "E_EXTERNAL_CONTENT_PROVENANCE_DENIED", "E_OUTPUT_PROVENANCE_INVALID",
                }:
                    raise ValueError(str(exc)) from None
                raise ValueError("E_NATIVE_WAVE_RESULT_INVALID: Native wave result is invalid") from None
            if destination.is_file():
                previous = _validate_manifest_rows(
                    work_dir, manifest, _read_jsonl(destination), bound_segments=bound_segments
                )
                if previous != rows:
                    raise ValueError("E_NATIVE_IMPORTED_TASK_CHANGED: preserve the validated task checkpoint")
            wave_ids.update(result_ids)
            planned.append((destination, rows))
        except (ValueError, json.JSONDecodeError) as exc:
            source_binding_invalid |= isinstance(exc, _NativeSourceBindingError)
            errors.append(ValueError(
                "E_NATIVE_WAVE_RESULT_INVALID: Native wave result is invalid"
                if isinstance(exc, json.JSONDecodeError) else str(exc)
            ))

    if source_binding_invalid:
        raise ValueError("E_NATIVE_WAVE_RESULT_INVALID: Native wave source binding is invalid")
    for destination, rows in planned:
        if not destination.is_file():
            _atomic_jsonl(destination, rows)
    if errors:
        raise errors[0]
    receipt = _write_wave_receipt(work_dir, manifest, tasks, wave)

    records = sum(len(rows) for _, rows in planned)
    success = sum(row["status"] == "success" for _, rows in planned for row in rows)
    return {
        "schema": "parr0t.native-wave-import.v1",
        "wave": wave,
        "tasks": len(planned),
        "records": records,
        "success": success,
        "failed": records - success,
        "artifacts": [
            destination.resolve().relative_to(work_dir.resolve()).as_posix()
            for destination, _ in planned
        ],
        "receipt": receipt.resolve().relative_to(work_dir.resolve()).as_posix(),
    }


def _native_control_plan_fingerprint(manifest: dict[str, Any]) -> str:
    def bindings() -> Iterable[dict[str, Any]]:
        yield {"pass_kind": manifest.get("pass_kind", "translation")}
        if manifest.get("route_binding") is not None:
            yield {"route_binding": manifest["route_binding"]}
        source_policy = _bound_manifest_content_source_policy(manifest)
        if source_policy is not None:
            yield {
                "content_source_policy_digest": source_policy[
                    "policy_digest"
                ]
            }
        if "execution_namespace" in manifest:
            yield {"execution_namespace": manifest["execution_namespace"]}
        for task in manifest["tasks"]:
            yield {
                "file": task["file"],
                "wave": task["wave"],
                "slot": task["slot"],
                "records": task["records"],
                "sha256": task["sha256"],
            }

    return stable_hash_sequence(bindings())


def _write_wave_relay_dispatch(
    work_dir: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    results_dir: Path,
    wave: int,
    tasks: list[dict[str, Any]],
    plan_fingerprint: str,
    *, legacy_results: list[dict[str, str]] | None = None,
) -> str:
    """Write the only dispatch artifact the root may give to a wave relay."""

    bindings = [
        {
            "task": (manifest_path.parent / task["file"])
            .resolve()
            .relative_to(work_dir.resolve())
            .as_posix(),
            "result": (results_dir / task["file"])
            .resolve()
            .relative_to(work_dir.resolve())
            .as_posix(),
            "records": task["records"],
            "estimated_material_tokens": task["estimated_material_tokens"],
        }
        for task in tasks
    ]
    unsigned = {
        "schema": NATIVE_WAVE_RELAY_SCHEMA,
        "dispatch_revision": RELAY_DISPATCH_REVISION,
        "pass_kind": manifest["pass_kind"],
        "route_binding": manifest.get("route_binding"),
        "manifest": manifest_path.resolve().relative_to(work_dir.resolve()).as_posix(),
        "manifest_contract_fingerprint": manifest["contract_fingerprint"],
        "plan_fingerprint": plan_fingerprint,
        "wave": wave,
        "task_bindings": bindings,
        "relay_contract": {
            "role": "isolated_wave_relay",
            "history_mode": "none",
            "authority": "dispatch_exact_bindings_only",
            "relay_delegation_allowed": True,
            "all_task_bindings_must_be_attempted_once": True,
            "semantic_task_files_may_be_opened_by_relay": False,
            "semantic_worker_history_mode": "none",
            "semantic_worker_delegation_allowed": False,
            "planning_allowed": False,
            "retry_allowed": False,
            "route_change_allowed": False,
            "raw_child_response_forwarding_allowed": False,
            "child_leak_action": "quarantine_relay_and_return_categorical_receipt",
            "worker_message_template": (
                "Workspace: {workspace}. Execute only the bound {pass_kind} task {task}. "
                "Read {manifest} for its immutable worker, detail, execution, route and "
                "content_source_policy contracts. Read only the exact task and context "
                "artifacts bound by that manifest; unrelated workspace paths and every "
                "outside-workspace local target are forbidden. External translation "
                "services, browser translation, remote dictionaries, implicit web search "
                "and every ungranted external content source are forbidden. Model-provider "
                "transport invokes only the selected model and is not a content source. "
                "Never open the complete source_artifact or delegate. Write one ordered JSONL "
                "result to {result}, with target_id, status, text, provider, issues and "
                "direct-model content_provenance for every bound target. Bind provenance.model "
                "to exact route_binding.model and provenance.route/provider to the separate "
                "route_binding.provider alias. Obey the manifest's one semantic batch contract "
                "and return only the prescribed metadata/artifact path, never source or "
                "translated text."
            ),
            "terminal_seal_command": (
                "game-l10n seal-native-wave-relay --workspace <workspace> "
                "--dispatch <relay-path> --relay-ended"
            ),
            "final_response": "workspace_relative_relay_receipt_path_only",
        },
    }
    path = _wave_relay_dispatch_path(work_dir, plan_fingerprint, wave)
    previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    previous_launch = previous.get("launch_contract") if isinstance(previous, dict) else None
    if legacy_results is None and isinstance(previous_launch, dict):
        legacy_results = previous_launch.get("legacy_results", [])
    launch = launch_contract(
        work_dir,
        _configured_manifest_provider(work_dir, manifest),
        [binding["task"] for binding in bindings],
    )
    _preserve_legacy_launch_fields(launch, previous_launch)
    if launch is not None:
        if "message_isolation" in launch:
            marker = "PARR0T_OMP_RELAY=" + path.resolve().relative_to(work_dir.resolve()).as_posix()
            launch.update(relay_prompt_line=marker, worker_prompt_line=marker)
        if legacy_results:
            preserved = {row["task"] for row in legacy_results}
            launch["tasks"] = [task for task in launch["tasks"] if task not in preserved]
            launch["legacy_results"] = legacy_results
            unsigned["relay_contract"].update(
                all_task_bindings_must_be_attempted_once=False,
                launch_task_selection="unstarted_tasks_only",
                legacy_results_must_not_be_reexecuted=True,
                zero_launch_tasks_action="seal_existing_results_without_dispatch",
            )
        if "worker_prompt" in launch and "message_isolation" in launch and (previous is None or (isinstance(previous_launch, dict) and "worker_dispatches" in previous_launch)):
            for binding, task in zip(bindings, tasks):
                binding["task_sha256"] = task["sha256"]
            launch["worker_dispatches"] = _relay_worker_dispatches(work_dir, path, unsigned, launch)
        if isinstance(previous_launch, dict):
            compatible = {**launch, "runtime_fingerprint": previous_launch.get("runtime_fingerprint")}
            if compatible == previous_launch:
                launch = compatible
        unsigned["launch_contract"] = launch
        if previous is None or "command_argv" in previous:
            unsigned["command_argv"] = _relay_command_argv(work_dir, path, unsigned)
    payload = {**unsigned, "dispatch_fingerprint": stable_hash(unsigned)}
    ledger_path = path.with_name(path.stem + "-launch.json")
    if previous_launch is not None and payload != previous and ledger_path.is_file():
        raise ValueError("E_NATIVE_LAUNCH_INVALID: preserve the recorded execution binding")
    _atomic_json(path, payload)
    return path.resolve().relative_to(work_dir.resolve()).as_posix()


def _wave_relay_dispatch_path(
    work_dir: Path, plan_fingerprint: str, wave: int
) -> Path:
    return (
        work_dir
        / ".game-l10n"
        / "native"
        / "relay"
        / f"{plan_fingerprint[:24]}-wave-{wave:04d}.json"
    )


def _repair_ended_wave_relay(
    work_dir: Path, manifest_path: Path, manifest: dict[str, Any],
    results_dir: Path, wave: int, tasks: list[dict[str, Any]], plan_fingerprint: str,
) -> bool:
    """Explicit ended-relay recovery; never turn old output into model evidence."""
    path = _wave_relay_dispatch_path(work_dir, plan_fingerprint, wave)
    if not path.is_file():
        return False
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    old_launch = payload.get("launch_contract")
    if "launch_contract" in payload and not isinstance(old_launch, dict):
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    unsigned = {key: value for key, value in payload.items() if key != "dispatch_fingerprint"}
    relative_manifest = manifest_path.resolve().relative_to(work_dir.resolve()).as_posix()
    bindings = payload.get("task_bindings")
    if (
        payload.get("schema") != NATIVE_WAVE_RELAY_SCHEMA
        or payload.get("dispatch_fingerprint") != stable_hash(unsigned)
        or payload.get("manifest") != relative_manifest
        or payload.get("manifest_contract_fingerprint") != manifest["contract_fingerprint"]
        or payload.get("plan_fingerprint") != plan_fingerprint or payload.get("wave") != wave
        or payload.get("route_binding") != manifest.get("route_binding")
        or payload.get("pass_kind") != manifest["pass_kind"]
        or not isinstance(bindings, list) or len(bindings) != len(tasks)
    ):
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    for task, binding in zip(tasks, bindings):
        _validate_manifest_task_file(manifest_path, manifest, task)
        expected_task = (manifest_path.parent / task["file"]).resolve().relative_to(work_dir.resolve()).as_posix()
        expected_result = (results_dir / task["file"]).resolve().relative_to(work_dir.resolve()).as_posix()
        if (not isinstance(binding, dict) or binding.get("task") != expected_task
                or binding.get("result") != expected_result or binding.get("records") != task["records"]):
            raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    receipt_path = path.with_name(path.stem + "-receipt.json")
    runtime = native_runtime(work_dir, required=True)
    isolation = runtime_isolation(work_dir, runtime)
    if old_launch is not None:
        same_route = (
            old_launch.get("host") == runtime["host"]
            and old_launch.get("route") == _configured_manifest_provider(work_dir, manifest)
            and old_launch.get("model") == runtime["models"].get(old_launch.get("route"))
            and old_launch.get("transport", "collaboration") == runtime.get("transport", "collaboration")
            and old_launch.get("host_profile_fingerprint") == runtime.get("host_profile_fingerprint")
        )
        same_isolation = old_launch.get("message_isolation") == isolation
        if same_route:
            _, _, ledger = _load_launch_evidence(work_dir, path)
            issue = ledger.get("issue_code")
            policy = runtime.get("host_policy_fingerprint")
            policy_denied = (
                runtime["host"] == "omp" and runtime.get("transport", "collaboration") == "collaboration"
                and issue == "E_NATIVE_WORKER_MODEL_UNAVAILABLE"
                and ledger.get("model_failure_code") == "host_dispatch_denied"
            )
            capability_denied = issue == "E_NATIVE_DISPATCH_UNAVAILABLE"
            if policy_denied or capability_denied:
                # Explicit resume asserts that nested dispatch was restored. A
                # model-policy denial additionally needs a changed bound policy.
                if policy_denied and (not policy or policy == old_launch.get("host_policy_fingerprint")):
                    return False
                if ledger["workers"]:
                    raise ValueError("E_NATIVE_LAUNCH_INVALID: cannot rebind a started worker")
                if ledger["capability"] != "unavailable":
                    raise ValueError("E_NATIVE_LAUNCH_INVALID: terminal denial evidence changed")
                if not receipt_path.is_file():
                    if policy_denied:
                        raise ValueError("E_NATIVE_RELAY_RECEIPT_REQUIRED")
                    return False
                _validated_native_wave_relay_receipt(
                    work_dir, plan_fingerprint, wave, manifest=manifest,
                    launch_evidence={"issue_codes": [issue]},
                )
                require_native_workers_ended(work_dir, path, require_terminal_evidence=True)
                # The host may have admitted a worker before the ledger recorded it.
                if "worker_dispatches" in old_launch:
                    digest = _file_sha256(path)
                    for task in old_launch["tasks"]:
                        key = hashlib.sha256(f"{digest}\0{task}".encode()).hexdigest()
                        admission = work_dir / ".game-l10n/native/omp-launch-admissions" / f"{key}.json"
                        if admission.is_file() and json.loads(admission.read_text(encoding="utf-8"))["state"] == "ended":
                            raise ValueError("E_NATIVE_LAUNCH_INVALID: cannot rebind a started worker")
            elif same_isolation:
                return False
        _unstarted_launch_ledger(work_dir, path, payload)
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict) or receipt.get("dispatch_fingerprint") != payload["dispatch_fingerprint"]
                or receipt.get("receipt_fingerprint") != stable_hash({k: v for k, v in receipt.items() if k != "receipt_fingerprint"})):
            raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    bound_segments: dict[str, Segment] = {}

    def validate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _validate_manifest_rows(work_dir, manifest, rows, bound_segments=bound_segments)

    legacy_results = []
    previously_preserved = {row["task"] for row in (old_launch or {}).get("legacy_results", [])}
    for task, binding in zip(tasks, bindings):
        result = work_dir / binding["result"]
        if result.is_file():
            if (old_launch is not None and binding["task"] not in previously_preserved
                    or old_launch is None and runtime["host"] == "omp"
                    and runtime.get("transport", "collaboration") == "collaboration"):
                # A result written without a recorded launch cannot be silently
                # relabeled as the newly selected model's output.
                archive = result.with_name(result.name + ".unstarted-original-" + _file_sha256(result))
                if archive.exists() and archive.read_bytes() != result.read_bytes():
                    raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
                result.replace(archive)
                continue
            _complete_ended_task_result(
                work_dir / binding["task"], result, expected_records=task["records"],
                validate_rows=validate_rows,
                expected_provider=(manifest.get("route_binding") or {}).get("provider"),
            )
            legacy_results.append({"task": binding["task"], "result_sha256": _file_sha256(result)})
    # Archive before removing stale canonical metadata. A crash leaves the old
    # dispatch resumable, and no completed result or imported shard is removed.
    for metadata in (path, receipt_path, path.with_name(path.stem + "-launch.json")):
        if metadata.is_file():
            original = metadata.read_bytes()
            archive = metadata.with_name(metadata.name + ".legacy-" + hashlib.sha256(original).hexdigest())
            if archive.exists() and archive.read_bytes() != original:
                raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
            archive.write_bytes(original)
            if metadata != path:
                metadata.unlink()
    _write_wave_relay_dispatch(work_dir, manifest_path, manifest, results_dir, wave, tasks,
                               plan_fingerprint, legacy_results=legacy_results)
    return True


def _read_structurally_bound_result(
    task: Path,
    result: Path,
    *,
    validate_rows: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> tuple[list[str], list[dict[str, Any]]] | None:
    """Return a complete ordered result without exposing semantic fields."""

    if not result.is_file():
        return None
    try:
        expected_ids = _task_target_ids(task, require_payload=True)
        raw_rows = _read_jsonl(result)
        rows = (
            validate_rows(raw_rows) if validate_rows is not None
            else [_validate_row(row) for row in raw_rows]
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if [row["target_id"] for row in rows] != expected_ids:
        return None
    return expected_ids, rows


def _archive_ended_result(result: Path, original: bytes) -> None:
    digest = hashlib.sha256(original).hexdigest()
    archive = result.with_name(f"{result.name}.ended-original-{digest}")
    if archive.exists():
        if archive.read_bytes() != original:
            raise ValueError("E_NATIVE_RELAY_INVALID: original archive changed")
    else:
        archive.write_bytes(original)


def _unstarted_rewrite_rows(task: Path) -> list[dict[str, Any]]:
    return [
        {"target_id": target, "status": "error", "text": "",
         "provider": "native-control", "issues": ["worker_not_started"]}
        for target in _task_target_ids(task, require_payload=True)
    ]


def _interrupted_rewrite_slots(payload: dict, launch: dict | None) -> set[str]:
    contract = payload.get("launch_contract") or {}
    if (
        payload.get("pass_kind") != "rewrite" or contract.get("host") != "omp"
        or contract.get("transport", "collaboration") != "collaboration"
        or not contract.get("worker_dispatches") or not launch
        or set(launch["issue_codes"]) - {"E_NATIVE_WORKER_NOT_STARTED", "E_NATIVE_RELAY_PREFLIGHT_REQUIRED"}
    ):
        raise ValueError("E_NATIVE_INTERRUPTED_REWRITE_INVALID: require an interrupted OMP rewrite binding")
    return {Path(task).name for task in launch["unstarted_tasks"]}


def _complete_ended_task_result(
    task: Path,
    result: Path,
    *,
    expected_records: int,
    validate_rows: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
    expected_provider: str | None = None,
    interrupted: bool = False,
) -> bool:
    """Preserve an ordered partial result and fill only its missing targets."""

    if _read_structurally_bound_result(task, result, validate_rows=validate_rows) is not None:
        return False
    if result.exists() and not result.is_file():
        raise ValueError("E_NATIVE_RELAY_INVALID: result path is not a file")

    expected_ids = _task_target_ids(task, require_payload=True)
    if len(expected_ids) != expected_records:
        raise ValueError("E_NATIVE_RELAY_INVALID: ended task record count changed")

    preserved: dict[str, dict[str, Any]] = {}
    issue_by_target: dict[str, str] = {}
    default_issue = "worker_ended_without_result"
    if result.is_file():
        original = result.read_bytes()
        default_issue = "worker_result_missing_target"
        partial_rows: list[dict[str, Any]] = []
        observed_target_ids: list[str] = []
        for line in original.splitlines():
            if not line.strip():
                continue
            raw: dict[str, Any] | None = None
            try:
                decoded = json.loads(line.decode("utf-8-sig"))
                raw = decoded if isinstance(decoded, dict) else None
                if not isinstance(raw, dict):
                    raise ValueError("Result row must be an object")
                if isinstance(raw.get("target_id"), str):
                    observed_target_ids.append(raw["target_id"])
                partial_rows.append(
                    validate_rows([raw])[0] if validate_rows is not None
                    else _validate_row(raw)
                )
            except (UnicodeError, ValueError, TypeError):
                target_id = raw.get("target_id") if isinstance(raw, dict) else None
                issue = "worker_result_schema_invalid"
                if isinstance(raw, dict):
                    try:
                        normalized = _validate_row(raw)
                    except (ValueError, TypeError):
                        normalized = None
                    if normalized is not None:
                        issue = "worker_result_contract_invalid"
                        if (
                            expected_provider is not None
                            and normalized["status"] != "error"
                            and normalized.get("provider") != expected_provider
                        ):
                            issue = "worker_result_provider_mismatch"
                if isinstance(target_id, str) and target_id in expected_ids:
                    issue_by_target[target_id] = issue
                else:
                    default_issue = issue
        expected_positions = {target_id: index for index, target_id in enumerate(expected_ids)}
        partial_ids = [row["target_id"] for row in partial_rows]
        binding_ids = observed_target_ids or partial_ids
        if (
            len(binding_ids) != len(set(binding_ids))
            or any(target_id not in expected_positions for target_id in binding_ids)
            or [expected_positions[target_id] for target_id in binding_ids]
            != sorted(expected_positions[target_id] for target_id in binding_ids)
        ):
            # An ended worker's invalid ID binding must not block its peers.
            # Keep the original bytes as evidence and route the whole task as
            # an operational failure; none of its candidates are safe to keep.
            issue_by_target = {
                target_id: "worker_result_binding_invalid" for target_id in expected_ids
            }
            partial_rows = []
        # Any result reaching this repair path will be rewritten. Preserve its
        # exact original bytes once so malformed/provider/binding evidence is
        # never collapsed into a generic missing-output diagnosis.
        _archive_ended_result(result, original)
        preserved = {row["target_id"]: row for row in partial_rows}

    completed_rows = [
        preserved.get(
            target_id,
            {
                "target_id": target_id,
                "status": "error",
                "text": "",
                "provider": "native-control",
                "issues": ["worker_interrupted" if interrupted else issue_by_target.get(target_id, default_issue)],
            },
        )
        for target_id in expected_ids
    ]
    _atomic_jsonl(result, completed_rows)
    return True


def seal_native_wave_relay(
    work_dir: Path,
    dispatch_path: Path,
    *,
    ended_tasks: tuple[str, ...] = (),
    relay_ended: bool = False,
    confirm_rewrite_workers_stopped: bool = False,
) -> dict[str, Any]:
    """Seal one relay and deterministically queue workers that ended without output."""

    require_control_plane_ready(work_dir)
    resolved = dispatch_path.resolve()
    relay_root = (work_dir / ".game-l10n" / "native" / "relay").resolve()
    try:
        resolved.relative_to(relay_root)
    except ValueError as exc:
        raise ValueError("E_NATIVE_RELAY_INVALID: dispatch escaped relay directory") from exc
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("E_NATIVE_RELAY_INVALID: dispatch is unreadable") from exc
    legacy_fields = {
        "schema",
        "pass_kind",
        "route_binding",
        "manifest",
        "manifest_contract_fingerprint",
        "plan_fingerprint",
        "wave",
        "task_bindings",
        "relay_contract",
        "dispatch_fingerprint",
    }
    fields = legacy_fields | {"dispatch_revision"}
    if not isinstance(payload, dict) or frozenset(payload) not in {
        frozenset(legacy_fields),
        frozenset(fields),
        frozenset(fields | {"launch_contract"}),
        frozenset(fields | {"command_argv"}),
        frozenset(fields | {"launch_contract", "command_argv"}),
    }:
        raise ValueError("E_NATIVE_RELAY_INVALID: dispatch shape is invalid")
    active_fields = set(payload)
    unsigned = {
        key: payload[key] for key in active_fields if key != "dispatch_fingerprint"
    }
    if (
        payload.get("schema") != NATIVE_WAVE_RELAY_SCHEMA
        or (
            "dispatch_revision" in payload
            and payload.get("dispatch_revision") != RELAY_DISPATCH_REVISION
        )
        or payload.get("pass_kind") not in PASS_KINDS
        or (
            payload.get("route_binding") is not None
            and (
                not isinstance(payload["route_binding"], dict)
                or set(payload["route_binding"]) not in (
                    {"provider", "attempt"},
                    {"provider", "model", "attempt"},
                )
                or not isinstance(payload["route_binding"].get("provider"), str)
                or not payload["route_binding"]["provider"]
                or (
                    "model" in payload["route_binding"]
                    and (
                        not isinstance(
                            payload["route_binding"].get("model"), str
                        )
                        or not payload["route_binding"]["model"]
                    )
                )
                or not isinstance(payload["route_binding"].get("attempt"), int)
                or isinstance(payload["route_binding"]["attempt"], bool)
                or payload["route_binding"]["attempt"] < 1
            )
        )
        or not isinstance(payload.get("wave"), int)
        or isinstance(payload.get("wave"), bool)
        or payload["wave"] < 1
        or payload.get("dispatch_fingerprint") != stable_hash(unsigned)
        or not isinstance(payload.get("task_bindings"), list)
        or not payload["task_bindings"]
        or not isinstance(payload.get("relay_contract"), dict)
        or payload["relay_contract"].get("role") != "isolated_wave_relay"
        or payload["relay_contract"].get("authority")
        != "dispatch_exact_bindings_only"
        or payload["relay_contract"].get("raw_child_response_forwarding_allowed")
        is not False
        or payload["relay_contract"].get("terminal_seal_command")
        not in {
            None,
            (
                "game-l10n seal-native-wave-relay --workspace <workspace> "
                "--dispatch <relay-path> --relay-ended"
            ),
        }
    ):
        raise ValueError("E_NATIVE_RELAY_INVALID: dispatch binding is invalid")
    if "command_argv" in payload and "launch_contract" in payload:
        if payload["command_argv"] != _relay_command_argv(work_dir, resolved, payload):
            raise ValueError("E_NATIVE_RELAY_INVALID: lifecycle commands changed")
    if "launch_contract" in payload:
        require_native_workers_ended(
            work_dir, resolved, operator_confirmed_stop=confirm_rewrite_workers_stopped,
        )
    if confirm_rewrite_workers_stopped and not relay_ended:
        raise ValueError("E_NATIVE_INTERRUPTED_REWRITE_INVALID: require relay termination confirmation")

    if relay_ended and ended_tasks:
        raise ValueError(
            "E_NATIVE_RELAY_INVALID: relay-ended and explicit ended tasks are mutually exclusive"
        )
    if len(set(ended_tasks)) != len(ended_tasks):
        raise ValueError("E_NATIVE_RELAY_INVALID: ended tasks must be unique")

    try:
        manifest_path = (work_dir / payload["manifest"]).resolve()
        manifest_path.relative_to(work_dir.resolve())
        manifest = _load_task_manifest(work_dir, manifest_path)
        if (
            manifest["contract_fingerprint"] != payload["manifest_contract_fingerprint"]
            or _native_control_plan_fingerprint(manifest) != payload["plan_fingerprint"]
            # The wave width is whatever prepare accepted (1..64), not the default.
            or len(payload["task_bindings"]) > int(manifest["max_workers"])
        ):
            raise ValueError("Relay manifest binding changed")
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError("E_NATIVE_RELAY_INVALID: bound manifest is invalid") from None
    bound_segments: dict[str, Segment] = {}

    def validate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _validate_manifest_rows(
            work_dir, manifest, rows, bound_segments=bound_segments
        )

    bindings_by_name: dict[str, tuple[Path, Path, int]] = {}
    for binding in payload["task_bindings"]:
        if not isinstance(binding, dict) or set(binding) not in (
            {"task", "result", "records", "estimated_material_tokens"},
            {"task", "result", "records", "estimated_material_tokens", "task_sha256"},
        ):
            raise ValueError("E_NATIVE_RELAY_INVALID: task binding is invalid")
        if (
            not isinstance(binding["task"], str)
            or not isinstance(binding["result"], str)
            or not isinstance(binding["records"], int)
            or isinstance(binding["records"], bool)
            or binding["records"] < 1
            or not isinstance(binding["estimated_material_tokens"], int)
            or isinstance(binding["estimated_material_tokens"], bool)
            or binding["estimated_material_tokens"] < 1
        ):
            raise ValueError("E_NATIVE_RELAY_INVALID: task binding values are invalid")
        task = (work_dir / binding["task"]).resolve()
        result = (work_dir / binding["result"]).resolve()
        try:
            task.relative_to(work_dir.resolve())
            result.relative_to(work_dir.resolve())
        except ValueError as exc:
            raise ValueError("E_NATIVE_RELAY_INVALID: task binding escaped workspace") from exc
        if not task.is_file():
            raise ValueError("E_NATIVE_RELAY_INVALID: bound task is unavailable")
        task_name = task.name
        if task_name in bindings_by_name:
            raise ValueError("E_NATIVE_RELAY_INVALID: task names must be unique")
        bindings_by_name[task_name] = (task, result, binding["records"])

    effective_ended_tasks = ended_tasks
    runtime = native_runtime(work_dir) or {}
    guarded_host = runtime.get("host") == "omp" and runtime.get("transport", "collaboration") == "collaboration"
    launch = native_launch_summary(work_dir, resolved) if "launch_contract" in payload or guarded_host else None
    unstarted = _interrupted_rewrite_slots(payload, launch) if confirm_rewrite_workers_stopped else set()
    launch_blocked = bool(launch and launch["issue_codes"] and not confirm_rewrite_workers_stopped)
    if relay_ended:
        effective_ended_tasks = tuple(
            name
            for name, (task, result, _) in bindings_by_name.items()
            if not launch_blocked
            if name not in unstarted
            if _read_structurally_bound_result(task, result, validate_rows=validate_rows) is None
        )
    elif launch_blocked and ended_tasks:
        raise ValueError("E_NATIVE_WORKER_NOT_STARTED: restore the relay before sealing worker errors")

    unknown_ended = [
        name for name in effective_ended_tasks if name not in bindings_by_name
    ]
    if unknown_ended:
        raise ValueError("E_NATIVE_RELAY_INVALID: ended task is not bound to this wave")

    ended_tasks_recorded = 0
    for name in unstarted:
        task, result, expected_records = bindings_by_name[name]
        rows = _unstarted_rewrite_rows(task)
        if len(rows) != expected_records:
            raise ValueError("E_NATIVE_RELAY_INVALID: unstarted task record count changed")
        if result.is_file():
            existing = _read_structurally_bound_result(task, result)
            if existing is not None and existing[1] == rows:
                continue
            _archive_ended_result(result, result.read_bytes())
        _atomic_jsonl(result, rows)
        ended_tasks_recorded += 1
    for name in effective_ended_tasks:
        task, result, expected_records = bindings_by_name[name]
        ended_tasks_recorded += int(
            _complete_ended_task_result(
                task,
                result,
                expected_records=expected_records,
                validate_rows=validate_rows,
                interrupted=confirm_rewrite_workers_stopped,
                expected_provider=(
                    manifest["route_binding"]["provider"]
                    if manifest.get("route_binding") is not None
                    else None
                ),
            )
        )

    ready = sum(
        _read_structurally_bound_result(task, result, validate_rows=validate_rows) is not None
        for task, result, _ in bindings_by_name.values()
    )

    total = len(payload["task_bindings"])
    issue_codes = (
        launch["issue_codes"] if launch_blocked
        else ([] if ready == total else ["E_NATIVE_WAVE_INCOMPLETE"])
    )
    dispatch_relative = resolved.relative_to(work_dir.resolve()).as_posix()
    receipt_unsigned = {
        "schema": NATIVE_WAVE_RELAY_RECEIPT_SCHEMA,
        "dispatch": dispatch_relative,
        "dispatch_fingerprint": payload["dispatch_fingerprint"],
        "wave": payload["wave"],
        "counts": {
            "expected_tasks": total,
            "ready_tasks": ready,
            "pending_tasks": total - ready,
        },
        "issue_codes": issue_codes,
    }
    if confirm_rewrite_workers_stopped:
        receipt_unsigned["recovery"] = INTERRUPTED_REWRITE_RECOVERY
    receipt = {
        **receipt_unsigned,
        "receipt_fingerprint": stable_hash(receipt_unsigned),
    }
    receipt_path = resolved.with_name(f"{resolved.stem}-receipt.json")
    _atomic_json(receipt_path, receipt)
    return {
        "schema": NATIVE_WAVE_RELAY_RECEIPT_SCHEMA,
        "receipt": receipt_path.relative_to(work_dir.resolve()).as_posix(),
        "wave": payload["wave"],
        "counts": receipt_unsigned["counts"],
        "issue_codes": issue_codes,
        "route_binding": payload.get("route_binding"),
        **({"recovery": receipt["recovery"]} if confirm_rewrite_workers_stopped else {}),
        "ended_tasks_recorded": ended_tasks_recorded,
    }


def _validated_native_wave_relay_receipt(
    work_dir: Path,
    plan_fingerprint: str,
    wave: int,
    *,
    manifest: dict[str, Any],
    allow_runtime_repair: bool = False,
    launch_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dispatch_path = _wave_relay_dispatch_path(work_dir, plan_fingerprint, wave).resolve()
    receipt_path = dispatch_path.with_name(f"{dispatch_path.stem}-receipt.json")
    if not receipt_path.is_file():
        raise ValueError("E_NATIVE_RELAY_RECEIPT_REQUIRED")
    try:
        dispatch = json.loads(dispatch_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID") from exc
    receipt_fields = {
        "schema",
        "dispatch",
        "dispatch_fingerprint",
        "wave",
        "counts",
        "issue_codes",
        "receipt_fingerprint",
    }
    recovered_rewrite = isinstance(receipt, dict) and receipt.get("recovery") == INTERRUPTED_REWRITE_RECOVERY
    if recovered_rewrite:
        receipt_fields.add("recovery")
    if not isinstance(receipt, dict) or set(receipt) != receipt_fields:
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    unsigned = {
        key: receipt[key] for key in receipt_fields if key != "receipt_fingerprint"
    }
    dispatch_relative = dispatch_path.relative_to(work_dir.resolve()).as_posix()
    bindings = dispatch.get("task_bindings") if isinstance(dispatch, dict) else None
    dispatch_revision = dispatch.get("dispatch_revision") if isinstance(dispatch, dict) else None
    if (
        not isinstance(bindings, list)
        or dispatch_revision not in {None, RELAY_DISPATCH_REVISION}
    ):
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    runtime = native_runtime(work_dir) or {}
    omp_host = runtime.get("host") == "omp" and runtime.get("transport", "collaboration") == "collaboration"
    launch = launch_evidence
    if launch is None and ("launch_contract" in dispatch or omp_host):
        launch = native_launch_summary(work_dir, dispatch_path)
    unstarted = _interrupted_rewrite_slots(dispatch, launch) if recovered_rewrite else set()
    # A blocked receipt is an immutable observation. Capability may have been
    # restored since it was sealed; resume may reopen only this bound blocker.
    if (
        allow_runtime_repair and "launch_contract" in dispatch
        and isinstance(receipt.get("issue_codes"), list) and receipt["issue_codes"]
        and set(receipt["issue_codes"]) <= RUNTIME_ISSUES
        and receipt.get("schema") == NATIVE_WAVE_RELAY_RECEIPT_SCHEMA
        and receipt.get("dispatch") == dispatch_relative
        and receipt.get("dispatch_fingerprint") == dispatch.get("dispatch_fingerprint")
        and receipt.get("wave") == wave
        and receipt.get("receipt_fingerprint") == stable_hash(unsigned)
    ):
        native_launch_summary(work_dir, dispatch_path)
        return receipt
    bound_segments: dict[str, Segment] = {}

    def validate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _validate_manifest_rows(
            work_dir, manifest, rows, bound_segments=bound_segments
        )

    ready = 0
    for binding in bindings:
        if (
            not isinstance(binding, dict)
            or not isinstance(binding.get("task"), str)
            or not isinstance(binding.get("result"), str)
        ):
            raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
        task = (work_dir / binding["task"]).resolve()
        result = (work_dir / binding["result"]).resolve()
        try:
            task.relative_to(work_dir.resolve())
            result.relative_to(work_dir.resolve())
        except ValueError as exc:
            raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID") from exc
        if dispatch_revision == RELAY_DISPATCH_REVISION:
            # New dispatches use the exact same validator as import. A result
            # cannot be terminal-ready under a looser relay-only definition.
            bound_result = _read_structurally_bound_result(
                task, result, validate_rows=validate_rows
            )
        else:
            # Legacy terminal receipts used shape-only readiness. Retain that
            # path so an old checkpoint can reach deterministic repair once.
            bound_result = _read_structurally_bound_result(task, result)
            if bound_result is None:
                bound_result = _read_structurally_bound_result(
                    task, result, validate_rows=validate_rows
                )
        if task.name in unstarted and (
            bound_result is None or bound_result[1] != _unstarted_rewrite_rows(task)
        ):
            raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
        ready += int(bound_result is not None)
    total = len(bindings)
    expected_counts = {
        "expected_tasks": total,
        "ready_tasks": ready,
        "pending_tasks": total - ready,
    }
    expected_issues = (
        launch["issue_codes"] if launch and launch["issue_codes"] and not recovered_rewrite
        else ([] if ready == total else ["E_NATIVE_WAVE_INCOMPLETE"])
    )
    if (
        receipt.get("schema") != NATIVE_WAVE_RELAY_RECEIPT_SCHEMA
        or receipt.get("dispatch") != dispatch_relative
        or receipt.get("dispatch_fingerprint") != dispatch.get("dispatch_fingerprint")
        or receipt.get("wave") != wave
        or receipt.get("counts") != expected_counts
        or receipt.get("issue_codes") != expected_issues
        or receipt.get("receipt_fingerprint") != stable_hash(unsigned)
    ):
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    return receipt


def _native_control_state_path(
    work_dir: Path, manifest: dict[str, Any], plan_fingerprint: str
) -> Path:
    binding = stable_hash(
        {
            "source_fingerprint": manifest["source_fingerprint"],
            "plan_fingerprint": plan_fingerprint,
        }
    )[:24]
    return work_dir / ".game-l10n" / "native" / "control" / f"{binding}.json"


def _new_native_control_state(
    manifest: dict[str, Any], plan_fingerprint: str
) -> dict[str, Any]:
    created_at_epoch = time.time()
    budget_approved = (
        manifest.get("execution_contract", {})
        .get("bulk_budget_gate", {})
        .get("user_approved")
        is True
    )
    return {
        "schema": NATIVE_CONTROL_STATE_SCHEMA,
        "source_fingerprint": manifest["source_fingerprint"],
        "plan_fingerprint": plan_fingerprint,
        "contract_fingerprint": manifest["contract_fingerprint"],
        "created_at_epoch": created_at_epoch,
        "budget_started_at_epoch": created_at_epoch if budget_approved else None,
        "active_wave": 0,
        "control_steps_in_wave": 0,
        "last_progress_marker": "",
        "unchanged_observations": 0,
        "last_action": "",
        "input_tokens": 0,
        "provider_cost_usd": 0.0,
        "usage_event_ids": [],
        "checkpoint_initialized": False,
        "imported_wave_count": 0,
        "imported_tasks": 0,
    }


def _load_native_control_state(
    path: Path, manifest: dict[str, Any], plan_fingerprint: str
) -> dict[str, Any]:
    if not path.is_file():
        return _new_native_control_state(manifest, plan_fingerprint)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("E_NATIVE_CONTROL_STATE_INVALID: state is unreadable") from exc
    legacy_required = {
        "schema",
        "source_fingerprint",
        "plan_fingerprint",
        "contract_fingerprint",
        "created_at_epoch",
        "active_wave",
        "control_steps_in_wave",
        "last_progress_marker",
        "unchanged_observations",
        "last_action",
        "input_tokens",
        "provider_cost_usd",
        "usage_event_ids",
    }
    v2_required = legacy_required | {
        "checkpoint_initialized",
        "imported_wave_count",
        "imported_tasks",
    }
    required = v2_required | {"budget_started_at_epoch"}
    if (
        isinstance(state, dict)
        and state.get("schema") == LEGACY_NATIVE_CONTROL_STATE_SCHEMA
        and set(state) == legacy_required
    ):
        state["schema"] = LEGACY_NATIVE_CONTROL_STATE_SCHEMA_V2
        state["checkpoint_initialized"] = False
        state["imported_wave_count"] = 0
        state["imported_tasks"] = 0
    contract_changed = (
        isinstance(state, dict)
        and state.get("contract_fingerprint") != manifest["contract_fingerprint"]
    )
    if (
        isinstance(state, dict)
        and state.get("schema") == LEGACY_NATIVE_CONTROL_STATE_SCHEMA_V2
        and set(state) == v2_required
    ):
        state["schema"] = NATIVE_CONTROL_STATE_SCHEMA
        budget_approved = (
            manifest.get("execution_contract", {})
            .get("bulk_budget_gate", {})
            .get("user_approved")
            is True
        )
        state["budget_started_at_epoch"] = (
            state["created_at_epoch"]
            if budget_approved and not contract_changed
            else None
        )
    if not isinstance(state, dict) or set(state) != required:
        raise ValueError("E_NATIVE_CONTROL_STATE_INVALID: state shape is invalid")
    if (
        state.get("schema") != NATIVE_CONTROL_STATE_SCHEMA
        or state.get("source_fingerprint") != manifest["source_fingerprint"]
        or state.get("plan_fingerprint") != plan_fingerprint
        or not isinstance(state.get("created_at_epoch"), (int, float))
        or isinstance(state.get("created_at_epoch"), bool)
        or (
            state.get("budget_started_at_epoch") is not None
            and (
                not isinstance(state["budget_started_at_epoch"], (int, float))
                or isinstance(state["budget_started_at_epoch"], bool)
                or not math.isfinite(state["budget_started_at_epoch"])
                or state["budget_started_at_epoch"] < 0
            )
        )
        or not isinstance(state.get("active_wave"), int)
        or isinstance(state.get("active_wave"), bool)
        or state["active_wave"] < 0
        or not isinstance(state.get("control_steps_in_wave"), int)
        or isinstance(state.get("control_steps_in_wave"), bool)
        or state["control_steps_in_wave"] < 0
        or not isinstance(state.get("last_progress_marker"), str)
        or not isinstance(state.get("unchanged_observations"), int)
        or isinstance(state.get("unchanged_observations"), bool)
        or state["unchanged_observations"] < 0
        or not isinstance(state.get("last_action"), str)
        or not isinstance(state.get("input_tokens"), int)
        or isinstance(state.get("input_tokens"), bool)
        or state["input_tokens"] < 0
        or not isinstance(state.get("provider_cost_usd"), (int, float))
        or isinstance(state.get("provider_cost_usd"), bool)
        or not math.isfinite(state["provider_cost_usd"])
        or state["provider_cost_usd"] < 0
        or not isinstance(state.get("usage_event_ids"), list)
        or not all(
            isinstance(item, str) and USAGE_EVENT.fullmatch(item)
            for item in state["usage_event_ids"]
        )
        or len(state["usage_event_ids"]) != len(set(state["usage_event_ids"]))
        or not isinstance(state.get("checkpoint_initialized"), bool)
        or not isinstance(state.get("imported_wave_count"), int)
        or isinstance(state.get("imported_wave_count"), bool)
        or state["imported_wave_count"] < 0
        or not isinstance(state.get("imported_tasks"), int)
        or isinstance(state.get("imported_tasks"), bool)
        or state["imported_tasks"] < 0
    ):
        raise ValueError("E_NATIVE_CONTROL_STATE_INVALID: state values are invalid")
    # A regenerated manifest may bind an approved budget without changing the task plan.
    # Preserve prior usage and imported checkpoints, but start a new decision generation.
    if contract_changed:
        state["active_wave"] = 0
        state["control_steps_in_wave"] = 0
        state["last_progress_marker"] = ""
        state["unchanged_observations"] = 0
        state["last_action"] = ""
    budget_approved = (
        manifest.get("execution_contract", {})
        .get("bulk_budget_gate", {})
        .get("user_approved")
        is True
    )
    if budget_approved and state["budget_started_at_epoch"] is None:
        state["budget_started_at_epoch"] = time.time()
    elif not budget_approved:
        state["budget_started_at_epoch"] = None
    state["contract_fingerprint"] = manifest["contract_fingerprint"]
    return state


def _record_native_usage(
    state: dict[str, Any],
    *,
    usage_event_id: str | None,
    input_tokens: int,
    provider_cost_usd: float,
) -> None:
    if input_tokens < 0 or not math.isfinite(provider_cost_usd) or provider_cost_usd < 0:
        raise ValueError("Native usage values must be non-negative")
    if not usage_event_id:
        if input_tokens or provider_cost_usd:
            raise ValueError("Native usage requires a stable usage event ID")
        return
    if not USAGE_EVENT.fullmatch(usage_event_id):
        raise ValueError("Native usage event ID is invalid")
    if usage_event_id in state["usage_event_ids"]:
        return
    if len(state["usage_event_ids"]) >= 10000:
        raise ValueError("E_NATIVE_CONTROL_STATE_INVALID: too many usage events")
    state["usage_event_ids"].append(usage_event_id)
    state["input_tokens"] += input_tokens
    state["provider_cost_usd"] = round(
        float(state["provider_cost_usd"]) + provider_cost_usd, 8
    )


def _imported_native_waves(
    work_dir: Path, manifest_path: Path, manifest: dict[str, Any]
) -> tuple[list[int], int]:
    imported_waves: list[int] = []
    imported_tasks = 0
    bound_segments: dict[str, Segment] = {}
    for wave in sorted({task["wave"] for task in manifest["tasks"]}):
        tasks = [task for task in manifest["tasks"] if task["wave"] == wave]
        receipt_path = _wave_receipt_path(work_dir, manifest, tasks, wave)
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "E_NATIVE_CONTROL_STATE_INVALID: wave receipt is unreadable"
                ) from exc
            if (
                not isinstance(receipt, dict)
                or set(receipt)
                != {"schema", "source_fingerprint", "wave", "tasks"}
                or receipt.get("schema") != NATIVE_WAVE_RECEIPT_SCHEMA
                or receipt.get("source_fingerprint")
                != manifest["source_fingerprint"]
                or receipt.get("wave") != wave
                or not isinstance(receipt.get("tasks"), list)
                or len(receipt["tasks"]) != len(tasks)
            ):
                raise ValueError(
                    "E_NATIVE_CONTROL_STATE_INVALID: wave receipt binding is invalid"
                )
            for task, item in zip(tasks, receipt["tasks"], strict=True):
                expected_shard = _manifest_shard_path(work_dir, manifest, task)
                expected_relative = (
                    expected_shard.resolve()
                    .relative_to(work_dir.resolve())
                    .as_posix()
                )
                if (
                    not isinstance(item, dict)
                    or set(item)
                    != {
                        "task_file",
                        "task_sha256",
                        "shard",
                        "shard_size",
                        "records",
                    }
                    or item.get("task_file") != task["file"]
                    or item.get("task_sha256") != task["sha256"]
                    or item.get("shard") != expected_relative
                    or item.get("records") != task["records"]
                    or not isinstance(item.get("shard_size"), int)
                    or item["shard_size"] < 0
                    or not expected_shard.is_file()
                    or expected_shard.stat().st_size != item["shard_size"]
                ):
                    raise ValueError(
                        "E_NATIVE_CONTROL_STATE_INVALID: wave receipt no longer matches"
                    )
            imported_waves.append(wave)
            imported_tasks += len(tasks)
            continue

        # Upgrade a legacy imported wave once. Future steps use its small receipt
        # instead of rereading every previously imported text-bearing shard.
        present: list[bool] = []
        for task in tasks:
            shard = _manifest_shard_path(work_dir, manifest, task)
            if not shard.is_file():
                present.append(False)
                continue
            try:
                rows = _validate_manifest_rows(
                    work_dir, manifest, _read_jsonl(shard), bound_segments=bound_segments
                )
            except (ValueError, json.JSONDecodeError):
                raise ValueError(
                    "E_NATIVE_CONTROL_IMPORTED_SHARD_INVALID: imported shard is invalid"
                ) from None
            expected_ids = _task_target_ids(
                manifest_path.parent / task["file"],
                require_payload=(
                    manifest.get("version", 0)
                    >= LEGACY_WORKER_SPLIT_MANIFEST_VERSION
                ),
            )
            if [row["target_id"] for row in rows] != expected_ids:
                raise ValueError(
                    "E_NATIVE_CONTROL_IMPORTED_SHARD_INVALID: imported shard binding changed"
                )
            present.append(True)
        if all(present):
            _write_wave_receipt(work_dir, manifest, tasks, wave)
            imported_waves.append(wave)
            imported_tasks += len(tasks)
        else:
            break
    return imported_waves, imported_tasks


def _native_budget_issue(
    execution_contract: dict[str, Any], state: dict[str, Any], next_wave: int
) -> str:
    gate = execution_contract.get("bulk_budget_gate", {})
    if gate.get("user_approved") is not True:
        return ""
    max_elapsed = gate.get("max_elapsed_minutes")
    elapsed_minutes = _native_budget_elapsed_minutes(state)
    if isinstance(max_elapsed, int) and elapsed_minutes >= max_elapsed:
        return "E_NATIVE_ELAPSED_BUDGET_EXHAUSTED"
    max_input = gate.get("max_input_tokens")
    if isinstance(max_input, int) and state["input_tokens"] >= max_input:
        return "E_NATIVE_INPUT_BUDGET_EXHAUSTED"
    max_cost = gate.get("max_provider_cost_usd")
    if (
        gate.get("provider_cost_unmetered") is not True
        and isinstance(max_cost, (int, float))
        and not isinstance(max_cost, bool)
        and state["provider_cost_usd"] >= max_cost
    ):
        return "E_NATIVE_COST_BUDGET_EXHAUSTED"
    return ""


def _native_budget_elapsed_minutes(state: dict[str, Any]) -> float:
    started_at = state.get("budget_started_at_epoch")
    if not isinstance(started_at, (int, float)) or isinstance(started_at, bool):
        return 0.0
    return max(0.0, (time.time() - float(started_at)) / 60.0)


def _native_task_dispatch_allowed(
    manifest: dict[str, Any], task: dict[str, Any]
) -> bool:
    if task.get("dispatch_authorized") is True:
        return True
    gate = manifest.get("execution_contract", {}).get("bulk_budget_gate", {})
    # Older manifests may contain framework-inferred pilot locks.  They are not
    # user authority and must not strand a valid inherited checkpoint.
    return gate.get("active") is True and gate.get("user_approved") is not True


def _review_split_chain_path(
    work_dir: Path, root_plan_fingerprint: str
) -> Path:
    return (
        work_dir
        / ".game-l10n"
        / "native"
        / "review-splits"
        / root_plan_fingerprint[:24]
        / "chain.json"
    )


def _review_manifest_rows(
    work_dir: Path, manifest_path: Path, manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Read one completed review pass in manifest order inside the data plane."""

    rows: list[dict[str, Any]] = []
    bound_segments: dict[str, Segment] = {}
    for task in manifest["tasks"]:
        shard = _manifest_shard_path(work_dir, manifest, task)
        if not shard.is_file():
            raise ValueError("E_NATIVE_REVIEW_SPLIT_PARENT_INCOMPLETE")
        expected_ids = _task_target_ids(
            manifest_path.parent / task["file"], require_payload=True
        )
        validated = _validate_manifest_rows(
            work_dir,
            manifest,
            _read_jsonl(shard),
            bound_segments=bound_segments,
        )
        if [row["target_id"] for row in validated] != expected_ids:
            raise ValueError("E_NATIVE_REVIEW_SPLIT_PARENT_INVALID")
        rows.extend(validated)
    if len(rows) != manifest["records"]:
        raise ValueError("E_NATIVE_REVIEW_SPLIT_PARENT_INCOMPLETE")
    return rows


def _manifest_task_segments(
    manifest_path: Path, manifest: dict[str, Any]
) -> list[Segment]:
    segments: list[Segment] = []
    for task in manifest["tasks"]:
        task_path = manifest_path.parent / task["file"]
        _validate_manifest_task_file(manifest_path, manifest, task)
        for row in _read_jsonl(task_path):
            try:
                segment = Segment(**row["segment"])
            except (KeyError, TypeError, ValueError):
                raise ValueError("E_NATIVE_REVIEW_SPLIT_SOURCE_INVALID") from None
            if segment.id != row.get("target_id"):
                raise ValueError("E_NATIVE_REVIEW_SPLIT_SOURCE_INVALID")
            segments.append(segment)
    if (
        len(segments) != manifest.get("records")
        or stable_hash_sequence(asdict(segment) for segment in segments)
        != manifest.get("source_fingerprint")
    ):
        raise ValueError("E_NATIVE_REVIEW_SPLIT_SOURCE_INVALID")
    return segments


def _failed_review_segments(
    work_dir: Path, manifest_path: Path, manifest: dict[str, Any]
) -> list[Segment]:
    segments = _manifest_task_segments(manifest_path, manifest)
    rows = _review_manifest_rows(work_dir, manifest_path, manifest)
    by_id = {segment.id: segment for segment in segments}
    failed_ids = [row["target_id"] for row in rows if row["status"] != "success"]
    if any(target_id not in by_id for target_id in failed_ids):
        raise ValueError("E_NATIVE_REVIEW_SPLIT_SOURCE_INVALID")
    return [by_id[target_id] for target_id in failed_ids]


def _inherit_native_control_usage(
    work_dir: Path,
    parent_manifest: dict[str, Any],
    child_manifest: dict[str, Any],
) -> None:
    parent_plan = _native_control_plan_fingerprint(parent_manifest)
    child_plan = _native_control_plan_fingerprint(child_manifest)
    parent_path = _native_control_state_path(work_dir, parent_manifest, parent_plan)
    child_path = _native_control_state_path(work_dir, child_manifest, child_plan)
    if child_path.is_file():
        return
    parent_state = _load_native_control_state(
        parent_path, parent_manifest, parent_plan
    )
    child_state = _new_native_control_state(child_manifest, child_plan)
    for key in (
        "created_at_epoch",
        "budget_started_at_epoch",
        "input_tokens",
        "provider_cost_usd",
        "usage_event_ids",
    ):
        child_state[key] = parent_state[key]
    _atomic_json(child_path, child_state)


def _configured_manifest_provider(work_dir: Path, manifest: dict[str, Any]) -> str:
    bound = (manifest.get("route_binding") or {}).get("provider")
    if bound:
        return bound
    config_path = work_dir / "game-l10n.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    kind = (manifest.get("content_source_policy") or {}).get("task_kind", manifest.get("pass_kind"))
    role = {"translation": "translators", "rewrite": "rewriters", "triage": "review_triagers"}.get(kind, "reviewers")
    if manifest.get("execution_contract", {}).get("review_tier") == "triage":
        role = "review_triagers"
    return next(iter(config.get("routing", {}).get(role, [])), "")


def _native_review_call_budget(work_dir: Path, manifest_path: Path, manifest: dict[str, Any]) -> int | None:
    additional = _additional_fallback_review_budget(work_dir, manifest_path, manifest)
    if additional is not None:
        return additional
    provider = _configured_manifest_provider(work_dir, manifest)
    if not provider:
        return None
    config_path = work_dir / "game-l10n.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    routing = config.get("routing", {})
    triage = (manifest.get("execution_contract", {}).get("review_tier") == "triage"
              or (manifest.get("content_source_policy") or {}).get("task_kind") == "triage")
    runtime = native_runtime(work_dir) if provider.startswith("native-") else None
    model = (manifest.get("route_binding") or {}).get("model") or (runtime or {}).get("models", {}).get(provider, "")
    limit = (routing.get("triage_max_calls") if triage else review_call_limit(
        provider, routing.get("reviewers", [provider] if provider else []),
        routing.get("review_attempt_limits", {}), model=model,
        tier_limit=routing.get("strong_max_calls"),
    ))
    if limit is None:
        return None
    from .models import AttemptRecord
    prior = 0
    for segment in _manifest_task_segments(manifest_path, manifest):
        saved = segment.metadata.get("_parr0t_review_state", {})
        attempts = [AttemptRecord(**item) for item in saved.get("attempts", [])]
        prior = max(prior, judge_calls(attempts, provider, model=model,
                                      stage_prefix="review-triage" if triage else "review"))
    return max(0, limit - prior)


def _prepare_review_split_child(
    work_dir: Path,
    current_manifest_path: Path,
    current_manifest: dict[str, Any],
    failed: list[Segment],
    root_plan_fingerprint: str,
    *,
    attempted_records: int,
    root_records: int,
    review_call_budget: int | None = None,
) -> tuple[Path, Path] | None:
    split = current_manifest["review_split"]
    parent_task_records = int(
        current_manifest.get("task_max_records", current_manifest["max_records"])
    )
    parent_task_material = int(
        current_manifest.get(
            "task_max_material_tokens", current_manifest["max_material_tokens"]
        )
    )
    if _review_split_stop_reason(
        current_manifest,
        failed,
        attempted_records=attempted_records,
        root_records=root_records,
        review_call_budget=review_call_budget,
    ):
        return None
    next_depth = split["depth"] + 1
    split_root = _review_split_chain_path(
        work_dir, root_plan_fingerprint
    ).parent
    child_root = split_root / f"depth-{next_depth:02d}"
    child_source = child_root / "source.jsonl"
    child_tasks = child_root / "tasks"
    child_results = child_root / "results"
    _atomic_jsonl(child_source, (asdict(segment) for segment in failed))

    child_max_records = max(1, parent_task_records // 2)
    child_max_material = max(
        max(_estimated_material_tokens(segment) for segment in failed),
        parent_task_material // 2,
    )
    execution_contract = current_manifest["execution_contract"]
    control_turn_budget_user_approved = (
        execution_contract.get("control_turn_budget_user_approved") is True
    )
    gate = execution_contract["bulk_budget_gate"]
    prepare_native_tasks(
        work_dir,
        child_source,
        failed,
        child_tasks,
        pass_kind="review",
        max_records=child_max_records,
        min_records=min(int(current_manifest["min_records"]), child_max_records),
        max_material_tokens=child_max_material,
        max_workers=int(current_manifest["max_workers"]),
        max_elapsed_minutes=gate.get("max_elapsed_minutes"),
        max_input_tokens=gate.get("max_input_tokens"),
        max_provider_cost_usd=gate.get("max_provider_cost_usd"),
        provider_cost_unmetered=bool(gate.get("provider_cost_unmetered")),
        user_approved_budget=bool(gate.get("user_approved")),
        max_root_model_calls_per_wave=(
            execution_contract.get("max_root_model_calls_per_wave")
            if control_turn_budget_user_approved
            else None
        ),
        max_root_model_calls_per_turn=(
            execution_contract.get("max_root_model_calls_per_turn")
            if control_turn_budget_user_approved
            else None
        ),
        max_root_input_tokens_per_turn=(
            execution_contract.get("max_root_input_tokens_per_turn")
            if control_turn_budget_user_approved
            else None
        ),
        control_turn_budget_user_approved=control_turn_budget_user_approved,
        review_tier=execution_contract.get("review_tier"),
        review_split_max_depth=execution_contract.get("review_optimization_policy", {}).get(
            "max_depth", current_manifest["worker_contract"]["failure_split"]["max_depth"]
        ),
        review_split_max_record_amplification=execution_contract.get(
            "review_optimization_policy", {}
        ).get("max_record_amplification"),
        _review_split={
            "depth": next_depth,
            "semantic_passes": split["semantic_passes"] + 1,
            "parent_manifest": current_manifest_path.resolve()
            .relative_to(work_dir.resolve())
            .as_posix(),
            "root_source_fingerprint": split["root_source_fingerprint"],
        },
        language_binding=current_manifest.get("language_binding"),
        route_binding=current_manifest.get("route_binding"),
        _content_source_policy=_bound_manifest_content_source_policy(current_manifest),
    )
    child_manifest_path = child_tasks / "manifest.json"
    child_manifest = _load_task_manifest(work_dir, child_manifest_path)
    _inherit_native_control_usage(work_dir, current_manifest, child_manifest)
    child_results.mkdir(parents=True, exist_ok=True)
    return child_manifest_path, child_results


def _review_split_stop_reason(
    current_manifest: dict[str, Any],
    failed: list[Segment],
    *,
    attempted_records: int,
    root_records: int,
    review_call_budget: int | None = None,
) -> str:
    split = current_manifest["review_split"]
    parent_task_records = int(
        current_manifest.get("task_max_records", current_manifest["max_records"])
    )
    if review_call_budget is not None and split["semantic_passes"] >= review_call_budget:
        return "review_call_limit"
    if len(failed) <= 1:
        return "smallest_safe_record_isolated"
    if parent_task_records <= 1 or (
        current_manifest.get("tasks")
        and all(task["records"] <= 1 for task in current_manifest["tasks"])
    ):
        return "single_record_tasks_reached"
    policy = current_manifest.get("execution_contract", {}).get("review_optimization_policy", {})
    max_depth = policy.get(
        "max_depth", current_manifest.get("worker_contract", {}).get(
            "failure_split", {}
        ).get("max_depth", DEFAULT_REVIEW_SPLIT_MAX_DEPTH)
    )
    if split["depth"] >= max_depth:
        return "split_depth_limit"
    amplification = policy.get("max_record_amplification")
    if amplification is not None and (
        root_records < 1 or (attempted_records + len(failed)) / root_records > amplification
    ):
        return "record_amplification_limit"
    return ""


def _load_review_split_chain(
    work_dir: Path, root_manifest_path: Path, root_plan_fingerprint: str
) -> dict[str, Any] | None:
    path = _review_split_chain_path(work_dir, root_plan_fingerprint)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID") from exc
    required = {
        "schema",
        "root_manifest",
        "root_plan_fingerprint",
        "manifests",
        "current_manifest",
        "current_results",
        "complete",
    }
    root_relative = root_manifest_path.resolve().relative_to(work_dir.resolve()).as_posix()
    if (
        not isinstance(state, dict)
        or set(state) != required
        or state.get("schema") != NATIVE_REVIEW_SPLIT_CHAIN_SCHEMA
        or state.get("root_manifest") != root_relative
        or state.get("root_plan_fingerprint") != root_plan_fingerprint
        or not isinstance(state.get("manifests"), list)
        or not state["manifests"]
        or state["manifests"][0] != root_relative
        or not all(isinstance(item, str) and item for item in state["manifests"])
        or state.get("current_manifest") != state["manifests"][-1]
        or not isinstance(state.get("current_results"), str)
        or not isinstance(state.get("complete"), bool)
    ):
        raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID")
    for key in ("current_manifest", "current_results"):
        resolved = (work_dir / state[key]).resolve()
        try:
            resolved.relative_to(work_dir.resolve())
        except ValueError as exc:
            raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID") from exc
    previous = ""
    previous_path: Path | None = None
    previous_manifest: dict[str, Any] | None = None
    root_source_fingerprint = ""
    for depth, relative in enumerate(state["manifests"]):
        resolved = (work_dir / relative).resolve()
        try:
            resolved.relative_to(work_dir.resolve())
        except ValueError as exc:
            raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID") from exc
        bound_manifest = _load_task_manifest(work_dir, resolved)
        split = bound_manifest.get("review_split", {})
        if depth == 0:
            if (
                resolved != root_manifest_path.resolve()
                or _native_control_plan_fingerprint(bound_manifest)
                != root_plan_fingerprint
            ):
                raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID")
            root_source_fingerprint = bound_manifest["source_fingerprint"]
        if (
            bound_manifest.get("version", 0) < TASK_MANIFEST_VERSION
            or bound_manifest.get("pass_kind") != "review"
            or split.get("depth") != depth
            or split.get("semantic_passes") != depth + 1
            or split.get("parent_manifest") != previous
            or split.get("root_source_fingerprint") != root_source_fingerprint
        ):
            raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID")
        if previous_path is not None and previous_manifest is not None:
            try:
                expected_segments = _failed_review_segments(
                    work_dir, previous_path, previous_manifest
                )
                child_segments = _manifest_task_segments(resolved, bound_manifest)
            except ValueError:
                raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID") from None
            if [asdict(segment) for segment in child_segments] != [
                asdict(segment) for segment in expected_segments
            ]:
                raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID")
        previous = relative
        previous_path = resolved
        previous_manifest = bound_manifest
    if len(state["manifests"]) > 1:
        current_manifest_path = (work_dir / state["current_manifest"]).resolve()
        expected_results = current_manifest_path.parent.parent / "results"
        if (work_dir / state["current_results"]).resolve() != expected_results.resolve():
            raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID")
    return state


def _native_control_step_single(
    work_dir: Path,
    manifest_path: Path,
    results_dir: Path,
    *,
    usage_event_id: str | None = None,
    input_tokens: int = 0,
    provider_cost_usd: float = 0.0,
    require_relay_receipt: bool = False,
    require_runtime: bool = False,
    resume_relay: bool = False,
    confirm_rewrite_workers_stopped: bool = False,
) -> dict[str, Any]:
    """Advance one manifest pass without asking a model to schedule or recover it."""

    require_control_plane_ready(work_dir)
    if require_runtime:
        native_runtime(work_dir, required=True)
    try:
        results_dir.resolve().relative_to(work_dir.resolve())
    except ValueError as exc:
        raise ValueError("Native result directory must stay inside the private workspace") from exc
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_task_manifest(work_dir, manifest_path)
    if manifest.get("version") not in {
        LEGACY_WORKER_SPLIT_MANIFEST_VERSION,
        TASK_MANIFEST_VERSION,
    }:
        raise ValueError(
            "E_NATIVE_CONTROL_MANIFEST_UPGRADE_REQUIRED: deterministic stepping requires v6 or v7"
        )
    # Probe the selected native API route before creating controller state.
    from .native_runtime import native_worker_ceiling
    try:
        provider = _configured_manifest_provider(work_dir, manifest)
        if provider:
            native_worker_ceiling(work_dir, provider, 1)
    except ValueError as error:
        existing = _wave_relay_dispatch_path(work_dir, _native_control_plan_fingerprint(manifest), 1)
        if str(error) != ISOLATION_ISSUE or not existing.is_file():
            raise
    plan_fingerprint = _native_control_plan_fingerprint(manifest)
    state_path = _native_control_state_path(work_dir, manifest, plan_fingerprint)
    state = _load_native_control_state(state_path, manifest, plan_fingerprint)
    _record_native_usage(
        state,
        usage_event_id=usage_event_id,
        input_tokens=input_tokens,
        provider_cost_usd=provider_cost_usd,
    )

    wave_tasks: dict[int, list[dict[str, Any]]] = {}
    for task in manifest["tasks"]:
        wave_tasks.setdefault(task["wave"], []).append(task)
    waves = sorted(wave_tasks)
    if state["checkpoint_initialized"] is not True:
        imported_waves, imported_tasks = _imported_native_waves(
            work_dir, manifest_path, manifest
        )
        state["checkpoint_initialized"] = True
        state["imported_wave_count"] = len(imported_waves)
        state["imported_tasks"] = imported_tasks
    else:
        imported_wave_count = state["imported_wave_count"]
        if imported_wave_count > len(waves):
            raise ValueError("E_NATIVE_CONTROL_STATE_INVALID: imported wave count is invalid")
        imported_waves = waves[:imported_wave_count]
        imported_tasks = sum(len(wave_tasks[wave]) for wave in imported_waves)
        if imported_tasks != state["imported_tasks"]:
            raise ValueError("E_NATIVE_CONTROL_STATE_INVALID: imported task count is invalid")
    import_summary: dict[str, Any] | None = None
    rebound_relay = False
    resumed_tasks: list[dict[str, Any]] | None = None
    next_wave = (
        waves[state["imported_wave_count"]]
        if state["imported_wave_count"] < len(waves)
        else None
    )

    if next_wave is not None:
        tasks = wave_tasks[next_wave]
        ready_paths = [
            results_dir / task["file"]
            for task in tasks
            if (results_dir / task["file"]).is_file()
        ]
        ready_results = len(ready_paths)
        result_fingerprint = stable_hash(
            [
                {"file": path.name, "sha256": _file_sha256(path)}
                for path in ready_paths
            ]
        )
        relay_dispatch_path = _wave_relay_dispatch_path(
            work_dir, plan_fingerprint, next_wave
        )
        relay_receipt_path = relay_dispatch_path.with_name(
            f"{relay_dispatch_path.stem}-receipt.json"
        )
        if confirm_rewrite_workers_stopped and not relay_receipt_path.is_file():
            if not relay_dispatch_path.is_file():
                raise ValueError("E_NATIVE_INTERRUPTED_REWRITE_INVALID: no existing relay")
            sealed = seal_native_wave_relay(
                work_dir, relay_dispatch_path, relay_ended=True,
                confirm_rewrite_workers_stopped=True,
            )
            ready_results = sealed["counts"]["ready_tasks"]
        rebound_relay = (
            resume_relay and not confirm_rewrite_workers_stopped and require_runtime and _repair_ended_wave_relay(
                work_dir, manifest_path, manifest, results_dir, next_wave, tasks, plan_fingerprint,
            )
        )
        if (resume_relay and not confirm_rewrite_workers_stopped and require_runtime and not rebound_relay
                and relay_dispatch_path.is_file() and not relay_receipt_path.is_file()):
            # --resume-relay asserts that the old relay ended. Seal its actual
            # outputs once; ordinary waits never make this assertion.
            seal_native_wave_relay(work_dir, relay_dispatch_path, relay_ended=True)
        relay_receipt_fingerprint = (
            _file_sha256(relay_receipt_path)
            if relay_receipt_path.is_file()
            else "missing"
        )
        progress_marker = (
            f"{imported_tasks}:{next_wave}:{ready_results}:"
            f"{result_fingerprint}:{relay_receipt_fingerprint}:"
            f"validation-{RESULT_VALIDATION_REVISION}:dispatch-{RELAY_DISPATCH_REVISION}"
        )
        if state["active_wave"] == next_wave:
            state["control_steps_in_wave"] += 1
        else:
            state["active_wave"] = next_wave
            state["control_steps_in_wave"] = 1
        if state["last_progress_marker"] == progress_marker:
            state["unchanged_observations"] += 1
        else:
            state["last_progress_marker"] = progress_marker
            state["unchanged_observations"] = 0

        execution_contract = manifest["execution_contract"]
        configured_max_root_calls = (
            execution_contract.get("max_root_model_calls_per_wave")
            if execution_contract.get("control_turn_budget_user_approved") is True
            else None
        )
        max_root_calls = (
            int(configured_max_root_calls)
            if configured_max_root_calls is not None
            else None
        )
        relay_issue = ""
        unobserved_relay = False
        if relay_dispatch_path.is_file():
            existing_dispatch = json.loads(relay_dispatch_path.read_text(encoding="utf-8"))
            runtime = native_runtime(work_dir) or {}
            guarded_host = runtime.get("host") == "omp" and runtime.get("transport", "collaboration") == "collaboration"
            if "launch_contract" in existing_dispatch or guarded_host:
                observed = native_launch_summary(work_dir, relay_dispatch_path)
                unobserved_relay = (
                    observed.get("launch_observed") is False
                    and not relay_receipt_path.is_file()
                )
                if unobserved_relay:
                    ready_results = 0
                if ISOLATION_ISSUE in observed["issue_codes"]:
                    relay_issue = ISOLATION_ISSUE
        budget_issue = _native_budget_issue(execution_contract, state, next_wave)
        control_limit_hit = max_root_calls is not None and state["control_steps_in_wave"] > max_root_calls
        if (
            not relay_issue
            and
            require_relay_receipt
            and not unobserved_relay
            and state["active_wave"] == next_wave
            and (
                state["last_action"] == "dispatch_isolated_wave_relay"
                or _wave_relay_dispatch_path(
                    work_dir, plan_fingerprint, next_wave
                ).is_file()
            )
        ):
            try:
                relay_receipt = _validated_native_wave_relay_receipt(
                    work_dir, plan_fingerprint, next_wave, manifest=manifest,
                    allow_runtime_repair=resume_relay and not confirm_rewrite_workers_stopped,
                )
            except ValueError as exc:
                code = str(exc)
                relay_issue = (
                    code
                    if code
                    in {
                        "E_NATIVE_RELAY_RECEIPT_REQUIRED",
                        "E_NATIVE_RELAY_RECEIPT_INVALID",
                    }
                    else "E_NATIVE_RELAY_RECEIPT_INVALID"
                )
            else:
                if relay_receipt["issue_codes"]:
                    relay_issue = next(
                        (code for code in relay_receipt["issue_codes"] if code in RUNTIME_ISSUES),
                        "E_NATIVE_WAVE_INCOMPLETE",
                    )
                if (
                    resume_relay and not confirm_rewrite_workers_stopped
                    and relay_issue == "E_NATIVE_WORKER_NOT_STARTED"
                    and observed["issue_codes"] == ["E_NATIVE_WORKER_NOT_STARTED"]
                    and existing_dispatch.get("launch_contract", {}).get("worker_dispatches")
                    and guarded_host and not budget_issue and not control_limit_hit
                ):
                    # Use the original dispatch: its ledger and OMP admissions
                    # exclude all started slots from the next relay's preflight.
                    require_native_workers_ended(work_dir, relay_dispatch_path)
                    unstarted = set(observed["unstarted_tasks"])
                    jobs = [
                        (task, (work_dir / binding["result"]).resolve())
                        for task, binding in zip(tasks, existing_dispatch["task_bindings"], strict=True)
                        if binding["task"] in unstarted
                    ]
                    if len(jobs) != len(unstarted) or not jobs or any(
                        result != (results_dir / task["file"]).resolve() for task, result in jobs
                    ):
                        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID: retry slot binding changed")
                    resumed_tasks = [task for task, _ in jobs]
                    for task in resumed_tasks:
                        if not _native_task_dispatch_allowed(manifest, task):
                            raise ValueError("E_NATIVE_DISPATCH_NOT_AUTHORIZED")
                        _validate_manifest_task_file(manifest_path, manifest, task)
                    for path, kind in (
                        *((result, "unstarted-original") for _, result in jobs if result.exists()),
                        (relay_receipt_path, "legacy"),
                    ):
                        original = path.read_bytes()
                        archive = path.with_name(f"{path.name}.{kind}-{hashlib.sha256(original).hexdigest()}")
                        if archive.exists() and archive.read_bytes() != original:
                            raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID: retry archive changed")
                        path.replace(archive)
                    rebound_relay = True
                elif (
                    resume_relay and not confirm_rewrite_workers_stopped
                    and relay_issue in RUNTIME_ISSUES
                    and relay_issue not in {
                        "E_NATIVE_WORKER_NOT_STARTED", "E_NATIVE_WORKER_MODEL_UNAVAILABLE",
                        "E_NATIVE_DISPATCH_UNAVAILABLE",
                    }
                ):
                    # Reopen the same transport after repair. Preserve all result
                    # files, launches, successful shards, route and attempt counts.
                    relay_receipt_path.unlink()
                    state["unchanged_observations"] = 0
                    state["last_progress_marker"] = ""
                    ready_results = 0
                    relay_issue = ""
        if rebound_relay:
            state["unchanged_observations"] = 0
            state["last_progress_marker"] = ""
            ready_results = 0
            relay_issue = ""
        awaiting_live_relay = (
            relay_issue == "E_NATIVE_RELAY_RECEIPT_REQUIRED"
            and relay_dispatch_path.is_file()
        )
        if awaiting_live_relay:
            if budget_issue:
                action, issue_codes = "checkpoint_and_stop", [budget_issue]
            elif max_root_calls is not None and state["control_steps_in_wave"] > max_root_calls:
                action, issue_codes = "checkpoint_and_stop", ["E_NATIVE_WAVE_CONTROL_LIMIT"]
            else:
                # A bounded host wait is a polling interval, not a worker
                # deadline. Only the relay's terminal seal ends this lifecycle.
                action, issue_codes = "wait_for_active_wave", []
        elif relay_issue:
            action = "repair_native_runtime" if relay_issue in RUNTIME_ISSUES else "checkpoint_and_stop"
            issue_codes = [relay_issue]
        elif budget_issue and ready_results != len(tasks):
            action = "checkpoint_and_stop"
            issue_codes = [budget_issue]
        elif (
            max_root_calls is not None
            and state["control_steps_in_wave"] > max_root_calls
            and ready_results != len(tasks)
        ):
            action = "checkpoint_and_stop"
            issue_codes = ["E_NATIVE_WAVE_CONTROL_LIMIT"]
        elif ready_results == len(tasks):
            try:
                try:
                    import_summary = import_native_wave(
                        work_dir, manifest_path, results_dir, next_wave
                    )
                except (ValueError, json.JSONDecodeError):
                    if not require_relay_receipt:
                        raise
                    # The verified terminal receipt permits deterministic repair
                    # of ended output only. Keep valid rows and archive rejected
                    # bytes; route invalid/missing targets as operational failures.
                    seal_native_wave_relay(
                        work_dir, relay_dispatch_path, relay_ended=True
                    )
                    import_summary = import_native_wave(
                        work_dir, manifest_path, results_dir, next_wave
                    )
            except (ValueError, json.JSONDecodeError):
                action = "checkpoint_and_stop"
                issue_codes = ["E_NATIVE_WAVE_RESULT_INVALID"]
            else:
                state["imported_wave_count"] += 1
                state["imported_tasks"] += len(tasks)
                imported_waves = waves[: state["imported_wave_count"]]
                imported_tasks = state["imported_tasks"]
                next_wave = (
                    waves[state["imported_wave_count"]]
                    if state["imported_wave_count"] < len(waves)
                    else None
                )
                state["last_progress_marker"] = f"{imported_tasks}:{next_wave or 0}:0"
                state["unchanged_observations"] = 0
                if next_wave is None:
                    action = "aggregate_completed_pass"
                    issue_codes = []
                    state["active_wave"] = 0
                    state["control_steps_in_wave"] = 0
                else:
                    state["active_wave"] = next_wave
                    state["control_steps_in_wave"] = 1
                    budget_issue = _native_budget_issue(
                        manifest["execution_contract"], state, next_wave
                    )
                    next_tasks = wave_tasks[next_wave]
                    if budget_issue:
                        action = "checkpoint_and_stop"
                        issue_codes = [budget_issue]
                    elif not all(
                        _native_task_dispatch_allowed(manifest, task)
                        for task in next_tasks
                    ):
                        action = "checkpoint_and_stop"
                        issue_codes = ["E_NATIVE_DISPATCH_NOT_AUTHORIZED"]
                    else:
                        action = "dispatch_isolated_wave_relay"
                        issue_codes = []
        elif relay_dispatch_path.is_file() and not (unobserved_relay or rebound_relay):
            action = "wait_for_active_wave"
            issue_codes = []
        elif ready_results and not (unobserved_relay or rebound_relay):
            action = "wait_for_active_wave"
            issue_codes = ["E_NATIVE_WAVE_INCOMPLETE"]
        elif not all(
            _native_task_dispatch_allowed(manifest, task) for task in tasks
        ):
            action = "checkpoint_and_stop"
            issue_codes = ["E_NATIVE_DISPATCH_NOT_AUTHORIZED"]
        else:
            action = "dispatch_isolated_wave_relay"
            issue_codes = []
    else:
        action = "aggregate_completed_pass"
        issue_codes = []
        state["active_wave"] = 0
        state["control_steps_in_wave"] = 0

    state["last_action"] = (
        "publish_isolated_wave_relay" if action == "dispatch_isolated_wave_relay" else action
    )
    _atomic_json(state_path, state)
    selected_tasks = (
        (resumed_tasks or wave_tasks[next_wave])
        if action == "dispatch_isolated_wave_relay" and next_wave is not None
        else []
    )
    for task in selected_tasks:
        _validate_manifest_task_file(manifest_path, manifest, task)
    if selected_tasks and manifest.get("translation_route_plan") is not None:
        predecessor = manifest.get("translation_predecessor")
        selected_segments = [
            Segment(**row["segment"])
            for task in selected_tasks
            for row in _read_jsonl(manifest_path.parent / task["file"])
        ]
        verified_predecessor = _native_translation_predecessor(
            work_dir, manifest["translation_route_plan"],
            (manifest.get("route_binding") or {}).get("provider", ""),
            work_dir / predecessor["receipt"] if predecessor is not None else None,
            selected_segments,
        )
        if verified_predecessor != predecessor:
            raise ValueError("E_TRANSLATION_PREDECESSOR_INVALID: immutable predecessor binding changed")
    if selected_tasks and require_runtime:
        native_runtime(work_dir, required=True)
    relay_dispatch = (
        _write_wave_relay_dispatch(
            work_dir,
            manifest_path,
            manifest,
            results_dir,
            next_wave,
            selected_tasks,
            plan_fingerprint,
        )
        if selected_tasks and next_wave is not None and not rebound_relay
        else ""
    )
    if action == "dispatch_isolated_wave_relay" and rebound_relay:
        relay_dispatch = relay_dispatch_path.resolve().relative_to(work_dir.resolve()).as_posix()
    if action == "dispatch_isolated_wave_relay":
        state["last_action"] = action
        _atomic_json(state_path, state)
    if action == "repair_native_runtime" and next_wave is not None:
        # Return the existing dispatch without rewriting its launch evidence.
        # A host repair must not require the parent to reconstruct a task.
        relay_dispatch = relay_dispatch_path.resolve().relative_to(work_dir.resolve()).as_posix()
        selected_tasks = wave_tasks[next_wave]
    imported_material_tokens = sum(
        int(task.get("estimated_material_tokens", 0))
        for wave in imported_waves
        for task in wave_tasks[wave]
    )
    planned_material_tokens = sum(
        int(task.get("estimated_material_tokens", 0))
        for task in manifest["tasks"]
    )
    usage_events = len(state["usage_event_ids"])
    return {
        "schema": NATIVE_CONTROL_STEP_SCHEMA,
        "action": action,
        "wave": next_wave or 0,
        "counts": {
            "total_waves": len(waves),
            "imported_waves": len(imported_waves),
            "total_tasks": len(manifest["tasks"]),
            "imported_tasks": imported_tasks,
            "pending_tasks": len(manifest["tasks"]) - imported_tasks,
        },
        "issue_codes": issue_codes,
        "route_binding": manifest.get("route_binding"),
        "resume_relay_command": (
            "game-l10n native-pass-step --workspace <workspace> --manifest <manifest> "
            "--results-dir <results-dir> --resume-relay"
            if action == "repair_native_runtime" else None
        ),
        "runtime_repair": (
            {"scope": "host", "required_capability": "nested_native_dispatch",
             "retry_after": "actual_host_tool_surface_change",
             "automatic_rotation_allowed": False, "isolation_relaxation_allowed": False}
            if set(issue_codes) & {"E_NATIVE_DISPATCH_UNAVAILABLE", ISOLATION_ISSUE} else None
        ),
        "relay": (
            {
                "dispatch": relay_dispatch,
                "prompt": "PARR0T_OMP_RELAY=" + relay_dispatch,
                "worker_prompt_policy": "include_launch_contract_worker_prompt_line_verbatim_in_each_task",
                "tool_transport": tool_transport_contract(prompt=(native_runtime(work_dir) or {}).get("omp_isolation") == "prompt") if (native_runtime(work_dir) or {}).get("host") == "omp"
                    and (native_runtime(work_dir) or {}).get("transport", "collaboration") == "collaboration" else None,
                "role": "isolated_wave_relay",
                "history_mode": "none",
                "tasks": len(selected_tasks),
                "records": sum(task["records"] for task in selected_tasks),
                "final_response": "workspace_relative_relay_receipt_path_only",
                "executor": "native-host-api" if (native_runtime(work_dir) or {}).get("transport") == "native-host-api" else "collaboration",
                "broker_command": ["game-l10n", "run-native-broker", "--workspace", str(work_dir.resolve()),
                                   "--relay", relay_dispatch]
                    if (native_runtime(work_dir) or {}).get("transport") == "native-host-api" else None,
            }
            if relay_dispatch
            else None
        ),
        "usage": {
            "events": usage_events,
            "input_tokens": state["input_tokens"],
            "input_tokens_known": usage_events > 0,
            "telemetry_status": (
                "measured"
                if usage_events > 0
                else "unmeasured"
                if imported_tasks > 0
                else "pending"
            ),
            "estimated_material_tokens_imported": imported_material_tokens,
            "estimated_material_tokens_planned": planned_material_tokens,
            "provider_cost_usd": state["provider_cost_usd"],
            "elapsed_minutes": round(
                _native_budget_elapsed_minutes(state),
                3,
            ),
        },
        "imported_wave": (
            {
                "wave": import_summary["wave"],
                "tasks": import_summary["tasks"],
                "records": import_summary["records"],
                "success": import_summary["success"],
                "failed": import_summary["failed"],
            }
            if import_summary is not None
            else None
        ),
        "state": state_path.resolve().relative_to(work_dir.resolve()).as_posix(),
    }


def native_control_step(
    work_dir: Path,
    manifest_path: Path,
    results_dir: Path,
    *,
    usage_event_id: str | None = None,
    input_tokens: int = 0,
    provider_cost_usd: float = 0.0,
    require_relay_receipt: bool = False,
    require_runtime: bool = False,
    resume_relay: bool = False,
    confirm_rewrite_workers_stopped: bool = False,
) -> dict[str, Any]:
    """Advance a pass and let the core isolate only failed review rows."""

    root_manifest = _load_task_manifest(work_dir, manifest_path)
    if confirm_rewrite_workers_stopped and (
        not resume_relay or not require_runtime or root_manifest.get("pass_kind") != "rewrite"
    ):
        raise ValueError("E_NATIVE_INTERRUPTED_REWRITE_INVALID: require --resume-relay for an OMP rewrite wave")
    if root_manifest.get("pass_kind") == "review" and root_manifest.get("version") < TASK_MANIFEST_VERSION:
        raise ValueError("E_NATIVE_REVIEW_CALL_POLICY_REQUIRED")
    if (
        root_manifest.get("version") < TASK_MANIFEST_VERSION
        or root_manifest.get("pass_kind") != "review"
    ):
        return _native_control_step_single(
            work_dir,
            manifest_path,
            results_dir,
            usage_event_id=usage_event_id,
            input_tokens=input_tokens,
            provider_cost_usd=provider_cost_usd,
            require_relay_receipt=require_relay_receipt,
            require_runtime=require_runtime,
            resume_relay=resume_relay,
            confirm_rewrite_workers_stopped=confirm_rewrite_workers_stopped,
        )

    review_call_budget = _native_review_call_budget(work_dir, manifest_path, root_manifest)
    root_plan_fingerprint = _native_control_plan_fingerprint(root_manifest)
    chain_path = _review_split_chain_path(work_dir, root_plan_fingerprint)
    chain = _load_review_split_chain(
        work_dir, manifest_path, root_plan_fingerprint
    )
    if chain is None:
        current_manifest_path = manifest_path
        current_results_dir = results_dir
        manifest_rel = manifest_path.resolve().relative_to(work_dir.resolve()).as_posix()
        results_rel = results_dir.resolve().relative_to(work_dir.resolve()).as_posix()
        chain = {
            "schema": NATIVE_REVIEW_SPLIT_CHAIN_SCHEMA,
            "root_manifest": manifest_rel,
            "root_plan_fingerprint": root_plan_fingerprint,
            "manifests": [manifest_rel],
            "current_manifest": manifest_rel,
            "current_results": results_rel,
            "complete": False,
        }
    else:
        current_manifest_path = (work_dir / chain["current_manifest"]).resolve()
        current_results_dir = (work_dir / chain["current_results"]).resolve()

    current_manifest = _load_task_manifest(work_dir, current_manifest_path)
    if review_call_budget is not None and current_manifest["review_split"]["semantic_passes"] > review_call_budget:
        raise ValueError("E_REVIEW_CALL_LIMIT_REACHED")
    root_records = int(root_manifest["records"])
    record_attempts = root_records
    for relative in chain["manifests"][1:-1]:
        record_attempts += int(
            _load_task_manifest(work_dir, (work_dir / relative).resolve())["records"]
        )
    if len(chain["manifests"]) > 1:
        record_attempts += int(current_manifest["records"])
    step = _native_control_step_single(
        work_dir,
        current_manifest_path,
        current_results_dir,
        usage_event_id=usage_event_id,
        input_tokens=input_tokens,
        provider_cost_usd=provider_cost_usd,
        require_relay_receipt=require_relay_receipt,
        require_runtime=require_runtime,
        resume_relay=resume_relay,
    )
    split = current_manifest["review_split"]
    split_summary = {
        "depth": split["depth"],
        "semantic_passes": split["semantic_passes"],
        "failed_records": None,
        "chain_complete": chain["complete"],
        "record_attempts": record_attempts,
        "record_amplification_ratio": round(record_attempts / root_records, 6),
        "max_record_amplification_ratio": current_manifest["execution_contract"].get(
            "review_optimization_policy", {}
        ).get("max_record_amplification"),
        "effective_max_depth": current_manifest["execution_contract"].get(
            "review_optimization_policy", {}
        ).get("max_depth", current_manifest["worker_contract"]["failure_split"]["max_depth"]),
        "terminal_reason": "",
    }
    split_summary["remaining_call_budget"] = review_call_budget
    if step["action"] != "aggregate_completed_pass":
        _atomic_json(chain_path, chain)
        return {**step, "review_split": split_summary}

    failed = _failed_review_segments(
        work_dir, current_manifest_path, current_manifest
    )
    split_summary["failed_records"] = len(failed)
    stop_reason = _review_split_stop_reason(
        current_manifest,
        failed,
        attempted_records=record_attempts,
        root_records=root_records,
        review_call_budget=review_call_budget,
    )
    child = _prepare_review_split_child(
        work_dir,
        current_manifest_path,
        current_manifest,
        failed,
        root_plan_fingerprint,
        attempted_records=record_attempts,
        root_records=root_records,
        review_call_budget=review_call_budget,
    )
    if child is None:
        chain["complete"] = True
        split_summary["chain_complete"] = True
        split_summary["terminal_reason"] = stop_reason
        _atomic_json(chain_path, chain)
        return {**step, "review_split": split_summary}

    child_manifest_path, child_results_dir = child
    child_manifest_rel = (
        child_manifest_path.resolve().relative_to(work_dir.resolve()).as_posix()
    )
    child_results_rel = (
        child_results_dir.resolve().relative_to(work_dir.resolve()).as_posix()
    )
    if child_manifest_rel not in chain["manifests"]:
        chain["manifests"].append(child_manifest_rel)
    chain["current_manifest"] = child_manifest_rel
    chain["current_results"] = child_results_rel
    chain["complete"] = False
    _atomic_json(chain_path, chain)
    child_step = _native_control_step_single(
        work_dir, child_manifest_path, child_results_dir,
        require_relay_receipt=require_relay_receipt, require_runtime=require_runtime,
    )
    child_split = _load_task_manifest(work_dir, child_manifest_path)["review_split"]
    return {
        **child_step,
        "review_split": {
            "depth": child_split["depth"],
            "semantic_passes": child_split["semantic_passes"],
            "failed_records": len(failed),
            "chain_complete": False,
            "record_attempts": record_attempts + len(failed),
            "record_amplification_ratio": round(
                (record_attempts + len(failed)) / root_records, 6
            ),
            "max_record_amplification_ratio": current_manifest["execution_contract"].get(
                "review_optimization_policy", {}
            ).get("max_record_amplification"),
            "effective_max_depth": current_manifest["execution_contract"].get(
                "review_optimization_policy", {}
            ).get("max_depth", current_manifest["worker_contract"]["failure_split"]["max_depth"]),
            "terminal_reason": "",
        },
    }


def merge_native_pass(
    work_dir: Path,
    segments: list[Segment],
    destination: Path,
    *,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    order = {segment.id: index for index, segment in enumerate(segments)}
    if len(order) != len(segments):
        raise ValueError("Ordered source contains duplicate target IDs")
    collected: dict[str, dict[str, Any]] = {}
    shard_root = work_dir / ".game-l10n" / "native" / "shards"
    manifest_bound = manifest_path is not None
    expected_shards = 0
    split_chain_complete = True
    shard_bindings: list[
        tuple[Path, list[str] | None, dict[str, Any] | None, bool]
    ] = []
    if manifest_path is not None:
        manifest = _load_task_manifest(work_dir, manifest_path)
        fingerprint = stable_hash_sequence(asdict(segment) for segment in segments)
        if (
            manifest.get("source_fingerprint") != fingerprint
            or manifest.get("records") != len(segments)
        ):
            raise ValueError("Native task manifest targets a different ordered source")
        manifests: list[tuple[Path, dict[str, Any], bool]] = [
            (manifest_path, manifest, False)
        ]
        if (
            manifest.get("version", 0) >= TASK_MANIFEST_VERSION
            and manifest.get("pass_kind") == "review"
        ):
            root_plan = _native_control_plan_fingerprint(manifest)
            chain = _load_review_split_chain(work_dir, manifest_path, root_plan)
            if chain is not None:
                split_chain_complete = chain["complete"]
                previous_relative = chain["root_manifest"]
                for depth, relative in enumerate(chain["manifests"][1:], start=1):
                    child_path = (work_dir / relative).resolve()
                    try:
                        child_path.relative_to(work_dir.resolve())
                    except ValueError as exc:
                        raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID") from exc
                    child = _load_task_manifest(work_dir, child_path)
                    child_split = child.get("review_split", {})
                    if (
                        child.get("pass_kind") != "review"
                        or child.get("version", 0) < TASK_MANIFEST_VERSION
                        or child_split.get("depth") != depth
                        or child_split.get("semantic_passes") != depth + 1
                        or child_split.get("parent_manifest") != previous_relative
                        or child_split.get("root_source_fingerprint") != fingerprint
                    ):
                        raise ValueError("E_NATIVE_REVIEW_SPLIT_STATE_INVALID")
                    manifests.append((child_path, child, True))
                    previous_relative = relative
        for bound_manifest_path, bound_manifest, is_child in manifests:
            expected_shards += len(bound_manifest["tasks"])
            shard_bindings.extend(
                (
                    _manifest_shard_path(work_dir, bound_manifest, task),
                    _task_target_ids(
                        bound_manifest_path.parent / task["file"],
                        require_payload=(
                            bound_manifest.get("version", 0)
                            >= LEGACY_WORKER_SPLIT_MANIFEST_VERSION
                        ),
                    ),
                    bound_manifest,
                    is_child,
                )
                for task in bound_manifest["tasks"]
            )
    else:
        shard_bindings = [
            (path, None, None, False)
            for path in sorted(shard_root.glob("*.jsonl"))
        ]
        expected_shards = len(shard_bindings)

    imported_shards = 0
    bound_segments: dict[str, Segment] = {}
    for shard, expected_ids, bound_manifest, is_child in shard_bindings:
        if not shard.is_file():
            continue
        if expected_ids is None:
            rows = [_validate_row(raw) for raw in _read_jsonl(shard)]
        else:
            try:
                rows = _validate_manifest_rows(
                    work_dir,
                    bound_manifest,
                    _read_jsonl(shard),
                    bound_segments=bound_segments,
                )
            except (ValueError, json.JSONDecodeError):
                raise ValueError("Imported native shard is invalid") from None
        if expected_ids is not None and [row["target_id"] for row in rows] != expected_ids:
            raise ValueError("Imported native shard no longer matches its task binding")
        imported_shards += 1
        for row in rows:
            target_id = row["target_id"]
            if target_id not in order:
                if manifest_bound:
                    raise ValueError("Imported native shard is outside the ordered source")
                raise ValueError(
                    f"Native shard target ID is absent from ordered source: {target_id}"
                )
            if is_child:
                if target_id not in collected:
                    raise ValueError(
                        "Review split child is not bound to an imported parent row"
                    )
                if collected[target_id]["status"] == "success":
                    raise ValueError("Review split attempted to reprocess a successful row")
            elif target_id in collected:
                if manifest_bound:
                    raise ValueError("Imported native shards overlap")
                raise ValueError(f"Native shards overlap on target ID: {target_id}")
            if row["status"] == "success":
                row["candidate_generation"] = 1
                row["candidate_digest"] = stable_hash(row["text"])
            if bound_manifest is not None and bound_manifest.get("pass_kind") == "review":
                previous_calls = collected.get(target_id, {}).get("native_review_attempts", 0)
                row["native_review_attempts"] = previous_calls + int(row.get("semantic_attempts", 1))
            collected[target_id] = row
    merged = [collected[target_id] for target_id in order if target_id in collected]
    _atomic_jsonl(destination, merged)
    missing = len(segments) - len(merged)
    return {
        "records": len(merged),
        "success": sum(row["status"] == "success" for row in merged),
        "failed": sum(row["status"] != "success" for row in merged),
        "missing": missing,
        "complete": missing == 0 and split_chain_complete,
        "review_split_complete": split_chain_complete,
        "manifest_bound": manifest_bound,
        "expected_shards": expected_shards,
        "imported_shards": imported_shards,
        "artifact": str(destination),
    }
