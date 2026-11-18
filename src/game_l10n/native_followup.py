from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable

from .content_source_policy import validate_content_source_policy
from .models import Segment
from .privacy import is_within
from .store import stable_hash, stable_hash_sequence
from .translation_exchange import (
    MANIFEST_NAME as EXCHANGE_MANIFEST_NAME,
    RECEIPT_NAME,
    FAILURE_NAME,
    ADAPTER_LEDGER_NAME,
    ENVELOPE_NAME,
    REQUESTS_NAME,
    SUCCESS_NAME,
    TranslationExchangeError,
    prepare_translation_exchange,
    seal_translation_exchange,
    _context as _exchange_context,
    _response_digest,
)
from .validators import validate_integrity


FOLLOWUP_SCHEMA = "parr0t.native-followup.v1"
FOLLOWUP_MANIFEST_NAME = "followup.json"
PRIMARY_CANDIDATES_NAME = "primary-candidates.jsonl"
REVIEW_INPUT_NAME = "review-input.jsonl"
UNRESOLVED_NAME = "translation-unresolved.jsonl"
CANDIDATE_METADATA_KEY = "_parr0t_candidate"
DEFAULT_EXCHANGE_RECORDS = 2000


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
        lines = path.read_text(encoding="utf-8-sig").split("\n")
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
    candidate = {
        "text": text,
        "generation": generation,
        "digest": digest,
        "provider": str(row.get("provider", "native-worker")),
    }
    provenance = row.get("content_provenance")
    if provenance is not None:
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
        ):
            raise NativeFollowupError("Native success provenance is invalid")
        candidate["content_provenance"] = dict(provenance)
        candidate["content_provenance_lineage"] = [dict(provenance)]
    metadata[CANDIDATE_METADATA_KEY] = candidate
    return replace(segment, metadata=metadata)


