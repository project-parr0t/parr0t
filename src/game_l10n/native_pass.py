from __future__ import annotations

import json
import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .control_plane import CONTROL_ENVELOPE_SCHEMA, require_control_plane_ready
from .models import Segment
from .store import stable_hash


ALLOWED_STATUSES = {"success", "refused", "error", "invalid"}
SAFE_NAME = re.compile(r"[^a-z0-9._-]+")
TASK_MANIFEST_VERSION = 4
DEFAULT_MAX_RECORDS = 64
DEFAULT_MIN_RECORDS = 8
DEFAULT_MAX_MATERIAL_TOKENS = 24_000
DEFAULT_MAX_WORKERS = 8


def _execution_contract(task_count: int, max_workers: int) -> dict[str, Any]:
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
        raise ValueError(f"Native task manifest must be version {TASK_MANIFEST_VERSION}")
    tasks = payload.get("tasks")
    if payload.get("version") != TASK_MANIFEST_VERSION or not isinstance(tasks, list) or not tasks:
        raise ValueError(f"Native task manifest must be version {TASK_MANIFEST_VERSION}")
    if not isinstance(payload.get("source_fingerprint"), str) or not payload["source_fingerprint"]:
        raise ValueError("Native task manifest source fingerprint is missing")
    seen_files: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("Native task manifest contains an invalid task")
        name = task.get("file")
        wave = task.get("wave")
        records = task.get("records")
        digest = task.get("sha256")
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
        ):
            raise ValueError("Native task manifest contains an invalid task binding")
        task_path = manifest_path.parent / name
        if not task_path.is_file() or _file_sha256(task_path) != digest:
            raise ValueError("Native task manifest references a missing or changed task")
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


def _task_target_ids(task_path: Path) -> list[str]:
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
        ):
            task_rows = manifest.get("tasks", [])
            expected_execution_contract = _execution_contract(len(task_rows), max_workers)
            valid = (
                bool(task_rows)
                and manifest.get("execution_contract") == expected_execution_contract
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
                    "execution_contract": execution_contract,
                    "manifest": str(manifest_path),
                    "artifacts": [str(output_dir / item["file"]) for item in task_rows],
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
            {"target_id": segment.id, "ordinal": ordinal}
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
    execution_contract = _execution_contract(len(tasks), max_workers)
    manifest = {
        "version": TASK_MANIFEST_VERSION,
        "source_artifact": source_artifact,
        "source_fingerprint": fingerprint,
        "max_records": max_records,
        "min_records": min_records,
        "max_material_tokens": max_material_tokens,
        "max_workers": max_workers,
        "records": len(segments),
        "worker_contract": {
            "history_mode": "none",
            "fresh_worker_per_task": True,
            "max_followup_tasks": 0,
            "max_agent_depth": 1,
            "delegation_allowed": False,
            "model_controller_role_allowed": False,
            "max_model_calls_per_task": 16,
            "max_parent_progress_messages": 0,
            "task_message_contains_game_text": False,
            "parent_result": "metadata_only",
            "control_envelope_schema": CONTROL_ENVELOPE_SCHEMA,
            "worker_final_response": "workspace_relative_envelope_path_only",
            "free_form_parent_response_allowed": False,
        },
        "execution_contract": execution_contract,
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
        "execution_contract": execution_contract,
        "manifest": str(manifest_path),
        "artifacts": [str(output_dir / item["file"]) for item in tasks],
    }


def _validate_row(row: dict[str, Any]) -> dict[str, Any]:
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
    tasks = [task for task in manifest["tasks"] if task["wave"] == wave]
    if not tasks:
        raise ValueError("Native task manifest does not contain the requested wave")

    planned: list[tuple[Path, list[dict[str, Any]]]] = []
    wave_ids: set[str] = set()
    for task in tasks:
        expected_ids = _task_target_ids(manifest_path.parent / task["file"])
        if len(expected_ids) != task["records"]:
            raise ValueError("Native task record count does not match its manifest binding")
        result_path = source_dir / task["file"]
        if not result_path.is_file():
            raise ValueError("Native wave result is incomplete")
        try:
            rows = [_validate_row(row) for row in _read_jsonl(result_path)]
        except (ValueError, json.JSONDecodeError):
            raise ValueError("Native wave result is invalid") from None
        result_ids = [row["target_id"] for row in rows]
        if result_ids != expected_ids:
            raise ValueError("Native wave result does not match its exclusive task binding")
        if wave_ids.intersection(result_ids):
            raise ValueError("Native wave results overlap")
        wave_ids.update(result_ids)
        planned.append((_manifest_shard_path(work_dir, manifest, task), rows))

    for destination, rows in planned:
        _atomic_jsonl(destination, rows)

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
                _task_target_ids(manifest_path.parent / task["file"]),
            )
            for task in manifest["tasks"]
        ]
    else:
        shard_bindings = [(path, None) for path in sorted(shard_root.glob("*.jsonl"))]
        expected_shards = len(shard_bindings)

    imported_shards = 0
    for shard, expected_ids in shard_bindings:
        if not shard.is_file():
            continue
        if expected_ids is None:
            rows = [_validate_row(raw) for raw in _read_jsonl(shard)]
        else:
            try:
                rows = [_validate_row(raw) for raw in _read_jsonl(shard)]
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
