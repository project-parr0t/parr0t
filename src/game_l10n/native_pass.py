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
from .store import stable_hash
from .validators import validate_integrity


ALLOWED_STATUSES = {"success", "refused", "error", "invalid"}
SAFE_NAME = re.compile(r"[^a-z0-9._-]+")
TASK_MANIFEST_VERSION = 6
SUPPORTED_TASK_MANIFEST_VERSIONS = {4, 5, TASK_MANIFEST_VERSION}
TASK_PAYLOAD_SCHEMA = "parr0t.native-task-payload.v1"
DEFAULT_MAX_RECORDS = 64
DEFAULT_MIN_RECORDS = 8
DEFAULT_MAX_MATERIAL_TOKENS = 24_000
DEFAULT_MAX_WORKERS = 8
DEFAULT_BULK_BUDGET_GATE_RECORDS = 4_096
NATIVE_CONTROL_STATE_SCHEMA = "parr0t.native-control-state.v1"
NATIVE_CONTROL_STEP_SCHEMA = "parr0t.native-control-step.v1"
NATIVE_WAVE_RECEIPT_SCHEMA = "parr0t.native-wave-receipt.v1"
USAGE_EVENT = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")


def _worker_contract() -> dict[str, Any]:
    return {
        "history_mode": "none",
        "fresh_worker_per_task": True,
        "max_followup_tasks": 0,
        "max_agent_depth": 1,
        "delegation_allowed": False,
        "model_controller_role_allowed": False,
        "max_model_calls_per_task": 16,
        "max_semantic_model_calls_per_task": 1,
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
    }


def _detail_contract() -> dict[str, Any]:
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
        "one_semantic_batch_per_task": True,
        "per_record_semantic_call_loop_allowed": False,
        "shared_context_loads_per_task": 1,
        "task_input": "bound_segment_payload_only",
        "full_source_artifact_access": "validation_only",
        "result_cardinality": "one_status_per_bound_target",
        "partial_failure_isolation": "per_target_status_in_same_batched_result",
        "unchanged_success_reprocessing_allowed": False,
        "budget_exhaustion_action": "checkpoint_and_return_control",
    }


def _execution_contract(
    task_count: int,
    max_workers: int,
    record_count: int,
    *,
    bulk_budget_gate_records: int,
    max_elapsed_minutes: int | None,
    max_input_tokens: int | None,
    max_provider_cost_usd: float | None,
    provider_cost_unmetered: bool,
    user_approved_budget: bool,
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
    return {
        "strategy": "direct_single_task" if task_count == 1 else "bounded_slot_queue",
        "orchestration_owner": "root_control_plane",
        "model_controller_allowed": False,
        "max_agent_depth": 1,
        "worker_delegation_allowed": False,
        "max_parallel_workers": max_workers,
        "max_tasks_per_wave": max_workers,
        "estimated_waves": (task_count + max_workers - 1) // max_workers,
        "max_root_model_calls_per_wave": 12,
        "max_root_model_calls_per_turn": 64,
        "max_root_input_tokens_per_turn": 5_000_000,
        "max_root_progress_messages_per_wave": 1,
        "max_worker_model_calls_per_task": 16,
        "budget_exhaustion_action": "checkpoint_and_return_control",
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
    if version == TASK_MANIFEST_VERSION:
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
        if worker_contract != _worker_contract() or detail_contract != _detail_contract():
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
                version == TASK_MANIFEST_VERSION
                and dispatch_authorized
                != (
                    budget_gate.get("active") is not True
                    or budget_gate.get("user_approved") is True
                    or wave <= pilot_waves
                )
            )
        ):
            raise ValueError("Native task manifest contains an invalid task binding")
        task_path = manifest_path.parent / name
        if not task_path.is_file() or _file_sha256(task_path) != digest:
            raise ValueError("Native task manifest references a missing or changed task")
        target_ids = _task_target_ids(
            task_path,
            require_payload=version == TASK_MANIFEST_VERSION,
        )
        if len(target_ids) != records:
            raise ValueError("Native task record count does not match its manifest binding")
        seen_files.add(name)
    return payload


