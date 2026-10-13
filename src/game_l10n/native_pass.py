from __future__ import annotations

import json
import hashlib
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .models import Segment
from .store import stable_hash


ALLOWED_STATUSES = {"success", "refused", "error", "invalid"}
SAFE_NAME = re.compile(r"[^a-z0-9._-]+")


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


def prepare_native_tasks(
    work_dir: Path,
    source_path: Path,
    segments: list[Segment],
    output_dir: Path,
    *,
    max_records: int = 256,
) -> dict[str, Any]:
    if not 1 <= max_records <= 2000:
        raise ValueError("max_records must be between 1 and 2000")
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
        if manifest.get("source_fingerprint") == fingerprint and manifest.get("max_records") == max_records:
            task_rows = manifest.get("tasks", [])
            valid = bool(task_rows) and all(
                (output_dir / item["file"]).is_file()
                and _file_sha256(output_dir / item["file"]) == item["sha256"]
                for item in task_rows
            )
            if valid:
                return {
                    "cached": True,
                    "records": len(segments),
                    "tasks": len(task_rows),
                    "max_records": max_records,
                    "manifest": str(manifest_path),
                    "artifacts": [str(output_dir / item["file"]) for item in task_rows],
                }

    output_dir.mkdir(parents=True, exist_ok=True)
    expected: set[str] = set()
    tasks: list[dict[str, Any]] = []
    for offset in range(0, len(segments), max_records):
        number = len(tasks) + 1
        name = f"task-{number:04d}.jsonl"
        path = output_dir / name
        rows = [
            {"target_id": segment.id, "ordinal": ordinal}
            for ordinal, segment in enumerate(segments[offset : offset + max_records], start=offset)
        ]
        _atomic_jsonl(path, rows)
        expected.add(name)
        tasks.append(
            {
                "file": name,
                "records": len(rows),
                "sha256": _file_sha256(path),
            }
        )
    for stale in output_dir.glob("task-*.jsonl"):
        if stale.name not in expected:
            stale.unlink()
    manifest = {
        "version": 1,
        "source_artifact": source_artifact,
        "source_fingerprint": fingerprint,
        "max_records": max_records,
        "records": len(segments),
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


def merge_native_pass(
    work_dir: Path,
    segments: list[Segment],
    destination: Path,
) -> dict[str, Any]:
    order = {segment.id: index for index, segment in enumerate(segments)}
    if len(order) != len(segments):
        raise ValueError("Ordered source contains duplicate target IDs")
    collected: dict[str, dict[str, Any]] = {}
    shard_root = work_dir / ".game-l10n" / "native" / "shards"
    for shard in sorted(shard_root.glob("*.jsonl")):
        for raw in _read_jsonl(shard):
            row = _validate_row(raw)
            target_id = row["target_id"]
            if target_id not in order:
                raise ValueError(f"Native shard target ID is absent from ordered source: {target_id}")
            if target_id in collected:
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
        "artifact": str(destination),
    }
