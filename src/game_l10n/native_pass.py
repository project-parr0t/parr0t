from __future__ import annotations

import json
import hashlib
import math
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .control_plane import (
    CONTROL_ENVELOPE_SCHEMA,
    require_control_plane_ready,
    review_control_contract,
)
from .models import Segment
from .native_followup import CANDIDATE_METADATA_KEY
from .store import stable_hash, stable_hash_sequence
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
DEFAULT_REVIEW_MAX_RECORDS = 2_048
DEFAULT_REVIEW_MIN_RECORDS = 64
DEFAULT_REVIEW_MAX_MATERIAL_TOKENS = 262_144
DEFAULT_REVIEW_INITIAL_RECORDS = 256
DEFAULT_REVIEW_INITIAL_MATERIAL_TOKENS = 65_536
DEFAULT_REVIEW_SPLIT_MAX_DEPTH = 11
DEFAULT_REVIEW_SPLIT_MAX_CALLS = 16
DEFAULT_MAX_WORKERS = 8
DEFAULT_BULK_BUDGET_GATE_RECORDS = 4_096
PASS_KINDS = {"translation", "review", "rewrite"}
NATIVE_CONTROL_STATE_SCHEMA = "parr0t.native-control-state.v2"
LEGACY_NATIVE_CONTROL_STATE_SCHEMA = "parr0t.native-control-state.v1"
NATIVE_CONTROL_STEP_SCHEMA = "parr0t.native-control-step.v1"
NATIVE_WAVE_RECEIPT_SCHEMA = "parr0t.native-wave-receipt.v1"
NATIVE_WAVE_RELAY_SCHEMA = "parr0t.native-wave-relay.v1"
NATIVE_WAVE_RELAY_RECEIPT_SCHEMA = "parr0t.native-wave-relay-receipt.v1"
NATIVE_REVIEW_SPLIT_CHAIN_SCHEMA = "parr0t.native-review-split-chain.v1"
USAGE_EVENT = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
RESULT_VALIDATION_REVISION = 2


