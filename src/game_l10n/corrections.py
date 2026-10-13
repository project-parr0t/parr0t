from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import GameL10nError
from .formats import write_results
from .models import AttemptRecord, Segment, TranslationResult
from .release_checkpoint import canonical_checkpoint_bytes
from .store import stable_hash
from .validators import validate_integrity


CORRECTION_REQUEST_SCHEMA = "parr0t.user-correction-request/v1"


class CorrectionError(GameL10nError):
    pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_results(path: Path) -> list[TranslationResult]:
    suffix = path.suffix.casefold()
    if suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    elif suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = payload.get("results", []) if isinstance(payload, dict) else []
    else:
        raise CorrectionError("Correction base results must use JSON or JSONL")
    if not isinstance(rows, list) or not rows:
        raise CorrectionError("Correction base results are empty or malformed")

    results: list[TranslationResult] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise CorrectionError(f"Correction base result {index} must be an object")
        value = dict(row)
        target_id = value.get("segment_id", value.pop("id", None))
        if not isinstance(target_id, str) or not target_id or target_id in seen:
            raise CorrectionError("Correction base results require unique segment IDs")
        seen.add(target_id)
        value["segment_id"] = target_id
        attempts = value.get("attempts", [])
        if not isinstance(attempts, list) or not all(isinstance(item, dict) for item in attempts):
            raise CorrectionError(f"Correction base result {target_id!r} has invalid attempts")
        value["attempts"] = [AttemptRecord(**item) for item in attempts]
        value["from_cache"] = False
        try:
            results.append(TranslationResult(**value))
        except TypeError as error:
            raise CorrectionError(
                f"Correction base result {target_id!r} does not match the result contract"
            ) from error
    return results