def _load_primary_rows(
    segments: list[Segment],
    primary_results: Path,
    *,
    content_source_policy: dict[str, Any] | None = None,
    retry_route_models: dict[str, str] | None = None,
    content_source_policies: list[dict[str, Any]] | None = None,
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
            if content_source_policy is not None or content_source_policies:
                provenance = row.get("content_provenance")
                route = provenance.get("route") if isinstance(provenance, dict) else None
                if (
                    not isinstance(provenance, dict)
                    or retry_route_models is None
                    or not isinstance(route, str)
                    or route not in retry_route_models
                ):
                    raise NativeFollowupError("E_OUTPUT_PROVENANCE_INVALID")
                from .content_source_policy import validate_output_provenance

                policy = next(
                    (item for item in content_source_policies or []
                     if item["policy_digest"] == provenance.get("policy_digest")),
                    content_source_policy,
                )
                if policy is None or provenance.get("policy_digest") != policy["policy_digest"]:
                    raise NativeFollowupError("E_OUTPUT_PROVENANCE_INVALID")
                validate_output_provenance(
                    provenance,
                    policy=policy,
                    model=retry_route_models[route],
                    route=route,
                )
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
    external_fallback: bool = True,
    content_source_policy: dict[str, Any] | None = None,
    retry_lineage: dict[str, Any] | None = None,
    retry_route_plan: list[dict[str, Any]] | None = None,
    exchange_max_records: int = DEFAULT_EXCHANGE_RECORDS,
    cohort_lineage: dict[str, Any] | None = None,
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
    if not isinstance(exchange_max_records, int) or isinstance(exchange_max_records, bool) or exchange_max_records < 1:
        raise NativeFollowupError("exchange_max_records must be a positive integer")

    source_fingerprint = stable_hash_sequence(asdict(segment) for segment in segments)
    result_sha256 = _sha256(primary_results)
    historical_policies: list[dict[str, Any]] | None = None
    if cohort_lineage is not None:
        from .native_translation_retry import verify_native_translation_retry_cohort
        verified = verify_native_translation_retry_cohort(
            work_dir, cohort_lineage, source_path=source_path, results_path=primary_results
        )
        if (
            source_fingerprint != cohort_lineage["source_fingerprint"]
            or retry_lineage is not None and retry_lineage != verified["primary_lineage"]
            or retry_route_plan is not None and retry_route_plan != verified["route_plan"]
        ):
            raise NativeFollowupError("E_NATIVE_FOLLOWUP_COHORT_BINDING_INVALID")
        retry_lineage = verified["primary_lineage"]
        retry_route_plan = verified["route_plan"]
        historical_policies = verified["content_source_policies"]
        if content_source_policy is None or content_source_policy not in historical_policies:
            raise NativeFollowupError("E_NATIVE_FOLLOWUP_ACTIVE_POLICY_REQUIRED")
    if content_source_policy is not None:
        content_source_policy = validate_content_source_policy(
            content_source_policy
        )
        if (
            content_source_policy["task_kind"] != "translation"
            or content_source_policy["route"] != "native-pass:translation"
        ):
            raise NativeFollowupError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    retry_route_models: dict[str, str] | None = None
    if retry_route_plan is not None:
        if (
            not isinstance(retry_route_plan, list)
            or not retry_route_plan
            or any(
                not isinstance(item, dict)
                or set(item) not in (
                    {"provider", "max_retries"},
                    {"provider", "model", "max_retries"},
                )
                or not isinstance(item.get("provider"), str)
                or not item["provider"]
                or (
                    "model" in item
                    and (
                        not isinstance(item["model"], str)
                        or not item["model"]
                    )
                )
                for item in retry_route_plan
            )
        ):
            raise NativeFollowupError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
        retry_route_models = {
            item["provider"]: str(item.get("model", ""))
            for item in retry_route_plan
        }
        if len(retry_route_models) != len(retry_route_plan) or any(not model for model in retry_route_models.values()):
            raise NativeFollowupError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    if content_source_policy is not None and retry_route_models is None:
        raise NativeFollowupError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    if retry_lineage is not None and (
        not isinstance(retry_lineage, dict)
        or cohort_lineage is None and retry_lineage.get("source_fingerprint") != source_fingerprint
        or (
            content_source_policy is not None
            and retry_lineage.get("content_source_policy_digest") not in {
                item["policy_digest"] for item in historical_policies or [content_source_policy]
            }
        )
    ):
        raise NativeFollowupError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    candidates, failures = _load_primary_rows(
        segments,
        primary_results,
        content_source_policy=content_source_policy,
        retry_route_models=retry_route_models,
        content_source_policies=historical_policies,
    )
    translation_continuation = None
    if failures and external_fallback and cohort_lineage is not None and (work_dir / "game-l10n.json").is_file():
        from .workspace import load_config
        from .translation_exchange import configured_translation_route_plan, build_translation_continuation
        full_plan = configured_translation_route_plan(work_dir, load_config(work_dir))
        if full_plan:
            native_plan = retry_route_plan or []
            native_start = next(
                (index for index, item in enumerate(full_plan) if native_plan and item["provider"] == native_plan[0]["provider"]),
                None,
            )
            if native_start is not None and native_start + len(native_plan) < len(full_plan):
                translation_continuation = build_translation_continuation(
                    work_dir, full_plan, native_cohort=cohort_lineage,
                )
            elif native_start is not None and native_start + len(native_plan) == len(full_plan):
                external_fallback = False
    manifest_path = output_dir / FOLLOWUP_MANIFEST_NAME
    if output_dir.exists() and any(output_dir.iterdir()):
        if not manifest_path.is_file():
            raise NativeFollowupError("Native follow-up output must be new or previously prepared")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema") != FOLLOWUP_SCHEMA
            or manifest.get("source_fingerprint") != source_fingerprint
            or manifest.get("primary_results_sha256") != result_sha256
            or failures and manifest.get("external_fallback", True) != external_fallback
            or manifest.get("content_source_policy") != content_source_policy
            or manifest.get("retry_lineage") != retry_lineage
            or manifest.get("retry_route_plan") != retry_route_plan
            or manifest.get("exchange_max_records", DEFAULT_EXCHANGE_RECORDS) != exchange_max_records
            or manifest.get("cohort_lineage") != cohort_lineage
            or manifest.get("content_source_policies") != historical_policies
            or failures and manifest.get("translation_continuation") != translation_continuation
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
            "collection": _followup_collection_request(work_dir, output_dir, cohort_lineage, standalone=segments),
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = output_dir / PRIMARY_CANDIDATES_NAME
    _atomic_jsonl(candidate_path, (asdict(segment) for segment in candidates))

    bundles: list[str] = []
    for offset in range(0, len(failures) if external_fallback else 0, exchange_max_records):
        number = offset // exchange_max_records + 1
        bundle = output_dir / f"translation-fallback-{number:04d}"
        prepare_translation_exchange(
            work_dir,
            failures[offset : offset + exchange_max_records],
            bundle,
            exchange_id=f"{exchange_prefix}-{number:04d}",
            mode="fallback",
            source_queue="translation_fallback_pending",
            content_source_policy=content_source_policy,
            retry_lineage=retry_lineage,
            content_source_policy_history=historical_policies,
            translation_continuation=translation_continuation,
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
        "exchange_max_records": exchange_max_records,
        "primary_candidates": _relative(work_dir, candidate_path),
        "fallback_bundles": bundles,
        **({"translation_continuation": translation_continuation} if translation_continuation is not None else {}),
        **(
            {"content_source_policy": content_source_policy}
            if content_source_policy is not None
            else {}
        ),
        **(
            {"retry_lineage": retry_lineage}
            if retry_lineage is not None
            else {}
        ),
        **(
            {"retry_route_plan": retry_route_plan}
            if retry_route_plan is not None
            else {}
        ),
        **({"cohort_lineage": cohort_lineage, "content_source_policies": historical_policies}
           if cohort_lineage is not None else {}),
    }
    if not external_fallback:
        manifest["external_fallback"] = False
    _atomic_json(manifest_path, manifest)
    return {
        "schema": FOLLOWUP_SCHEMA,
        "cached": False,
        "candidate_count": len(candidates),
        "fallback_count": len(failures),
        "fallback_bundles": bundles,
        "primary_candidates": manifest["primary_candidates"],
        "manifest": _relative(work_dir, manifest_path),
        "collection": _followup_collection_request(work_dir, output_dir, cohort_lineage, standalone=segments),
    }


def _exchange_success_candidates(
    work_dir: Path, bundle: Path, *, write_outputs: bool = True,
) -> dict[str, dict[str, Any]]:
    sealed = seal_translation_exchange(work_dir, bundle, write_outputs=write_outputs)
    manifest = json.loads((bundle / EXCHANGE_MANIFEST_NAME).read_text(encoding="utf-8"))
    if manifest.get("mode") != "fallback" or manifest.get("source_queue") != "translation_fallback_pending":
        raise NativeFollowupError("Native follow-up accepts only sealed translation fallback bundles")
    requests = {row["target_id"]: row for row in _read_jsonl(bundle / REQUESTS_NAME)}
    successes: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(bundle / SUCCESS_NAME):
        request = requests.get(row.get("target_id"))
        if request is None:
            raise NativeFollowupError("Translation fallback success is not request-bound")
        candidate = {
            "text": row["text"],
            "generation": 1,
            "digest": stable_hash(row["text"]),
            "provider": row["route"],
        }
        if isinstance(row.get("model"), str) and row["model"]:
            candidate["model"] = row["model"]
        if isinstance(row.get("attempt_lineage"), list):
            candidate["adapter_attempt_lineage"] = [dict(item) for item in row["attempt_lineage"]]
        if "content_provenance" in row:
            candidate["content_provenance"] = dict(
                row["content_provenance"]
            )
            candidate["content_provenance_lineage"] = [
                dict(row["content_provenance"])
            ]
        successes[row["target_id"]] = candidate
    if len(successes) != sealed["success_count"] or _response_digest(bundle) != sealed["response_sha256"]:
        raise NativeFollowupError("Translation fallback changed during collection")
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
    if any(
        request["source"] != segment.source or request["context"] != _exchange_context(segment)
        for segment, request in zip(segments, requests, strict=True)
    ):
        from .translation_exchange import _request_segment
        _, canonical = _freeze_collection_inventory(
            work_dir, bundle, [_request_segment(row) for row in requests],
        )
        if segments != canonical:
            raise NativeFollowupError("Translation exchange source or context is stale")
    review = []
    for segment in segments:
        if CANDIDATE_METADATA_KEY in segment.metadata:
            raise NativeFollowupError("Existing candidates cannot be rebound as generation one")
        candidate = candidates.get(segment.id)
        if candidate is not None:
            review.append(replace(segment, metadata={
                **segment.metadata, CANDIDATE_METADATA_KEY: candidate,
            }))
    return _write_followup_collection(
        work_dir, segments, {item.id: item for item in review}, review_output, unresolved_output, [],
        protected_files={bundle / name for name in (
            EXCHANGE_MANIFEST_NAME, REQUESTS_NAME, SUCCESS_NAME, FAILURE_NAME,
            ADAPTER_LEDGER_NAME, RECEIPT_NAME, ENVELOPE_NAME, "chain-input.jsonl",
        )},
        protected_dirs={bundle / "native-continuations"},
    )


def _collect_followup_candidates(
    work_dir: Path,
    followup_dir: Path,
    segments: list[Segment],
    *,
    frozen_bundles: dict[str, Path] | None = None,
) -> tuple[dict[str, Segment], list[str], dict[str, Any]]:
    work_dir = work_dir.resolve()
    followup_dir = followup_dir.resolve()
    if not is_within(followup_dir, work_dir):
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
    if manifest.get("cohort_lineage") is not None:
        from .native_translation_retry import verify_native_translation_retry_cohort
        if not isinstance(manifest.get("source"), str) or not isinstance(manifest.get("primary_results"), str):
            raise NativeFollowupError("E_NATIVE_FOLLOWUP_COHORT_BINDING_INVALID")
        results_path = work_dir / manifest["primary_results"]
        verified = verify_native_translation_retry_cohort(
            work_dir, manifest["cohort_lineage"],
            source_path=work_dir / manifest["source"], results_path=results_path,
        )
        if (
            manifest.get("retry_lineage") != verified["primary_lineage"]
            or manifest.get("retry_route_plan") != verified["route_plan"]
            or manifest.get("content_source_policies") != verified["content_source_policies"]
            or manifest.get("content_source_policy") not in verified["content_source_policies"]
        ):
            raise NativeFollowupError("E_NATIVE_FOLLOWUP_COHORT_BINDING_INVALID")
        # Rejoin the authoritative frozen outcomes, not a mutable derived cache.
        primary, _ = _load_primary_rows(
            segments, results_path,
            content_source_policy=manifest["content_source_policy"],
            content_source_policies=verified["content_source_policies"],
            retry_route_models={item["provider"]: item["model"] for item in verified["route_plan"]},
        )
        candidates = {segment.id: segment for segment in primary}
    else:
        primary_candidates = followup_dir / PRIMARY_CANDIDATES_NAME
        for row in _read_jsonl(primary_candidates):
            try:
                segment = Segment(**row)
            except (TypeError, ValueError) as exc:
                raise NativeFollowupError("Primary native candidate artifact is invalid") from exc
            candidates[segment.id] = segment

    source_by_id = {segment.id: segment for segment in segments}
    blocked_bundles: list[str] = []
    for relative in manifest["fallback_bundles"]:
        bundle = (frozen_bundles or {}).get(relative, (work_dir / relative).resolve())
        if not is_within(bundle, work_dir):
            raise NativeFollowupError("Translation fallback bundle escaped the workspace")
        try:
            bundle_candidates = _exchange_success_candidates(
                work_dir, bundle, write_outputs=relative not in (frozen_bundles or {}),
            )
            if any(target_id not in source_by_id or target_id in candidates
                   for target_id in bundle_candidates):
                raise NativeFollowupError("Translation fallback candidate is unknown or duplicated")
        except (NativeFollowupError, TranslationExchangeError, json.JSONDecodeError, OSError):
            blocked_bundles.append(relative)
            continue
        for target_id, candidate in bundle_candidates.items():
            source = source_by_id[target_id]
            metadata = dict(source.metadata)
            if CANDIDATE_METADATA_KEY in metadata:
                raise NativeFollowupError("Source metadata uses the reserved native candidate key")
            metadata[CANDIDATE_METADATA_KEY] = candidate
            candidates[target_id] = replace(source, metadata=metadata)

    return candidates, blocked_bundles, manifest


def _write_followup_collection(
    work_dir: Path, segments: list[Segment], candidates: dict[str, Segment],
    review_output: Path, unresolved_output: Path, blocked_bundles: list[str],
    *, protected_files: set[Path] | None = None, protected_dirs: set[Path] | None = None,
) -> dict[str, Any]:
    review_output, unresolved_output = review_output.resolve(), unresolved_output.resolve()
    if review_output == unresolved_output:
        raise NativeFollowupError("Native follow-up output paths must differ")
    for output in (review_output, unresolved_output):
        if (
            not is_within(output, work_dir) or output in (protected_files or set())
            or any(is_within(output, directory) for directory in protected_dirs or set())
        ):
            raise NativeFollowupError("E_NATIVE_FOLLOWUP_OUTPUT_INVALID: do not overwrite bound inputs or frozen evidence")
    ordered = [candidates[item.id] for item in segments if item.id in candidates]
    unresolved = [item for item in segments if item.id not in candidates]
    _atomic_jsonl(review_output, (asdict(item) for item in ordered))
    _atomic_jsonl(unresolved_output, (asdict(item) for item in unresolved))
    return {
        "schema": FOLLOWUP_SCHEMA, "candidate_count": len(ordered),
        "unresolved_count": len(unresolved),
        "review_input": _relative(work_dir, review_output),
        "unresolved": _relative(work_dir, unresolved_output),
        "translation_barrier_complete": not unresolved,
        "review_ready": bool(ordered), "blocked_bundles": blocked_bundles,
        "issue_codes": ["E_NATIVE_FOLLOWUP_BUNDLE_REPAIR_REQUIRED"] if blocked_bundles else [],
    }


def collect_native_followup(
    work_dir: Path, followup_dir: Path, segments: list[Segment],
    review_output: Path, unresolved_output: Path,
) -> dict[str, Any]:
    work_dir, followup_dir = work_dir.resolve(), followup_dir.resolve()
    candidates, blocked, manifest = _collect_followup_candidates(work_dir, followup_dir, segments)
    protected = {followup_dir / FOLLOWUP_MANIFEST_NAME}
    protected.update(
        (work_dir / manifest[key]).resolve()
        for key in ("source", "primary_results", "primary_candidates")
        if isinstance(manifest.get(key), str)
    )
    return _write_followup_collection(
        work_dir, segments, candidates, review_output, unresolved_output, blocked,
        protected_files=protected,
    )


def collect_native_followup_chain(
    work_dir: Path, followup_dirs: list[Path], segments: list[Segment],
    review_output: Path, unresolved_output: Path,
) -> dict[str, Any]:
    """Join verified native/API ancestry into the original ordered inventory."""
    from .control_plane import require_control_plane_ready
    from .formats import read_segments
    from .native_translation_retry import (
        verify_native_translation_retry_cohort,
        load_native_translation_retry_cohort_successes,
    )
    from .translation_exchange import verify_translation_predecessor, _request_segment

    require_control_plane_ready(work_dir)
    work_dir = work_dir.resolve()
    paths = list(dict.fromkeys(path.resolve() for path in followup_dirs))
    if not paths or not segments or len({item.id for item in segments}) != len(segments):
        raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_INPUT_INVALID")
    for path in paths:
        _relative(work_dir, path)
    if any(CANDIDATE_METADATA_KEY in item.metadata for item in segments):
        raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_INPUT_INVALID: use the original source inventory")
    if len(paths) == 1:
        legacy_path = paths[0] / FOLLOWUP_MANIFEST_NAME
        legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        if legacy.get("cohort_lineage") is None and legacy.get("translation_continuation") is None:
            # Old standalone passes remain full inventories without inventing
            # historical models or pretending a typed successor is a root.
            candidates, blocked, manifest = _collect_followup_candidates(work_dir, paths[0], segments)
            protected = {legacy_path, paths[0] / "chain-input.jsonl"}
            protected.update((work_dir / manifest[key]).resolve() for key in (
                "source", "primary_results", "primary_candidates",
            ))
            result = _write_followup_collection(
                work_dir, segments, candidates, review_output, unresolved_output, blocked,
                protected_files=protected,
                protected_dirs={(work_dir / item).resolve() for item in manifest["fallback_bundles"]},
            )
            result.update(
                joined_followups=[_relative(work_dir, paths[0])], lineage_receipts=[],
                source_fingerprint=stable_hash_sequence(asdict(item) for item in segments),
                legacy_standalone=True,
            )
            return result
    inventory = {item.id: item for item in segments}
    inventory_fingerprint = stable_hash_sequence(asdict(item) for item in segments)
    nodes: list[tuple[Path, dict[str, Any], list[Segment]]] = []
    pending: list[dict[str, Any]] = []
    cohorts: dict[str, tuple[dict[str, Any], dict[str, Any], list[Segment]]] = {}
    parents: dict[str, tuple[str, str]] = {}
    receipts: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, Path] = {}
    blocked_followups: list[str] = []
    blocked_cohorts: list[str] = []
    protected_files: set[Path] = set()
    protected_dirs: set[Path] = set()

    def artifact(value: str) -> Path:
        path = (work_dir / value).resolve()
        _relative(work_dir, path)
        return path

    def check_sources(source: list[Segment]) -> None:
        if any(item.id not in inventory or inventory[item.id].source != item.source for item in source):
            raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_SOURCE_MISMATCH")

    def predecessor(binding: dict[str, Any]) -> dict[str, Any]:
        key = binding["receipt"]
        if key not in receipts:
            proof = verify_translation_predecessor(work_dir, artifact(key))
            if any(proof[field] != binding[field] for field in ("receipt_sha256", "contract_fingerprint")):
                raise NativeFollowupError("E_TRANSLATION_PREDECESSOR_INVALID")
            snapshot = artifact(proof["files"]["manifest"]).parent
            prior = proof["prior_native_cohort"]
            if prior is not None:
                pending.append(prior)
            else:
                original = [_request_segment(row) for row in _read_jsonl(snapshot / REQUESTS_NAME)]
                if stable_hash_sequence(asdict(item) for item in original) != inventory_fingerprint:
                    _, canonical = _freeze_collection_inventory(work_dir, paths[0], original)
                    if stable_hash_sequence(asdict(item) for item in canonical) != inventory_fingerprint:
                        raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_ROOT_SOURCE_MISMATCH")
            receipts[key] = proof
            snapshots[proof["exchange"]] = snapshot
            protected_dirs.add(artifact(key).parent)
        return receipts[key]

    def register(path: Path, expected_cohort: dict[str, Any] | None = None) -> None:
        manifest = json.loads((path / FOLLOWUP_MANIFEST_NAME).read_text(encoding="utf-8"))
        cohort = manifest.get("cohort_lineage")
        if not isinstance(cohort, dict) or expected_cohort is not None and cohort != expected_cohort:
            raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_LINEAGE_REQUIRED")
        source = read_segments(artifact(cohort["source_artifact"]))
        check_sources(source)
        protected_files.update((path / FOLLOWUP_MANIFEST_NAME, path / "chain-input.jsonl"))
        for field in ("source", "primary_results", "primary_candidates"):
            if isinstance(manifest.get(field), str):
                protected_files.add(artifact(manifest[field]))
        for bundle in manifest.get("fallback_bundles", []):
            protected_files.update(
                artifact(bundle) / name for name in (
                    EXCHANGE_MANIFEST_NAME, REQUESTS_NAME, SUCCESS_NAME, FAILURE_NAME,
                    ADAPTER_LEDGER_NAME, RECEIPT_NAME, ENVELOPE_NAME,
                )
            )
        nodes.append((path, manifest, source))
        pending.append(cohort)

    for path in paths:
        try:
            register(path)
        except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
            blocked_followups.append(_relative(work_dir, path))

    for cohort in pending:
        key = cohort.get("export_path") if isinstance(cohort, dict) else None
        if isinstance(key, str) and key in cohorts:
            continue
        try:
            if not isinstance(key, str) or not key:
                raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_LINEAGE_REQUIRED")
            verified = verify_native_translation_retry_cohort(
                work_dir, cohort, source_path=artifact(cohort["source_artifact"]),
                results_path=artifact(cohort["results_artifact"]),
            )
            lineage = verified["primary_lineage"] or {}
            binding = lineage.get("translation_predecessor")
            if binding is not None:
                proof = predecessor(binding)
                source = read_segments(artifact(proof["source"]))
                prior = proof["prior_native_cohort"]
                parent = ("native", prior["state"]) if prior is not None else ("external", proof["exchange"])
            else:
                source = segments
                if cohort["root_source_fingerprint"] != inventory_fingerprint:
                    raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_ROOT_SOURCE_MISMATCH")
                parent = ("root", cohort["state"])
            check_sources(source)
            if stable_hash_sequence(asdict(item) for item in source) != cohort["root_source_fingerprint"]:
                raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_SOURCE_MISMATCH")
            state = cohort["state"]
            if state in parents and parents[state] != parent:
                raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_LINEAGE_CONFLICT")
            parents[state] = parent
            cohorts[key] = (cohort, verified, source)
            protected_dirs.add(artifact(key).parent)
            protected_dirs.add(artifact(cohort["state"]).parent / "attempt-results")
            protected_files.add(artifact(cohort["state"]))
            if isinstance(lineage.get("manifest"), str):
                native_manifest = artifact(lineage["manifest"])
                protected_files.add(native_manifest)
                metadata = json.loads(native_manifest.read_text(encoding="utf-8"))
                protected_files.update(native_manifest.parent / item["file"] for item in metadata["tasks"])
                protected_files.add(artifact(metadata["source_artifact"]))
        except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
            blocked_cohorts.append(key if isinstance(key, str) else stable_hash(cohort))

    # Include independent sibling bundles from a bound predecessor follow-up,
    # even when their successes did not produce a native continuation.
    for proof in receipts.values():
        prior = proof["prior_native_cohort"]
        path = artifact(proof["exchange"]).parent
        if prior is None or any(node[0] == path for node in nodes):
            continue
        try:
            register(path, prior)
        except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
            blocked_followups.append(_relative(work_dir, path))

    def root_for(state: str) -> tuple[str, str] | None:
        seen = set()
        while state not in seen:
            seen.add(state)
            if state not in parents:
                return None
            kind, parent = parents[state]
            if kind == "root":
                return ("native", parent)
            if kind == "external":
                return (kind, parent)
            state = parent
        return None

    anchor_roots = {
        root_for(manifest["cohort_lineage"]["state"])
        for path, manifest, _ in nodes
        if path == paths[0] and manifest["cohort_lineage"]["state"] in parents
    }
    if not anchor_roots:
        for proof in receipts.values():
            if artifact(proof["exchange"]).parent == paths[0]:
                prior = proof["prior_native_cohort"]
                anchor_roots.add(root_for(prior["state"]) if prior is not None else ("external", proof["exchange"]))
    anchor_roots.discard(None)
    if len(anchor_roots) != 1:
        raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_ROOT_BINDING_REQUIRED")
    anchor = next(iter(anchor_roots))
    candidates: dict[str, Segment] = {}
    blocked_bundles: list[str] = []
    conflicting_targets: set[str] = set()

    def merge(items: Iterable[Segment]) -> None:
        for item in items:
            check_sources([item])
            base = inventory[item.id]
            candidate = replace(base, metadata={**base.metadata, CANDIDATE_METADATA_KEY: item.metadata[CANDIDATE_METADATA_KEY]})
            if item.id in conflicting_targets:
                continue
            if item.id in candidates and candidates[item.id] != candidate:
                conflicting_targets.add(item.id)
                del candidates[item.id]
            else:
                candidates[item.id] = candidate

    for key, (cohort, verified, _) in cohorts.items():
        if root_for(cohort["state"]) != anchor:
            continue
        try:
            selected = read_segments(artifact(cohort["source_artifact"]))
            primary, _ = _load_primary_rows(
                selected, artifact(cohort["results_artifact"]),
                content_source_policy=verified["content_source_policy"],
                content_source_policies=verified["content_source_policies"],
                retry_route_models={item["provider"]: item["model"] for item in verified["route_plan"]},
            )
            merge(primary)
        except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
            blocked_cohorts.append(key)
    for proof in receipts.values():
        prior = proof["prior_native_cohort"]
        root = root_for(prior["state"]) if prior is not None else ("external", proof["exchange"])
        if root == anchor:
            try:
                snapshot = artifact(proof["files"]["manifest"]).parent
                ready = _exchange_success_candidates(work_dir, snapshot, write_outputs=False)
                merge(replace(inventory[target], metadata={
                    **inventory[target].metadata, CANDIDATE_METADATA_KEY: candidate,
                }) for target, candidate in ready.items())
            except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
                blocked_bundles.append(proof["exchange"])
    joined = []
    for path, manifest, source in nodes:
        state = manifest["cohort_lineage"]["state"]
        if state not in parents or root_for(state) != anchor:
            blocked_followups.append(_relative(work_dir, path))
            continue
        try:
            ready, blocked, _ = _collect_followup_candidates(work_dir, path, source, frozen_bundles=snapshots)
            merge(ready.values())
            blocked_bundles.extend(blocked)
            joined.append(_relative(work_dir, path))
        except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
            blocked_followups.append(_relative(work_dir, path))
    for key, (cohort, _, source) in cohorts.items():
        if root_for(cohort["state"]) != anchor or all(item.id in candidates for item in source):
            continue
        try:
            successes = load_native_translation_retry_cohort_successes(work_dir, cohort, segments=source)
            merge(_candidate_segment(item, successes[item.id]) for item in source if item.id in successes)
        except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
            blocked_cohorts.append(key)
    result = _write_followup_collection(
        work_dir, segments, candidates, review_output, unresolved_output, blocked_bundles,
        protected_files=protected_files, protected_dirs=protected_dirs,
    )
    result.update(
        joined_followups=joined,
        lineage_receipts=list(receipts),
        blocked_followups=list(dict.fromkeys(blocked_followups)),
        blocked_cohorts=list(dict.fromkeys(blocked_cohorts)),
        source_fingerprint=inventory_fingerprint,
        conflicting_targets=sorted(conflicting_targets),
    )
    if blocked_followups or blocked_cohorts:
        result["issue_codes"].append("E_NATIVE_FOLLOWUP_CHAIN_COHORT_REPAIR_REQUIRED")
    if conflicting_targets:
        result["issue_codes"].append("E_NATIVE_FOLLOWUP_CHAIN_CANDIDATE_CONFLICT")
    return result