def _failure_split_contract(
    pass_kind: str, *, manifest_version: int = TASK_MANIFEST_VERSION
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
        "max_depth": DEFAULT_REVIEW_SPLIT_MAX_DEPTH if enabled else 0,
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
    pass_kind: str = "translation", *, manifest_version: int = TASK_MANIFEST_VERSION
) -> dict[str, Any]:
    split_contract = _failure_split_contract(
        pass_kind, manifest_version=manifest_version
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
        "max_model_calls_per_task": 24 if worker_owned_split else 16,
        "max_semantic_model_calls_per_task": split_contract["max_semantic_calls"],
        "per_record_semantic_call_loop_allowed": False,
        "max_prompt_artifacts_per_task": 1,
        "task_payload_schema": TASK_PAYLOAD_SCHEMA,
        "task_payload_contains_bound_segments": True,
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


def _detail_contract(
    pass_kind: str = "translation", *, manifest_version: int = TASK_MANIFEST_VERSION
) -> dict[str, Any]:
    split_contract = _failure_split_contract(
        pass_kind, manifest_version=manifest_version
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
    bulk_budget_gate_records: int,
    max_elapsed_minutes: int | None,
    max_input_tokens: int | None,
    max_provider_cost_usd: float | None,
    provider_cost_unmetered: bool,
    user_approved_budget: bool,
    max_root_model_calls_per_wave: int | None,
    max_root_model_calls_per_turn: int | None,
    max_root_input_tokens_per_turn: int | None,
    control_turn_budget_user_approved: bool,
) -> dict[str, Any]:
    bulk_gate_active = record_count >= bulk_budget_gate_records
    budget_complete = (
        isinstance(max_elapsed_minutes, int)
        and max_elapsed_minutes > 0
        and isinstance(max_input_tokens, int)
        and max_input_tokens > 0
        and (
            provider_cost_unmetered
            or (
                isinstance(max_provider_cost_usd, (int, float))
                and not isinstance(max_provider_cost_usd, bool)
                and max_provider_cost_usd > 0
            )
        )
    )
    budget_approved = bool(user_approved_budget and budget_complete)
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
        "max_root_progress_messages_per_wave": 1,
        "max_worker_model_calls_per_task": (
            24 if pass_kind == "review" else 16
        ),
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
        "progress_reporting": "wave_boundary_or_blocker_only",
        "max_unchanged_progress_observations": 1,
        "max_control_actions_without_progress": 2,
        "capacity_failure_action": "defer_task_and_end_worker_attempt",
        "automatic_root_turn_restart_allowed": False,
        "detail_contract_schema": "parr0t.detail-contract.v1",
        "bulk_budget_gate": {
            "active": bulk_gate_active,
            "activation_records": bulk_budget_gate_records,
            "pilot_waves_without_approved_budget": 1,
            "user_approved": budget_approved,
            "max_elapsed_minutes": max_elapsed_minutes,
            "max_input_tokens": max_input_tokens,
            "max_provider_cost_usd": max_provider_cost_usd,
            "provider_cost_unmetered": provider_cost_unmetered,
            "missing_budget_action": "checkpoint_and_request_user_limit",
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
        contract_fingerprint = payload.get("contract_fingerprint")
        expected_fingerprint = stable_hash(
            {
                "worker_contract": worker_contract,
                "detail_contract": detail_contract,
                "execution_contract": execution_contract,
            }
        )
        if (
            worker_contract
            != _worker_contract(pass_kind, manifest_version=version)
            or detail_contract
            != _detail_contract(pass_kind, manifest_version=version)
        ):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: native task contract was changed")
        if not isinstance(execution_contract, dict) or (
            execution_contract.get("detail_contract_schema")
            != "parr0t.detail-contract.v1"
        ):
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: execution contract is missing")
        if contract_fingerprint != expected_fingerprint:
            raise ValueError("E_DETAIL_CONTRACT_CONFLICT: contract fingerprint is invalid")
        budget_gate = execution_contract.get("bulk_budget_gate", {})
        pilot_waves = budget_gate.get("pilot_waves_without_approved_budget", 0)
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
    binding = stable_hash(binding_source)[:16]
    stem = Path(task["file"]).stem
    return work_dir / ".game-l10n" / "native" / "shards" / f"{binding}-{stem}.jsonl"


def _wave_receipt_path(
    work_dir: Path, manifest: dict[str, Any], tasks: list[dict[str, Any]], wave: int
) -> Path:
    binding = stable_hash(
        {
            "source_fingerprint": manifest["source_fingerprint"],
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


def _task_target_ids(task_path: Path, *, require_payload: bool = False) -> list[str]:
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
        valid_empty = (
            isinstance(text, str)
            and text == ""
            and segment.metadata.get("ui_empty_target") is not None
            and not validate_integrity(segment, text, [])
        )
        if (
            not isinstance(text, str)
            or (not text.strip() and not valid_empty)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or digest != stable_hash(text)
        ):
            raise ValueError(
                f"{pass_kind} target {segment.id!r} has an invalid candidate binding"
            )


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
    bulk_budget_gate_records: int = DEFAULT_BULK_BUDGET_GATE_RECORDS,
    max_elapsed_minutes: int | None = None,
    max_input_tokens: int | None = None,
    max_provider_cost_usd: float | None = None,
    provider_cost_unmetered: bool = False,
    user_approved_budget: bool = False,
    max_root_model_calls_per_wave: int | None = None,
    max_root_model_calls_per_turn: int | None = None,
    max_root_input_tokens_per_turn: int | None = None,
    control_turn_budget_user_approved: bool = False,
    _review_split: dict[str, Any] | None = None,
) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    if pass_kind not in PASS_KINDS:
        raise ValueError(f"pass_kind must be one of: {', '.join(sorted(PASS_KINDS))}")
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
    if not 1 <= max_records <= DEFAULT_REVIEW_MAX_RECORDS:
        raise ValueError("max_records must be between 1 and 2048")
    if min_records is None:
        min_records = min(
            DEFAULT_REVIEW_MIN_RECORDS if pass_kind == "review" else DEFAULT_MIN_RECORDS,
            max_records,
        )
    if not 1 <= min_records <= max_records:
        raise ValueError("min_records must be between 1 and max_records")
    if not 256 <= max_material_tokens <= 1_000_000:
        raise ValueError("max_material_tokens must be between 256 and 1000000")
    if not 1 <= max_workers <= 64:
        raise ValueError("max_workers must be between 1 and 64")
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
    if not 1 <= bulk_budget_gate_records <= DEFAULT_BULK_BUDGET_GATE_RECORDS:
        raise ValueError(
            "bulk_budget_gate_records may be lowered but cannot exceed the core default"
        )
    if max_elapsed_minutes is not None and max_elapsed_minutes < 1:
        raise ValueError("max_elapsed_minutes must be positive")
    if max_input_tokens is not None and max_input_tokens < 1:
        raise ValueError("max_input_tokens must be positive")
    if max_provider_cost_usd is not None and (
        not math.isfinite(max_provider_cost_usd) or max_provider_cost_usd <= 0
    ):
        raise ValueError("max_provider_cost_usd must be positive")
    if provider_cost_unmetered and max_provider_cost_usd is not None:
        raise ValueError(
            "provider_cost_unmetered and max_provider_cost_usd are mutually exclusive"
        )
    if user_approved_budget and (
        max_elapsed_minutes is None
        or max_input_tokens is None
        or (not provider_cost_unmetered and max_provider_cost_usd is None)
    ):
        raise ValueError(
            "A user-approved bulk budget requires elapsed, input-token, and cost-or-unmetered limits"
        )
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

    fingerprint = stable_hash_sequence(asdict(segment) for segment in segments)
    if review_split is not None and not review_split["root_source_fingerprint"]:
        review_split = {**review_split, "root_source_fingerprint": fingerprint}
    task_max_records = (
        min(max_records, DEFAULT_REVIEW_INITIAL_RECORDS)
        if pass_kind == "review"
        else max_records
    )
    task_max_material_tokens = (
        min(max_material_tokens, DEFAULT_REVIEW_INITIAL_MATERIAL_TOKENS)
        if pass_kind == "review"
        else max_material_tokens
    )
    task_min_records = min(min_records, task_max_records)
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
            and manifest.get("task_payload_schema") == TASK_PAYLOAD_SCHEMA
        ):
            task_rows = manifest.get("tasks", [])
            expected_execution_contract = _execution_contract(
                len(task_rows),
                max_workers,
                len(segments),
                pass_kind=pass_kind,
                bulk_budget_gate_records=bulk_budget_gate_records,
                max_elapsed_minutes=max_elapsed_minutes,
                max_input_tokens=max_input_tokens,
                max_provider_cost_usd=max_provider_cost_usd,
                provider_cost_unmetered=provider_cost_unmetered,
                user_approved_budget=user_approved_budget,
                max_root_model_calls_per_wave=max_root_model_calls_per_wave,
                max_root_model_calls_per_turn=max_root_model_calls_per_turn,
                max_root_input_tokens_per_turn=max_root_input_tokens_per_turn,
                control_turn_budget_user_approved=control_turn_budget_user_approved,
            )
            expected_contract_fingerprint = stable_hash(
                {
                    "worker_contract": _worker_contract(pass_kind),
                    "detail_contract": _detail_contract(pass_kind),
                    "execution_contract": expected_execution_contract,
                }
            )
            valid = (
                bool(task_rows)
                and manifest.get("worker_contract") == _worker_contract(pass_kind)
                and manifest.get("detail_contract") == _detail_contract(pass_kind)
                and manifest.get("execution_contract") == expected_execution_contract
                and manifest.get("contract_fingerprint")
                == expected_contract_fingerprint
                and all(
                    item.get("dispatch_authorized")
                    == (
                        expected_execution_contract["bulk_budget_gate"]["active"]
                        is not True
                        or expected_execution_contract["bulk_budget_gate"]["user_approved"]
                        is True
                        or item["wave"]
                        <= expected_execution_contract["bulk_budget_gate"][
                            "pilot_waves_without_approved_budget"
                        ]
                    )
                    for item in task_rows
                )
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
                    "contract_fingerprint": expected_contract_fingerprint,
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

    output_dir.mkdir(parents=True, exist_ok=True)
    expected: set[str] = set()
    tasks: list[dict[str, Any]] = []
    batches: list[list[tuple[int, Segment, int]]] = []
    current: list[tuple[int, Segment, int]] = []
    current_tokens = 0
    for ordinal, segment in enumerate(segments):
        estimated_tokens = _estimated_material_tokens(segment)
        if current and (
            len(current) >= task_max_records
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
        bulk_budget_gate_records=bulk_budget_gate_records,
        max_elapsed_minutes=max_elapsed_minutes,
        max_input_tokens=max_input_tokens,
        max_provider_cost_usd=max_provider_cost_usd,
        provider_cost_unmetered=provider_cost_unmetered,
        user_approved_budget=user_approved_budget,
        max_root_model_calls_per_wave=max_root_model_calls_per_wave,
        max_root_model_calls_per_turn=max_root_model_calls_per_turn,
        max_root_input_tokens_per_turn=max_root_input_tokens_per_turn,
        control_turn_budget_user_approved=control_turn_budget_user_approved,
    )
    budget_gate = execution_contract["bulk_budget_gate"]
    for task in tasks:
        task["dispatch_authorized"] = (
            budget_gate["active"] is not True
            or budget_gate["user_approved"] is True
            or task["wave"] <= budget_gate["pilot_waves_without_approved_budget"]
        )
    worker_contract = _worker_contract(pass_kind)
    detail_contract = _detail_contract(pass_kind)
    contract_fingerprint = stable_hash(
        {
            "worker_contract": worker_contract,
            "detail_contract": detail_contract,
            "execution_contract": execution_contract,
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
        "worker_contract": worker_contract,
        "detail_contract": detail_contract,
        "execution_contract": execution_contract,
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
        "execution_contract": execution_contract,
        "manifest": str(manifest_path),
        "artifacts": [
            str(output_dir / item["file"])
            for item in tasks
            if item["dispatch_authorized"]
        ],
        "deferred_tasks": sum(not item["dispatch_authorized"] for item in tasks),
    }


def _validate_row(row: dict[str, Any], *, segment: Segment | None = None) -> dict[str, Any]:
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
            segment is None or segment.id != target_id or text != ""
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
    if row.get("issues") == ["worker_ended_without_result"]:
        return row
    if _review_failure_has_split_proof(
        row, max_depth=max_depth, max_calls=max_calls
    ):
        return row
    return {
        "target_id": row["target_id"],
        "status": "error",
        "text": "",
        "provider": "native-control",
        "issues": ["review_split_proof_missing"],
    }


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
            raise ValueError("Optional-slot source artifact is invalid")
        try:
            if Path(artifact).is_absolute():
                raise ValueError("Optional-slot source artifact must be relative")
            source = (work_dir / artifact).resolve()
            source.relative_to(work_dir.resolve())
            if not source.is_file():
                raise ValueError("Optional-slot source artifact must be a file")
            ordered = read_segments(source)
        except (OSError, UnicodeError, ValueError, TypeError, KeyError):
            raise ValueError("Optional-slot source artifact is invalid") from None
        if (
            len(ordered) != manifest.get("records")
            or stable_hash_sequence(asdict(segment) for segment in ordered)
            != manifest.get("source_fingerprint")
        ):
            raise ValueError("Optional-slot source no longer matches its task manifest")
        source_ids = {segment.id for segment in ordered}
        for segment in ordered:
            if segment.metadata.get("ui_empty_target") is not None:
                related = segment.metadata.get("ui_composed_with_ids")
                if not isinstance(related, list) or not all(
                    isinstance(target_id, str) and target_id in source_ids
                    for target_id in related
                ):
                    raise ValueError("Optional-slot composition references an unbound target")
        # Cache only a fully validated source; reuse once per wave/import scan.
        bound_segments.update((segment.id, segment) for segment in ordered)
    validated = [
        _validate_row(
            row,
            segment=bound_segments.get(row["target_id"])
            if isinstance(row.get("target_id"), str) else None,
        )
        for row in rows
    ]
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


def import_native_shard(work_dir: Path, source: Path, shard_name: str) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    safe_name = SAFE_NAME.sub("-", shard_name.casefold()).strip("-.")
    if not safe_name:
        raise ValueError("Native shard name is empty after normalization")
    rows = [_validate_row(row) for row in _read_jsonl(source)]
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
    """Validate and import one completed manifest wave without exposing result text."""

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
    if manifest.get("version", 0) >= LEGACY_WORKER_SPLIT_MANIFEST_VERSION:
        budget_gate = manifest["execution_contract"].get("bulk_budget_gate", {})
        pilot_waves = budget_gate.get("pilot_waves_without_approved_budget", 0)
        if (
            budget_gate.get("active") is True
            and budget_gate.get("user_approved") is not True
            and wave > pilot_waves
        ):
            raise ValueError(
                "E_BULK_EXECUTION_BUDGET_REQUIRED: checkpoint after the pilot wave and "
                "bind user-approved elapsed, input-token, and cost-or-unmetered limits"
            )
    tasks = [task for task in manifest["tasks"] if task["wave"] == wave]
    if not tasks:
        raise ValueError("Native task manifest does not contain the requested wave")

    planned: list[tuple[Path, list[dict[str, Any]]]] = []
    wave_ids: set[str] = set()
    bound_segments: dict[str, Segment] = {}
    for task in tasks:
        expected_ids = _validate_manifest_task_file(manifest_path, manifest, task)
        result_path = source_dir / task["file"]
        if not result_path.is_file():
            raise ValueError("Native wave result is incomplete")
        try:
            rows = _validate_manifest_rows(
                work_dir, manifest, _read_jsonl(result_path), bound_segments=bound_segments
            )
        except (ValueError, json.JSONDecodeError):
            raise ValueError("E_NATIVE_WAVE_RESULT_INVALID: Native wave result is invalid") from None
        result_ids = [row["target_id"] for row in rows]
        if result_ids != expected_ids:
            raise ValueError("Native wave result does not match its exclusive task binding")
        if wave_ids.intersection(result_ids):
            raise ValueError("Native wave results overlap")
        wave_ids.update(result_ids)
        planned.append((_manifest_shard_path(work_dir, manifest, task), rows))

    for destination, rows in planned:
        _atomic_jsonl(destination, rows)
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
        "pass_kind": manifest["pass_kind"],
        "manifest": manifest_path.resolve().relative_to(work_dir.resolve()).as_posix(),
        "manifest_contract_fingerprint": manifest["contract_fingerprint"],
        "plan_fingerprint": plan_fingerprint,
        "wave": wave,
        "task_bindings": bindings,
        "relay_contract": {
            "role": "isolated_wave_relay",
            "history_mode": "none",
            "authority": "dispatch_exact_bindings_only",
            "semantic_task_files_may_be_opened_by_relay": False,
            "semantic_worker_history_mode": "none",
            "semantic_worker_delegation_allowed": False,
            "planning_allowed": False,
            "retry_allowed": False,
            "route_change_allowed": False,
            "raw_child_response_forwarding_allowed": False,
            "child_leak_action": "quarantine_relay_and_return_categorical_receipt",
            "final_response": "workspace_relative_relay_receipt_path_only",
        },
    }
    payload = {**unsigned, "dispatch_fingerprint": stable_hash(unsigned)}
    path = _wave_relay_dispatch_path(work_dir, plan_fingerprint, wave)
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


def _read_structurally_bound_result(
    task: Path,
    result: Path,
) -> tuple[list[str], list[dict[str, Any]]] | None:
    """Return a complete ordered result without exposing semantic fields."""

    if not result.is_file():
        return None
    try:
        expected_ids = _task_target_ids(task, require_payload=True)
        rows = [_validate_row(row) for row in _read_jsonl(result)]
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None
    if [row["target_id"] for row in rows] != expected_ids:
        return None
    return expected_ids, rows


def _complete_ended_task_result(
    task: Path,
    result: Path,
    *,
    expected_records: int,
) -> bool:
    """Preserve an ordered partial result and fill only its missing targets."""

    if _read_structurally_bound_result(task, result) is not None:
        return False
    if result.exists() and not result.is_file():
        raise ValueError("E_NATIVE_RELAY_INVALID: result path is not a file")

    expected_ids = _task_target_ids(task, require_payload=True)
    if len(expected_ids) != expected_records:
        raise ValueError("E_NATIVE_RELAY_INVALID: ended task record count changed")

    preserved: dict[str, dict[str, Any]] = {}
    if result.is_file():
        original = result.read_bytes()
        partial_rows: list[dict[str, Any]] = []
        malformed = False
        for line in original.splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line.decode("utf-8-sig"))
                if not isinstance(raw, dict):
                    raise ValueError("Result row must be an object")
                partial_rows.append(_validate_row(raw))
            except (UnicodeError, ValueError, TypeError):
                malformed = True
        expected_positions = {target_id: index for index, target_id in enumerate(expected_ids)}
        partial_ids = [row["target_id"] for row in partial_rows]
        if (
            len(partial_ids) != len(set(partial_ids))
            or any(target_id not in expected_positions for target_id in partial_ids)
            or [expected_positions[target_id] for target_id in partial_ids]
            != sorted(expected_positions[target_id] for target_id in partial_ids)
        ):
            raise ValueError(
                "E_NATIVE_RELAY_INVALID: ended task result is not an ordered subset"
            )
        if malformed:
            digest = hashlib.sha256(original).hexdigest()
            archive = result.with_name(f"{result.name}.ended-original-{digest}")
            if archive.exists():
                if archive.read_bytes() != original:
                    raise ValueError("E_NATIVE_RELAY_INVALID: original archive changed")
            else:
                archive.write_bytes(original)
        preserved = {row["target_id"]: row for row in partial_rows}

    completed_rows = [
        preserved.get(
            target_id,
            {
                "target_id": target_id,
                "status": "error",
                "text": "",
                "provider": "native-worker",
                "issues": ["worker_ended_without_result"],
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
    fields = {
        "schema",
        "pass_kind",
        "manifest",
        "manifest_contract_fingerprint",
        "plan_fingerprint",
        "wave",
        "task_bindings",
        "relay_contract",
        "dispatch_fingerprint",
    }
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("E_NATIVE_RELAY_INVALID: dispatch shape is invalid")
    unsigned = {key: payload[key] for key in fields if key != "dispatch_fingerprint"}
    if (
        payload.get("schema") != NATIVE_WAVE_RELAY_SCHEMA
        or payload.get("pass_kind") not in PASS_KINDS
        or not isinstance(payload.get("wave"), int)
        or isinstance(payload.get("wave"), bool)
        or payload["wave"] < 1
        or payload.get("dispatch_fingerprint") != stable_hash(unsigned)
        or not isinstance(payload.get("task_bindings"), list)
        or not payload["task_bindings"]
        or len(payload["task_bindings"]) > DEFAULT_MAX_WORKERS
        or not isinstance(payload.get("relay_contract"), dict)
        or payload["relay_contract"].get("role") != "isolated_wave_relay"
        or payload["relay_contract"].get("authority")
        != "dispatch_exact_bindings_only"
        or payload["relay_contract"].get("raw_child_response_forwarding_allowed")
        is not False
    ):
        raise ValueError("E_NATIVE_RELAY_INVALID: dispatch binding is invalid")

    if len(set(ended_tasks)) != len(ended_tasks):
        raise ValueError("E_NATIVE_RELAY_INVALID: ended tasks must be unique")

    bindings_by_name: dict[str, tuple[Path, Path, int]] = {}
    for binding in payload["task_bindings"]:
        if not isinstance(binding, dict) or set(binding) != {
            "task",
            "result",
            "records",
            "estimated_material_tokens",
        }:
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

    unknown_ended = [name for name in ended_tasks if name not in bindings_by_name]
    if unknown_ended:
        raise ValueError("E_NATIVE_RELAY_INVALID: ended task is not bound to this wave")

    ended_tasks_recorded = 0
    for name in ended_tasks:
        task, result, expected_records = bindings_by_name[name]
        ended_tasks_recorded += int(
            _complete_ended_task_result(
                task,
                result,
                expected_records=expected_records,
            )
        )

    ready = sum(
        _read_structurally_bound_result(task, result) is not None
        for task, result, _ in bindings_by_name.values()
    )

    total = len(payload["task_bindings"])
    issue_codes = [] if ready == total else ["E_NATIVE_WAVE_INCOMPLETE"]
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
        "ended_tasks_recorded": ended_tasks_recorded,
    }


def _validated_native_wave_relay_receipt(
    work_dir: Path,
    plan_fingerprint: str,
    wave: int,
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
    if not isinstance(receipt, dict) or set(receipt) != receipt_fields:
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
    unsigned = {
        key: receipt[key] for key in receipt_fields if key != "receipt_fingerprint"
    }
    dispatch_relative = dispatch_path.relative_to(work_dir.resolve()).as_posix()
    bindings = dispatch.get("task_bindings") if isinstance(dispatch, dict) else None
    if not isinstance(bindings, list):
        raise ValueError("E_NATIVE_RELAY_RECEIPT_INVALID")
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
        ready += int(_read_structurally_bound_result(task, result) is not None)
    total = len(bindings)
    expected_counts = {
        "expected_tasks": total,
        "ready_tasks": ready,
        "pending_tasks": total - ready,
    }
    expected_issues = [] if ready == total else ["E_NATIVE_WAVE_INCOMPLETE"]
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
    return {
        "schema": NATIVE_CONTROL_STATE_SCHEMA,
        "source_fingerprint": manifest["source_fingerprint"],
        "plan_fingerprint": plan_fingerprint,
        "contract_fingerprint": manifest["contract_fingerprint"],
        "created_at_epoch": time.time(),
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
    required = legacy_required | {
        "checkpoint_initialized",
        "imported_wave_count",
        "imported_tasks",
    }
    if (
        isinstance(state, dict)
        and state.get("schema") == LEGACY_NATIVE_CONTROL_STATE_SCHEMA
        and set(state) == legacy_required
    ):
        state["schema"] = NATIVE_CONTROL_STATE_SCHEMA
        state["checkpoint_initialized"] = False
        state["imported_wave_count"] = 0
        state["imported_tasks"] = 0
    if not isinstance(state, dict) or set(state) != required:
        raise ValueError("E_NATIVE_CONTROL_STATE_INVALID: state shape is invalid")
    if (
        state.get("schema") != NATIVE_CONTROL_STATE_SCHEMA
        or state.get("source_fingerprint") != manifest["source_fingerprint"]
        or state.get("plan_fingerprint") != plan_fingerprint
        or not isinstance(state.get("created_at_epoch"), (int, float))
        or isinstance(state.get("created_at_epoch"), bool)
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
    # Preserve pilot usage and imported checkpoints, but start a new decision generation.
    if state["contract_fingerprint"] != manifest["contract_fingerprint"]:
        state["active_wave"] = 0
        state["control_steps_in_wave"] = 0
        state["last_progress_marker"] = ""
        state["unchanged_observations"] = 0
        state["last_action"] = ""
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
        if any(present) and not all(present):
            raise ValueError(
                "E_NATIVE_CONTROL_STATE_INVALID: a wave is only partially imported"
            )
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
    if (
        gate.get("active") is True
        and gate.get("user_approved") is not True
        and next_wave > gate.get("pilot_waves_without_approved_budget", 0)
    ):
        return "E_BULK_EXECUTION_BUDGET_REQUIRED"
    if gate.get("user_approved") is not True:
        return ""
    max_elapsed = gate.get("max_elapsed_minutes")
    elapsed_minutes = (time.time() - float(state["created_at_epoch"])) / 60.0
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
        "input_tokens",
        "provider_cost_usd",
        "usage_event_ids",
    ):
        child_state[key] = parent_state[key]
    _atomic_json(child_path, child_state)


def _prepare_review_split_child(
    work_dir: Path,
    current_manifest_path: Path,
    current_manifest: dict[str, Any],
    failed: list[Segment],
    root_plan_fingerprint: str,
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
    if (
        len(failed) <= 1
        or parent_task_records <= 1
        or split["depth"] >= DEFAULT_REVIEW_SPLIT_MAX_DEPTH
        or split["semantic_passes"] >= DEFAULT_REVIEW_SPLIT_MAX_CALLS
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
    child_max_material = max(256, parent_task_material // 2)
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
        bulk_budget_gate_records=int(gate["activation_records"]),
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
        _review_split={
            "depth": next_depth,
            "semantic_passes": split["semantic_passes"] + 1,
            "parent_manifest": current_manifest_path.resolve()
            .relative_to(work_dir.resolve())
            .as_posix(),
            "root_source_fingerprint": split["root_source_fingerprint"],
        },
    )
    child_manifest_path = child_tasks / "manifest.json"
    child_manifest = _load_task_manifest(work_dir, child_manifest_path)
    _inherit_native_control_usage(work_dir, current_manifest, child_manifest)
    child_results.mkdir(parents=True, exist_ok=True)
    return child_manifest_path, child_results


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
) -> dict[str, Any]:
    """Advance one manifest pass without asking a model to schedule or recover it."""

    require_control_plane_ready(work_dir)
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
        relay_receipt_fingerprint = (
            _file_sha256(relay_receipt_path)
            if relay_receipt_path.is_file()
            else "missing"
        )
        progress_marker = (
            f"{imported_tasks}:{next_wave}:{ready_results}:"
            f"{result_fingerprint}:{relay_receipt_fingerprint}:"
            f"validation-{RESULT_VALIDATION_REVISION}"
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
        max_unchanged = int(
            manifest["execution_contract"].get(
                "max_unchanged_progress_observations", 1
            )
        )
        relay_issue = ""
        if (
            require_relay_receipt
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
                    work_dir, plan_fingerprint, next_wave
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
                    relay_issue = "E_NATIVE_WAVE_INCOMPLETE"
        budget_issue = _native_budget_issue(
            manifest["execution_contract"], state, next_wave
        )
        if relay_issue:
            action = "checkpoint_and_stop"
            issue_codes = [relay_issue]
        elif budget_issue == "E_BULK_EXECUTION_BUDGET_REQUIRED":
            action = "checkpoint_and_stop"
            issue_codes = [budget_issue]
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
        elif state["unchanged_observations"] >= max_unchanged:
            action = "checkpoint_and_stop"
            issue_codes = ["E_NATIVE_NO_MEASURABLE_PROGRESS"]
        elif ready_results == len(tasks):
            try:
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
                        task["dispatch_authorized"] for task in next_tasks
                    ):
                        action = "checkpoint_and_stop"
                        issue_codes = ["E_NATIVE_DISPATCH_NOT_AUTHORIZED"]
                    else:
                        action = "dispatch_isolated_wave_relay"
                        issue_codes = []
        elif ready_results:
            action = "wait_for_active_wave"
            issue_codes = ["E_NATIVE_WAVE_INCOMPLETE"]
        elif not all(task["dispatch_authorized"] for task in tasks):
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

    state["last_action"] = action
    _atomic_json(state_path, state)
    selected_tasks = (
        wave_tasks[next_wave]
        if action == "dispatch_isolated_wave_relay" and next_wave is not None
        else []
    )
    for task in selected_tasks:
        _validate_manifest_task_file(manifest_path, manifest, task)
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
        if selected_tasks and next_wave is not None
        else ""
    )
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
        "relay": (
            {
                "dispatch": relay_dispatch,
                "role": "isolated_wave_relay",
                "history_mode": "none",
                "tasks": len(selected_tasks),
                "records": sum(task["records"] for task in selected_tasks),
                "final_response": "workspace_relative_relay_receipt_path_only",
            }
            if relay_dispatch
            else None
        ),
        "usage": {
            "events": len(state["usage_event_ids"]),
            "input_tokens": state["input_tokens"],
            "provider_cost_usd": state["provider_cost_usd"],
            "elapsed_minutes": round(
                max(0.0, (time.time() - float(state["created_at_epoch"])) / 60.0),
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
) -> dict[str, Any]:
    """Advance a pass and let the core isolate only failed review rows."""

    root_manifest = _load_task_manifest(work_dir, manifest_path)
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
        )

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
    step = _native_control_step_single(
        work_dir,
        current_manifest_path,
        current_results_dir,
        usage_event_id=usage_event_id,
        input_tokens=input_tokens,
        provider_cost_usd=provider_cost_usd,
        require_relay_receipt=require_relay_receipt,
    )
    split = current_manifest["review_split"]
    split_summary = {
        "depth": split["depth"],
        "semantic_passes": split["semantic_passes"],
        "failed_records": None,
        "chain_complete": chain["complete"],
    }
    if step["action"] != "aggregate_completed_pass":
        _atomic_json(chain_path, chain)
        return {**step, "review_split": split_summary}

    failed = _failed_review_segments(
        work_dir, current_manifest_path, current_manifest
    )
    split_summary["failed_records"] = len(failed)
    child = _prepare_review_split_child(
        work_dir,
        current_manifest_path,
        current_manifest,
        failed,
        root_plan_fingerprint,
    )
    if child is None:
        chain["complete"] = True
        split_summary["chain_complete"] = True
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
        work_dir, child_manifest_path, child_results_dir
    )
    child_split = _load_task_manifest(work_dir, child_manifest_path)["review_split"]
    return {
        **child_step,
        "review_split": {
            "depth": child_split["depth"],
            "semantic_passes": child_split["semantic_passes"],
            "failed_records": len(failed),
            "chain_complete": False,
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