def _load_request(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or set(payload) != {"schema", "changes"}:
        raise CorrectionError("Correction request fields must be schema and changes")
    if payload.get("schema") != CORRECTION_REQUEST_SCHEMA:
        raise CorrectionError("Correction request schema is unsupported")
    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        raise CorrectionError("Correction request must contain at least one change")
    result: dict[str, str] = {}
    for index, item in enumerate(changes, start=1):
        if not isinstance(item, dict) or set(item) != {"id", "target"}:
            raise CorrectionError(f"Correction request change {index} is malformed")
        target_id = item.get("id")
        target = item.get("target")
        if (
            not isinstance(target_id, str)
            or not target_id
            or target_id in result
            or not isinstance(target, str)
            or not target.strip()
        ):
            raise CorrectionError("Correction changes require unique IDs and non-empty targets")
        result[target_id] = target
    return result


def _reverse_dependency_closure(
    selected: set[str], dependencies: dict[str, list[str]]
) -> set[str]:
    reverse: dict[str, set[str]] = {target_id: set() for target_id in dependencies}
    for target_id, refs in dependencies.items():
        for ref in refs:
            reverse[ref].add(target_id)
    closure = set(selected)
    pending = list(selected)
    while pending:
        current = pending.pop()
        for dependent in reverse[current]:
            if dependent not in closure:
                closure.add(dependent)
                pending.append(dependent)
    return closure


def apply_user_corrections(
    *,
    segments: list[Segment],
    base_results_path: Path | None,
    checkpoint_path: Path,
    request_path: Path,
    output_path: Path,
    expected_inventory_fingerprint: str,
) -> dict[str, Any]:
    """Apply exact user-specified wording without model calls or broad re-review."""

    if output_path.suffix.casefold() not in {".json", ".jsonl"}:
        raise CorrectionError("Correction output must use JSON or JSONL to retain audit metadata")

    checkpoint, _ = canonical_checkpoint_bytes(checkpoint_path)
    if checkpoint["source_inventory"]["fingerprint"] != expected_inventory_fingerprint:
        raise CorrectionError("Release checkpoint does not match the verified source inventory")

    changes = _load_request(request_path)
    segment_by_id = {item.id: item for item in segments}
    checkpoint_by_id = {item["id"]: item for item in checkpoint["entries"]}
    expected_ids = set(segment_by_id)
    if len(segment_by_id) != len(segments) or set(checkpoint_by_id) != expected_ids:
        raise CorrectionError("Segments and checkpoint target IDs must match exactly")
    if base_results_path is None:
        results = [
            TranslationResult(
                segment_id=segment.id,
                source=segment.source,
                text=checkpoint_by_id[segment.id]["target"],
                status=checkpoint_by_id[segment.id]["status"],
                from_cache=False,
                metadata={"candidate_origin": "release_checkpoint"},
                candidate_generation=1,
                candidate_digest=stable_hash(checkpoint_by_id[segment.id]["target"]),
                review_outcome="reused_release_checkpoint",
                acceptance_authority="release_checkpoint",
            )
            for segment in segments
        ]
        base_source = "release_checkpoint"
    else:
        results = _load_results(base_results_path)
        base_source = "full_results"
    result_by_id = {item.segment_id: item for item in results}
    if set(result_by_id) != expected_ids:
        raise CorrectionError("Segments, base results, and checkpoint target IDs must match exactly")
    unknown = set(changes) - expected_ids
    if unknown:
        raise CorrectionError(f"Correction request contains unknown target IDs: {sorted(unknown)}")

    dependencies: dict[str, list[str]] = {}
    for target_id in expected_ids:
        segment = segment_by_id[target_id]
        result = result_by_id[target_id]
        entry = checkpoint_by_id[target_id]
        if _sha256_text(segment.source) != entry["source_sha256"]:
            raise CorrectionError(f"Checkpoint source binding changed for target {target_id!r}")
        if result.source != segment.source:
            raise CorrectionError(f"Base result source changed for target {target_id!r}")
        if result.text != entry["target"] or _sha256_text(result.text) != entry["target_sha256"]:
            raise CorrectionError(f"Base result does not match the released target {target_id!r}")
        if result.candidate_digest and result.candidate_digest != stable_hash(result.text):
            raise CorrectionError(f"Base result candidate digest is invalid for target {target_id!r}")
        dependencies[target_id] = list(entry["dependencies"])

    selected = set(changes)
    invalidated = _reverse_dependency_closure(selected, dependencies)
    dependent_review = invalidated - selected
    if dependent_review:
        raise CorrectionError(
            "Exact correction requires targeted review of reverse dependencies before application: "
            + ", ".join(sorted(dependent_review))
        )

    deterministic_issues: dict[str, list[str]] = {}
    for target_id, target in changes.items():
        if target == result_by_id[target_id].text:
            raise CorrectionError(f"Correction target {target_id!r} is unchanged")
        issues = validate_integrity(segment_by_id[target_id], target, [])
        if issues:
            deterministic_issues[target_id] = [item.code for item in issues]
    if deterministic_issues:
        raise CorrectionError(
            "Correction failed deterministic validation: "
            + json.dumps(deterministic_issues, sort_keys=True)
        )

    corrected: list[TranslationResult] = []
    for result in results:
        if result.segment_id not in changes:
            corrected.append(result)
            continue
        updated = copy.deepcopy(result)
        updated.text = changes[result.segment_id]
        updated.status = "reviewed"
        updated.from_cache = False
        updated.reviewer = "explicit-user-correction"
        updated.errors = []
        updated.candidate_generation = max(1, updated.candidate_generation + 1)
        updated.candidate_digest = stable_hash(updated.text)
        updated.review_outcome = "user_directed_correction"
        updated.acceptance_authority = "user"
        updated.resolution_code = "explicit_user_correction"
        updated.metadata = {
            **updated.metadata,
            "candidate_origin": "explicit_user_correction",
        }
        updated.attempts.append(
            AttemptRecord(
                stage="user-correction",
                provider="explicit-user-instruction",
                status="accepted",
                candidate_generation=updated.candidate_generation,
            )
        )
        corrected.append(updated)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")
    try:
        write_results(temporary, corrected)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "schema": "parr0t.user-correction-result/v1",
        "status": "corrected",
        "changed_count": len(changes),
        "unchanged_count": len(results) - len(changes),
        "changed_target_ids": sorted(changes),
        "dependent_review_count": 0,
        "model_calls": 0,
        "context_analysis_runs": 0,
        "base_source": base_source,
        "output": str(output_path),
        "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
    }
