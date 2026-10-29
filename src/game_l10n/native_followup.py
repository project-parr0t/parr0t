from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable

from .models import Segment
from .privacy import is_within
from .store import stable_hash, stable_hash_sequence
from .translation_exchange import (
    MANIFEST_NAME as EXCHANGE_MANIFEST_NAME,
    RECEIPT_NAME,
    REQUESTS_NAME,
    SUCCESS_NAME,
    prepare_translation_exchange,
    seal_translation_exchange,
    _context as _exchange_context,
)
from .validators import validate_integrity


FOLLOWUP_SCHEMA = "parr0t.native-followup.v1"
FOLLOWUP_MANIFEST_NAME = "followup.json"
PRIMARY_CANDIDATES_NAME = "primary-candidates.jsonl"
REVIEW_INPUT_NAME = "review-input.jsonl"
UNRESOLVED_NAME = "translation-unresolved.jsonl"
CANDIDATE_METADATA_KEY = "_parr0t_candidate"
MAX_EXCHANGE_RECORDS = 2000


class NativeFollowupError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def _atomic_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    body = b"".join(
        (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        for row in rows
    )
    _atomic_bytes(path, body)


def _relative(work_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(work_dir.resolve()).as_posix()
    except ValueError as exc:
        raise NativeFollowupError("Native follow-up artifacts must stay inside the workspace") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeError as exc:
        raise NativeFollowupError(f"Native follow-up input is not UTF-8: {path.name}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NativeFollowupError(
                f"Native follow-up input has invalid JSONL at row {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise NativeFollowupError(
                f"Native follow-up input row {line_number} must be an object"
            )
        rows.append(row)
    return rows


def _candidate_segment(segment: Segment, row: dict[str, Any]) -> Segment:
    text = row.get("text")
    generation = row.get("candidate_generation")
    digest = row.get("candidate_digest")
    if (
        row.get("status") != "success"
        or not isinstance(text, str)
        or (not text.strip() and bool(validate_integrity(segment, text, [])))
        or not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
        or digest != stable_hash(text)
    ):
        raise NativeFollowupError("Native success row has an invalid candidate binding")
    metadata = dict(segment.metadata)
    if CANDIDATE_METADATA_KEY in metadata:
        raise NativeFollowupError("Source metadata uses the reserved native candidate key")
    metadata[CANDIDATE_METADATA_KEY] = {
        "text": text,
        "generation": generation,
        "digest": digest,
        "provider": str(row.get("provider", "native-worker")),
    }
    return replace(segment, metadata=metadata)


def _load_primary_rows(
    segments: list[Segment], primary_results: Path
) -> tuple[list[Segment], list[Segment]]:
    rows = _read_jsonl(primary_results)
    expected_ids = [segment.id for segment in segments]
    if [row.get("target_id") for row in rows] != expected_ids:
        raise NativeFollowupError(
            "Merged native results must contain every source target once in stable order"
        )
    candidates: list[Segment] = []
    failures: list[Segment] = []
    for segment, row in zip(segments, rows, strict=True):
        status = row.get("status")
        if status == "success":
            candidates.append(_candidate_segment(segment, row))
        elif status in {"refused", "error"} and row.get("text", "") == "":
            failures.append(segment)
        else:
            raise NativeFollowupError("Merged native results contain an invalid terminal row")
    return candidates, failures


def prepare_native_followup(
    work_dir: Path,
    source_path: Path,
    segments: list[Segment],
    primary_results: Path,
    output_dir: Path,
    *,
    exchange_prefix: str,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    source_path = source_path.resolve()
    primary_results = primary_results.resolve()
    output_dir = output_dir.resolve()
    for path in (source_path, primary_results, output_dir):
        if not is_within(path, work_dir):
            raise NativeFollowupError("Native follow-up paths must stay inside the workspace")
    if not source_path.is_file() or not primary_results.is_file():
        raise NativeFollowupError("Native follow-up source and primary results are required")
    if not segments:
        raise NativeFollowupError("Native follow-up source contains no targets")
    if len({segment.id for segment in segments}) != len(segments):
        raise NativeFollowupError("Native follow-up source contains duplicate target IDs")

    source_fingerprint = stable_hash_sequence(asdict(segment) for segment in segments)
    result_sha256 = _sha256(primary_results)
    manifest_path = output_dir / FOLLOWUP_MANIFEST_NAME
    if output_dir.exists() and any(output_dir.iterdir()):
        if not manifest_path.is_file():
            raise NativeFollowupError("Native follow-up output must be new or previously prepared")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema") != FOLLOWUP_SCHEMA
            or manifest.get("source_fingerprint") != source_fingerprint
            or manifest.get("primary_results_sha256") != result_sha256
        ):
            raise NativeFollowupError("Existing native follow-up is bound to different inputs")
        artifact_paths = [
            manifest.get("primary_candidates"),
            *(manifest.get("fallback_bundles") or []),
        ]
        if not all(
            isinstance(path, str) and (work_dir / path).exists() for path in artifact_paths
        ):
            raise NativeFollowupError("Existing native follow-up is incomplete")
        return {
            "schema": FOLLOWUP_SCHEMA,
            "cached": True,
            "candidate_count": manifest["candidate_count"],
            "fallback_count": manifest["fallback_count"],
            "fallback_bundles": manifest["fallback_bundles"],
            "primary_candidates": manifest["primary_candidates"],
            "manifest": _relative(work_dir, manifest_path),
        }

    candidates, failures = _load_primary_rows(segments, primary_results)
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = output_dir / PRIMARY_CANDIDATES_NAME
    _atomic_jsonl(candidate_path, (asdict(segment) for segment in candidates))

    bundles: list[str] = []
    for offset in range(0, len(failures), MAX_EXCHANGE_RECORDS):
        number = offset // MAX_EXCHANGE_RECORDS + 1
        bundle = output_dir / f"translation-fallback-{number:04d}"
        prepare_translation_exchange(
            work_dir,
            failures[offset : offset + MAX_EXCHANGE_RECORDS],
            bundle,
            exchange_id=f"{exchange_prefix}-{number:04d}",
            mode="fallback",
            source_queue="translation_fallback_pending",
        )
        bundles.append(_relative(work_dir, bundle))

    manifest = {
        "schema": FOLLOWUP_SCHEMA,
        "source": _relative(work_dir, source_path),
        "source_fingerprint": source_fingerprint,
        "primary_results": _relative(work_dir, primary_results),
        "primary_results_sha256": result_sha256,
        "target_count": len(segments),
        "candidate_count": len(candidates),
        "fallback_count": len(failures),
        "primary_candidates": _relative(work_dir, candidate_path),
        "fallback_bundles": bundles,
    }
    _atomic_json(manifest_path, manifest)
    return {
        "schema": FOLLOWUP_SCHEMA,
        "cached": False,
        "candidate_count": len(candidates),
        "fallback_count": len(failures),
        "fallback_bundles": bundles,
        "primary_candidates": manifest["primary_candidates"],
        "manifest": _relative(work_dir, manifest_path),
    }


def _exchange_success_candidates(work_dir: Path, bundle: Path) -> dict[str, dict[str, Any]]:
    sealed = seal_translation_exchange(work_dir, bundle)
    manifest = json.loads((bundle / EXCHANGE_MANIFEST_NAME).read_text(encoding="utf-8"))
    receipt = json.loads((bundle / RECEIPT_NAME).read_text(encoding="utf-8"))
    if manifest.get("mode") != "fallback" or manifest.get("source_queue") != "translation_fallback_pending":
        raise NativeFollowupError("Native follow-up accepts only sealed translation fallback bundles")
    if sealed.get("success_count") != receipt.get("success_count"):
        raise NativeFollowupError("Translation fallback receipt is inconsistent")
    requests = {row["target_id"]: row for row in _read_jsonl(bundle / REQUESTS_NAME)}
    successes: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(bundle / SUCCESS_NAME):
        request = requests.get(row.get("target_id"))
        if request is None:
            raise NativeFollowupError("Translation fallback success is not request-bound")
        successes[row["target_id"]] = {
            "text": row["text"],
            "generation": 1,
            "digest": stable_hash(row["text"]),
            "provider": row["route"],
        }
    return successes


def collect_translation_exchange_candidates(
    work_dir: Path, segments: list[Segment], bundle: Path,
    review_output: Path, unresolved_output: Path,
) -> dict[str, Any]:
    """Join one sealed failure-only exchange without inventing a primary pass.

    Existing failed attempts and their usage remain in the exchange/audit
    history. Only first valid candidates receive a generation-one binding.
    """
    from .control_plane import require_control_plane_ready
    require_control_plane_ready(work_dir)
    work_dir = work_dir.resolve()
    for path in (bundle, review_output, unresolved_output):
        if not is_within(path.resolve(), work_dir):
            raise NativeFollowupError("Native follow-up paths must stay inside the workspace")
    if review_output.resolve() == unresolved_output.resolve():
        raise NativeFollowupError("Native follow-up output paths must differ")
    candidates = _exchange_success_candidates(work_dir, bundle)
    requests = _read_jsonl(bundle / REQUESTS_NAME)
    if [r["target_id"] for r in requests] != [s.id for s in segments]:
        raise NativeFollowupError("Translation exchange targets do not match ordered source")
    review, unresolved = [], []
    for segment, request in zip(segments, requests, strict=True):
        if (request["source"] != segment.source or
                request["context"] != _exchange_context(segment)):
            raise NativeFollowupError("Translation exchange source or context is stale")
        if CANDIDATE_METADATA_KEY in segment.metadata:
            raise NativeFollowupError("Existing candidates cannot be rebound as generation one")
        candidate = candidates.get(segment.id)
        if candidate is None:
            unresolved.append(segment)
        else:
            review.append(replace(segment, metadata={
                **segment.metadata, CANDIDATE_METADATA_KEY: candidate,
            }))
    _atomic_jsonl(review_output, (asdict(s) for s in review))
    _atomic_jsonl(unresolved_output, (asdict(s) for s in unresolved))
    return {"schema": FOLLOWUP_SCHEMA, "candidate_count": len(review),
            "unresolved_count": len(unresolved),
            "review_input": _relative(work_dir, review_output),
            "unresolved": _relative(work_dir, unresolved_output),
            "translation_barrier_complete": not unresolved}


def collect_native_followup(
    work_dir: Path,
    followup_dir: Path,
    segments: list[Segment],
    review_output: Path,
    unresolved_output: Path,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    followup_dir = followup_dir.resolve()
    review_output = review_output.resolve()
    unresolved_output = unresolved_output.resolve()
    for path in (followup_dir, review_output, unresolved_output):
        if not is_within(path, work_dir):
            raise NativeFollowupError("Native follow-up paths must stay inside the workspace")
    manifest_path = followup_dir / FOLLOWUP_MANIFEST_NAME
    if not manifest_path.is_file():
        raise NativeFollowupError("Native follow-up manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema") != FOLLOWUP_SCHEMA
        or manifest.get("source_fingerprint")
        != stable_hash_sequence(asdict(segment) for segment in segments)
        or not isinstance(manifest.get("fallback_bundles"), list)
    ):
        raise NativeFollowupError("Native follow-up manifest is stale or malformed")

    candidates: dict[str, Segment] = {}
    primary_candidates = followup_dir / PRIMARY_CANDIDATES_NAME
    for row in _read_jsonl(primary_candidates):
        try:
            segment = Segment(**row)
        except (TypeError, ValueError) as exc:
            raise NativeFollowupError("Primary native candidate artifact is invalid") from exc
        candidates[segment.id] = segment

    source_by_id = {segment.id: segment for segment in segments}
    for relative in manifest["fallback_bundles"]:
        bundle = (work_dir / relative).resolve()
        if not is_within(bundle, work_dir):
            raise NativeFollowupError("Translation fallback bundle escaped the workspace")
        for target_id, candidate in _exchange_success_candidates(work_dir, bundle).items():
            source = source_by_id.get(target_id)
            if source is None or target_id in candidates:
                raise NativeFollowupError("Translation fallback candidate is unknown or duplicated")
            metadata = dict(source.metadata)
            if CANDIDATE_METADATA_KEY in metadata:
                raise NativeFollowupError("Source metadata uses the reserved native candidate key")
            metadata[CANDIDATE_METADATA_KEY] = candidate
            candidates[target_id] = replace(source, metadata=metadata)

    ordered_candidates = [candidates[segment.id] for segment in segments if segment.id in candidates]
    unresolved = [segment for segment in segments if segment.id not in candidates]
    _atomic_jsonl(review_output, (asdict(segment) for segment in ordered_candidates))
    _atomic_jsonl(unresolved_output, (asdict(segment) for segment in unresolved))
    return {
        "schema": FOLLOWUP_SCHEMA,
        "candidate_count": len(ordered_candidates),
        "unresolved_count": len(unresolved),
        "review_input": _relative(work_dir, review_output),
        "unresolved": _relative(work_dir, unresolved_output),
        "translation_barrier_complete": len(unresolved) == 0,
    }