def _freeze_collection_inventory(
    work_dir: Path, followup_dir: Path, segments: list[Segment],
) -> tuple[Path, list[Segment]]:
    """Materialize a bounded, deterministic input without exposing its rows."""
    from .formats import read_segments
    path = followup_dir / "chain-input.jsonl"
    _relative(work_dir, path)
    rows = [asdict(item) for item in segments]
    if path.exists():
        if _read_jsonl(path) != rows:
            raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_INVENTORY_CHANGED")
    else:
        _atomic_jsonl(path, rows)
    return path, read_segments(path)


def _followup_collection_request(
    work_dir: Path, followup_dir: Path, cohort: dict[str, Any] | None,
    *, standalone: list[Segment] | None = None,
) -> dict[str, Any]:
    """Issue the whole-chain command, including external-first inventories."""
    from .formats import read_segments
    from .native_pass import _manifest_task_segments
    from .native_translation_retry import (
        verify_native_translation_retry_cohort,
        load_native_translation_retry_cohort_root_source,
    )
    from .translation_exchange import verify_translation_predecessor, _request_segment

    def artifact(value: str) -> Path:
        path = (work_dir / value).resolve()
        _relative(work_dir, path)
        return path

    seen = set()
    try:
        while cohort is not None:
            key = cohort["contract_fingerprint"]
            if key in seen:
                raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_LINEAGE_CONFLICT")
            seen.add(key)
            verified = verify_native_translation_retry_cohort(
                work_dir, cohort, source_path=artifact(cohort["source_artifact"]),
                results_path=artifact(cohort["results_artifact"]),
            )
            lineage = verified["primary_lineage"] or {}
            binding = lineage.get("translation_predecessor")
            if binding is not None:
                proof = verify_translation_predecessor(work_dir, artifact(binding["receipt"]))
                if any(proof[field] != binding[field] for field in ("receipt_sha256", "contract_fingerprint")):
                    raise NativeFollowupError("E_TRANSLATION_PREDECESSOR_INVALID")
                if proof["prior_native_cohort"] is not None:
                    cohort = proof["prior_native_cohort"]
                    continue
                original = [_request_segment(row) for row in _read_jsonl(artifact(proof["files"]["requests"]))]
            elif cohort["source_fingerprint"] == cohort["root_source_fingerprint"]:
                original = read_segments(artifact(cohort["source_artifact"]))
            elif isinstance(cohort.get("root_source_artifact"), str):
                original = load_native_translation_retry_cohort_root_source(work_dir, cohort)
            elif isinstance(lineage.get("manifest"), str):
                manifest_path = artifact(lineage["manifest"])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                try:
                    original = read_segments(artifact(manifest["source_artifact"]))
                    if stable_hash_sequence(asdict(item) for item in original) != cohort["root_source_fingerprint"]:
                        raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_ROOT_SOURCE_MISMATCH")
                except (ValueError, OSError):
                    original = _manifest_task_segments(manifest_path, manifest)
            else:
                raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_ROOT_SOURCE_REQUIRED")
            if binding is None and stable_hash_sequence(asdict(item) for item in original) != cohort["root_source_fingerprint"]:
                raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_ROOT_SOURCE_MISMATCH")
            break
        if cohort is None:
            if standalone is None:
                raise NativeFollowupError("E_NATIVE_FOLLOWUP_CHAIN_ROOT_SOURCE_REQUIRED")
            original = standalone
        input_path, original = _freeze_collection_inventory(work_dir, followup_dir, original)
    except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
        return {
            "next_action": "repair_chain_inventory",
            "issue_code": "E_NATIVE_FOLLOWUP_CHAIN_INVENTORY_REPAIR_REQUIRED",
            "followup": _relative(work_dir, followup_dir),
        }
    relative = _relative(work_dir, followup_dir)
    return {
        "next_action": "collect_native_followup_chain",
        "command": "collect-native-followup",
        "argv": [
            "collect-native-followup", "--workspace", str(work_dir),
            "--collect-chain", "--input", _relative(work_dir, input_path),
            "--followup", relative,
            "--review-output", _relative(work_dir, followup_dir / REVIEW_INPUT_NAME),
            "--unresolved-output", _relative(work_dir, followup_dir / UNRESOLVED_NAME),
        ],
        "input": _relative(work_dir, input_path),
        "followup": relative,
        "source_fingerprint": stable_hash_sequence(asdict(item) for item in original),
        "successor_followup_flag": "--successor-followup",
    }