def _manifest_shard_path(
    work_dir: Path, manifest: dict[str, Any], task: dict[str, Any]
) -> Path:
    binding = stable_hash(
        {
            "source_fingerprint": manifest["source_fingerprint"],
            "task_file": task["file"],
        }
    )[:16]
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


def prepare_native_tasks(
    work_dir: Path,
    source_path: Path,
    segments: list[Segment],
    output_dir: Path,
    *,
    max_records: int = DEFAULT_MAX_RECORDS,
    min_records: int | None = None,
    max_material_tokens: int = DEFAULT_MAX_MATERIAL_TOKENS,
    max_workers: int = DEFAULT_MAX_WORKERS,
    bulk_budget_gate_records: int = DEFAULT_BULK_BUDGET_GATE_RECORDS,
    max_elapsed_minutes: int | None = None,
    max_input_tokens: int | None = None,
    max_provider_cost_usd: float | None = None,
    provider_cost_unmetered: bool = False,
    user_approved_budget: bool = False,
) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    if not 1 <= max_records <= 2000:
        raise ValueError("max_records must be between 1 and 2000")
    if min_records is None:
        min_records = min(DEFAULT_MIN_RECORDS, max_records)
    if not 1 <= min_records <= max_records:
        raise ValueError("min_records must be between 1 and max_records")
    if not 256 <= max_material_tokens <= 1_000_000:
        raise ValueError("max_material_tokens must be between 256 and 1000000")
    if not 1 <= max_workers <= 64:
        raise ValueError("max_workers must be between 1 and 64")
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
    if not segments:
        raise ValueError("Ordered source contains no segments")
    ids = [segment.id for segment in segments]
    if len(ids) != len(set(ids)):
        raise ValueError("Ordered source contains duplicate target IDs")
    try:
        source_artifact = source_path.resolve().relative_to(work_dir.resolve()).as_posix()
        output_dir.resolve().relative_to(work_dir.resolve())
    except ValueError as exc:
        raise ValueError("Native task source and output must stay inside the private workspace") from exc

    fingerprint = stable_hash([asdict(segment) for segment in segments])
    manifest_path = output_dir / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("version") == TASK_MANIFEST_VERSION
            and manifest.get("source_fingerprint") == fingerprint
            and manifest.get("max_records") == max_records
            and manifest.get("min_records") == min_records
            and manifest.get("max_material_tokens") == max_material_tokens
            and manifest.get("max_workers") == max_workers
            and manifest.get("task_payload_schema") == TASK_PAYLOAD_SCHEMA
        ):
            task_rows = manifest.get("tasks", [])
            expected_execution_contract = _execution_contract(
                len(task_rows),
                max_workers,
                len(segments),
                bulk_budget_gate_records=bulk_budget_gate_records,
                max_elapsed_minutes=max_elapsed_minutes,
                max_input_tokens=max_input_tokens,
                max_provider_cost_usd=max_provider_cost_usd,
                provider_cost_unmetered=provider_cost_unmetered,
                user_approved_budget=user_approved_budget,
            )
            expected_contract_fingerprint = stable_hash(
                {
                    "worker_contract": _worker_contract(),
                    "detail_contract": _detail_contract(),
                    "execution_contract": expected_execution_contract,
                }
            )
            valid = (
                bool(task_rows)
                and manifest.get("worker_contract") == _worker_contract()
                and manifest.get("detail_contract") == _detail_contract()
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
                    and _file_sha256(output_dir / item["file"]) == item["sha256"]
                    for item in task_rows
                )
            )
            if valid:
                execution_contract = expected_execution_contract
                return {
                    "cached": True,
                    "records": len(segments),
                    "tasks": len(task_rows),
                    "max_records": max_records,
                    "min_records": min_records,
                    "max_material_tokens": max_material_tokens,
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
            len(current) >= max_records
            or current_tokens + estimated_tokens > max_material_tokens
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
        min_records=min_records,
        max_records=max_records,
        max_material_tokens=max_material_tokens,
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
                    item[2] > max_material_tokens for item in batch
                ),
                "sha256": _file_sha256(path),
            }
        )
    for stale in output_dir.glob("task-*.jsonl"):
        if stale.name not in expected:
            stale.unlink()
    execution_contract = _execution_contract(
        len(tasks),
        max_workers,
        len(segments),
        bulk_budget_gate_records=bulk_budget_gate_records,
        max_elapsed_minutes=max_elapsed_minutes,
        max_input_tokens=max_input_tokens,
        max_provider_cost_usd=max_provider_cost_usd,
        provider_cost_unmetered=provider_cost_unmetered,
        user_approved_budget=user_approved_budget,
    )
    budget_gate = execution_contract["bulk_budget_gate"]
    for task in tasks:
        task["dispatch_authorized"] = (
            budget_gate["active"] is not True
            or budget_gate["user_approved"] is True
            or task["wave"] <= budget_gate["pilot_waves_without_approved_budget"]
        )
    worker_contract = _worker_contract()
    detail_contract = _detail_contract()
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
        "source_artifact": source_artifact,
        "source_fingerprint": fingerprint,
        "max_records": max_records,
        "min_records": min_records,
        "max_material_tokens": max_material_tokens,
        "max_workers": max_workers,
        "records": len(segments),
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
        "max_records": max_records,
        "min_records": min_records,
        "max_material_tokens": max_material_tokens,
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
    return {
        "target_id": target_id,
        "status": status,
        "text": text if status == "success" else "",
        "provider": str(row.get("provider", "native-worker")),
        "issues": issues,
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
            or stable_hash([asdict(segment) for segment in ordered])
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
    return [
        _validate_row(
            row,
            segment=bound_segments.get(row["target_id"])
            if isinstance(row.get("target_id"), str) else None,
        )
        for row in rows
    ]


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
    if manifest.get("version") == TASK_MANIFEST_VERSION:
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
        expected_ids = _task_target_ids(
            manifest_path.parent / task["file"],
            require_payload=manifest.get("version") == TASK_MANIFEST_VERSION,
        )
        if len(expected_ids) != task["records"]:
            raise ValueError("Native task record count does not match its manifest binding")
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
    return stable_hash(
        [
            {
                "file": task["file"],
                "wave": task["wave"],
                "slot": task["slot"],
                "records": task["records"],
                "sha256": task["sha256"],
            }
            for task in manifest["tasks"]
        ]
    )


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
    required = {
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
    gap_seen = False
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
            if gap_seen:
                raise ValueError(
                    "E_NATIVE_CONTROL_STATE_INVALID: imported waves are not contiguous"
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
                require_payload=manifest.get("version") == TASK_MANIFEST_VERSION,
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
            if gap_seen:
                raise ValueError(
                    "E_NATIVE_CONTROL_STATE_INVALID: imported waves are not contiguous"
                )
            _write_wave_receipt(work_dir, manifest, tasks, wave)
            imported_waves.append(wave)
            imported_tasks += len(tasks)
        else:
            gap_seen = True
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


def native_control_step(
    work_dir: Path,
    manifest_path: Path,
    results_dir: Path,
    *,
    usage_event_id: str | None = None,
    input_tokens: int = 0,
    provider_cost_usd: float = 0.0,
) -> dict[str, Any]:
    """Advance one manifest pass without asking a model to schedule or recover it."""

    require_control_plane_ready(work_dir)
    try:
        results_dir.resolve().relative_to(work_dir.resolve())
    except ValueError as exc:
        raise ValueError("Native result directory must stay inside the private workspace") from exc
    results_dir.mkdir(parents=True, exist_ok=True)
    manifest = _load_task_manifest(work_dir, manifest_path)
    if manifest.get("version") != TASK_MANIFEST_VERSION:
        raise ValueError(
            "E_NATIVE_CONTROL_MANIFEST_UPGRADE_REQUIRED: deterministic stepping requires v6"
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

    imported_waves, imported_tasks = _imported_native_waves(
        work_dir, manifest_path, manifest
    )
    waves = sorted({task["wave"] for task in manifest["tasks"]})
    import_summary: dict[str, Any] | None = None
    next_wave = next((wave for wave in waves if wave not in imported_waves), None)

    if next_wave is not None:
        tasks = [task for task in manifest["tasks"] if task["wave"] == next_wave]
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
        progress_marker = (
            f"{imported_tasks}:{next_wave}:{ready_results}:{result_fingerprint}"
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

        max_root_calls = int(
            manifest["execution_contract"].get("max_root_model_calls_per_wave", 12)
        )
        max_unchanged = int(
            manifest["execution_contract"].get(
                "max_unchanged_progress_observations", 1
            )
        )
        budget_issue = _native_budget_issue(
            manifest["execution_contract"], state, next_wave
        )
        if budget_issue == "E_BULK_EXECUTION_BUDGET_REQUIRED":
            action = "checkpoint_and_stop"
            issue_codes = [budget_issue]
        elif budget_issue and ready_results != len(tasks):
            action = "checkpoint_and_stop"
            issue_codes = [budget_issue]
        elif state["control_steps_in_wave"] > max_root_calls:
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
                imported_waves, imported_tasks = _imported_native_waves(
                    work_dir, manifest_path, manifest
                )
                next_wave = next(
                    (wave for wave in waves if wave not in imported_waves), None
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
                    next_tasks = [
                        task
                        for task in manifest["tasks"]
                        if task["wave"] == next_wave
                    ]
                    if budget_issue:
                        action = "checkpoint_and_stop"
                        issue_codes = [budget_issue]
                    elif not all(
                        task["dispatch_authorized"] for task in next_tasks
                    ):
                        action = "checkpoint_and_stop"
                        issue_codes = ["E_NATIVE_DISPATCH_NOT_AUTHORIZED"]
                    else:
                        action = "dispatch_authorized_wave"
                        issue_codes = []
        elif ready_results:
            action = "wait_for_active_wave"
            issue_codes = ["E_NATIVE_WAVE_INCOMPLETE"]
        elif not all(task["dispatch_authorized"] for task in tasks):
            action = "checkpoint_and_stop"
            issue_codes = ["E_NATIVE_DISPATCH_NOT_AUTHORIZED"]
        else:
            action = "dispatch_authorized_wave"
            issue_codes = []
    else:
        action = "aggregate_completed_pass"
        issue_codes = []
        state["active_wave"] = 0
        state["control_steps_in_wave"] = 0

    state["last_action"] = action
    _atomic_json(state_path, state)
    selected_tasks = (
        [task for task in manifest["tasks"] if task["wave"] == next_wave]
        if action == "dispatch_authorized_wave" and next_wave is not None
        else []
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
        "tasks": [
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
            for task in selected_tasks
        ],
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
    shard_bindings: list[tuple[Path, list[str] | None]] = []
    if manifest_path is not None:
        manifest = _load_task_manifest(work_dir, manifest_path)
        fingerprint = stable_hash([asdict(segment) for segment in segments])
        if (
            manifest.get("source_fingerprint") != fingerprint
            or manifest.get("records") != len(segments)
        ):
            raise ValueError("Native task manifest targets a different ordered source")
        expected_shards = len(manifest["tasks"])
        shard_bindings = [
            (
                _manifest_shard_path(work_dir, manifest, task),
                _task_target_ids(
                    manifest_path.parent / task["file"],
                    require_payload=manifest.get("version") == TASK_MANIFEST_VERSION,
                ),
            )
            for task in manifest["tasks"]
        ]
    else:
        shard_bindings = [(path, None) for path in sorted(shard_root.glob("*.jsonl"))]
        expected_shards = len(shard_bindings)

    imported_shards = 0
    bound_segments: dict[str, Segment] = {}
    for shard, expected_ids in shard_bindings:
        if not shard.is_file():
            continue
        if expected_ids is None:
            rows = [_validate_row(raw) for raw in _read_jsonl(shard)]
        else:
            try:
                rows = _validate_manifest_rows(
                    work_dir, manifest, _read_jsonl(shard), bound_segments=bound_segments
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
            if target_id in collected:
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
        "complete": missing == 0,
        "manifest_bound": manifest_bound,
        "expected_shards": expected_shards,
        "imported_shards": imported_shards,
        "artifact": str(destination),
    }
