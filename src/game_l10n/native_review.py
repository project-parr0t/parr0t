"""Deterministic joins between native verdicts and existing candidate segments.

This module does no semantic work. It uses the existing Segment,
TranslationResult and native-pass formats, and never infers an approval from
file existence, transport success, or a missing judge response.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from .control_plane import require_control_plane_ready
from .coverage import require_inventory_attestation
from .formats import read_segments, read_translation_results, write_results
from .models import (
    AgentStatus,
    AttemptRecord,
    FailureCode,
    ReviewVerdict,
    Segment,
    TranslationResult,
)
from .native_followup import CANDIDATE_METADATA_KEY, _atomic_json, _atomic_jsonl, _read_jsonl
from .native_pass import _load_task_manifest, _require_candidate_bindings, merge_native_pass
from .orchestrator import LocalizationOrchestrator
from .providers import _parse_plain_review_verdict
from .review_selection import (
    DEFAULT_STRONG_REVIEW_CATEGORIES, provenance_review_reasons, review_category,
    rewrite_issue_codes, strong_review_selection_reasons, target_language_review_reasons,
)
from .store import stable_hash
from .validators import validate_integrity

REVIEW_STATE_KEY = "_parr0t_review_state"


def collect_reviewed_candidates(
    work_dir: Path, source: Path, result_paths: list[Path], destination: Path,
    *, glossary: list[Any] | None = None, extra_patterns: list[str] | None = None,
    max_length_ratio: float = 4.0, target_language: str = "",
) -> dict[str, Any]:
    """Join disjoint approved results in the original order, without reviewing again."""
    require_control_plane_ready(work_dir)
    work_dir = work_dir.resolve()
    for path in (source, destination, *result_paths):
        path.resolve().relative_to(work_dir)
    # The independent inventory remains authoritative after an old execution
    # contract has been superseded; do not regenerate any completed pass.
    require_inventory_attestation(work_dir, source)
    segments = read_segments(source)
    by_id = {s.id: s for s in segments}
    results = {}
    for path in result_paths:
        if destination.resolve() == path.resolve():
            raise ValueError("E_NATIVE_REVIEW_OUTPUT_INVALID")
        for result in read_translation_results(path):
            source_segment = by_id.get(result.segment_id)
            strong = result.metadata.get("review_tier") in {"strong", "strong_pending"}
            stages = {"review", "review-confirm", "review-grace-retry", "review-refusal-retry"}
            if not strong:
                stages |= {"review-triage", "review-triage-confirm", "review-triage-grace-retry",
                           "review-triage-refusal-retry"}
            approved = any(a.stage in stages and a.status == "approved" and
                           a.candidate_generation == result.candidate_generation
                           for a in result.attempts)
            if (source_segment is None or result.segment_id in results or
                    result.source != source_segment.source or result.status != "reviewed" or
                    result.acceptance_authority != "reviewer" or not approved or
                    result.candidate_digest != stable_hash(result.text) or
                    target_language_review_reasons(
                        result.text, target_language, source_segment.metadata
                    ) or
                    validate_integrity(source_segment, result.text, glossary or [],
                        extra_patterns=extra_patterns, max_length_ratio=max_length_ratio)):
                raise ValueError("E_NATIVE_REVIEW_ACCEPTED_RESULT_INVALID")
            results[result.segment_id] = result
    ordered = [results[s.id] for s in segments if s.id in results]
    # A new standard result artifact; no queue entries or history are reset.
    payload = {"results": [item.to_dict() for item in ordered]}
    if destination.exists():
        if json.loads(destination.read_text(encoding="utf-8-sig")) != payload:
            raise ValueError("E_NATIVE_REVIEW_OUTPUT_ALREADY_EXISTS")
    else:
        _atomic_json(destination, payload)
    return {"reviewed": len(ordered), "unresolved": len(segments)-len(ordered),
            "records": len(segments), "artifact": destination.relative_to(work_dir).as_posix()}


def materialize_delivery_candidates(
    work_dir: Path, source: Path, approved: Path, pending: list[Path],
    translation_failures: Path, output: Path, *, include_unreviewed: bool = False,
    report: Path | None = None, glossary: list[Any] | None = None,
    extra_patterns: list[str] | None = None, max_length_ratio: float = 4.0,
    target_language: str = "",
) -> dict[str, Any]:
    """Materialize explicit delivery decisions without approving candidates.

    Original review results, failure queues, generations and attempts are never
    changed. Pending candidates are included only with explicit authorization;
    exhausted or deterministically unsafe targets preserve their exact source.
    """
    require_control_plane_ready(work_dir)
    work_dir = work_dir.resolve()
    paths = [source, approved, translation_failures, output, *pending]
    if report is not None:
        paths.append(report)
    for path in paths:
        path.resolve().relative_to(work_dir)
    if output.resolve() in {
        p.resolve() for p in (source, approved, translation_failures, *pending)
    }:
        raise ValueError("E_NATIVE_REVIEW_OUTPUT_INVALID")
    if report is not None and (
        report.resolve() in {path.resolve() for path in paths if path != report}
    ):
        raise ValueError("E_DELIVERY_REPORT_INVALID")
    require_inventory_attestation(work_dir, source)
    segments = read_segments(source)
    by_id = {s.id: s for s in segments}
    results = {}
    pending_results: dict[str, TranslationResult] = {}
    for result in read_translation_results(approved):
        original = by_id.get(result.segment_id)
        if (original is None or result.segment_id in results or result.source != original.source
                or result.status != "reviewed" or result.acceptance_authority != "reviewer"
                or result.candidate_digest != stable_hash(result.text)
                or not any(a.stage in {"review", "review-confirm", "review-triage"}
                           and a.status == "approved"
                           and a.candidate_generation == result.candidate_generation
                           for a in result.attempts)):
            raise ValueError("E_NATIVE_REVIEW_ACCEPTED_RESULT_INVALID")
        results[result.segment_id] = result
    for path in pending:
        candidates = read_segments(path)
        _require_candidate_bindings(candidates, "rewrite")
        for segment in candidates:
            original = by_id.get(segment.id)
            if (
                original is None
                or segment.id in results
                or segment.id in pending_results
                or segment.source != original.source
            ):
                raise ValueError("E_DELIVERY_CANDIDATE_SET_INVALID")
            result = _bound_result(segment)
            if not result.text.strip() or result.candidate_digest != stable_hash(result.text):
                raise ValueError("E_NATIVE_REVIEW_CANDIDATE_STALE")
            result.status = "accepted_unreviewed"
            result.acceptance_authority = "user"
            result.review_outcome = "unresolved"
            result.metadata["delivery_review_status"] = "not_approved"
            result.metadata["delivery_inclusion_authority"] = "user"
            pending_results[segment.id] = result
    if include_unreviewed:
        results.update(pending_results)
    covered_ids = set(results) | set(pending_results)
    failures = read_segments(translation_failures)
    failure_ids = {s.id for s in failures}
    if (len(failure_ids) != len(failures)
            or failure_ids != set(by_id) - covered_ids
            or any(s.source != by_id[s.id].source for s in failures)):
        raise ValueError("E_DELIVERY_TRANSLATION_QUEUE_MISMATCH")

    safe_results: dict[str, TranslationResult] = {}
    integrity_rejections: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}
    for segment in segments:
        result = results.get(segment.id)
        if result is None:
            continue
        issues = validate_integrity(
            segment,
            result.text,
            glossary or [],
            extra_patterns=extra_patterns,
            max_length_ratio=max_length_ratio,
        )
        language_issues = target_language_review_reasons(
            result.text, target_language, segment.metadata
        )
        if not issues and not language_issues:
            safe_results[segment.id] = result
            continue
        codes = list(
            dict.fromkeys(
                [issue.code for issue in issues] + language_issues
            )
        )
        integrity_rejections.append({"target_id": segment.id, "issue_codes": codes})
        for code in codes:
            issue_counts[code] = issue_counts.get(code, 0) + 1
        preserved = replace(result)
        preserved.text = segment.source
        preserved.status = "source_preserved"
        preserved.candidate_digest = stable_hash(segment.source)
        preserved.review_outcome = "unresolved"
        preserved.acceptance_authority = "integrity_gate"
        preserved.resolution_code = FailureCode.INTEGRITY_INVALID.value
        preserved.metadata = {
            **result.metadata,
            "delivery_review_status": "source_preserved",
            "integrity_issue_codes": codes,
        }
        preserved.attempts = [
            *result.attempts,
            AttemptRecord(
                stage="delivery-integrity",
                provider="core",
                status="rejected",
                issues=codes,
                issue_code=FailureCode.INTEGRITY_INVALID.value,
                candidate_generation=result.candidate_generation,
            ),
        ]
        safe_results[segment.id] = preserved

    for segment in failures:
        safe_results[segment.id] = TranslationResult(
            segment_id=segment.id,
            source=segment.source,
            text=segment.source,
            status="source_preserved",
            candidate_digest=stable_hash(segment.source),
            review_outcome="unresolved",
            acceptance_authority="route_exhaustion",
            resolution_code=FailureCode.SOURCE_PRESERVED.value,
            metadata={
                **{
                    key: value
                    for key, value in segment.metadata.items()
                    if key not in {CANDIDATE_METADATA_KEY, REVIEW_STATE_KEY}
                },
                "delivery_review_status": "source_preserved",
            },
        )

    ordered = [safe_results[s.id] for s in segments if s.id in safe_results]
    reviewed_included = sum(result.status == "reviewed" for result in ordered)
    report_payload = {
        "schema": "parr0t.delivery-selection.v1",
        "records": len(segments),
        "included": len(ordered),
        "reviewed": reviewed_included,
        "included_unreviewed": sum(
            result.status == "accepted_unreviewed" for result in ordered
        ),
        "translation_failed": len(failures),
        "integrity_rejected": len(integrity_rejections),
        "source_preserved_total": sum(
            result.status == "source_preserved" for result in ordered
        ),
        "integrity_issue_counts": dict(sorted(issue_counts.items())),
        "integrity_rejections": integrity_rejections,
    }
    output_payload = {"results": [item.to_dict() for item in ordered]}
    cached = output.exists()
    if cached:
        if json.loads(output.read_text(encoding="utf-8-sig")) != output_payload:
            raise ValueError("E_DELIVERY_OUTPUT_ALREADY_EXISTS")
    if report is not None and report.exists():
        if json.loads(report.read_text(encoding="utf-8-sig")) != report_payload:
            raise ValueError("E_DELIVERY_REPORT_ALREADY_EXISTS")
    if not cached:
        write_results(output, ordered)
    if report is not None and not report.exists():
        _atomic_json(report, report_payload)
    return {
        **{key: value for key, value in report_payload.items() if key != "integrity_rejections"},
        "cached": cached,
        "artifact": output.relative_to(work_dir).as_posix(),
        "report": report.relative_to(work_dir).as_posix() if report is not None else None,
    }


def _compact_verdict(text: str) -> Any:
    stripped = text.strip()
    raw = json.loads(stripped) if stripped.startswith("{") else _parse_plain_review_verdict(text)
    if not isinstance(raw, dict) or set(raw) - {"verdict", "issues"}:
        raise ValueError("E_NATIVE_REVIEW_VERDICT_INVALID")
    return LocalizationOrchestrator._review_verdict_envelope(raw)


def _bound_result(segment: Segment) -> TranslationResult:
    candidate = segment.metadata[CANDIDATE_METADATA_KEY]
    result = TranslationResult(
        segment_id=segment.id, source=segment.source, text=candidate["text"],
        translator=candidate["provider"], candidate_generation=candidate["generation"],
        candidate_digest=candidate["digest"],
        metadata={key: value for key, value in segment.metadata.items()
                  if key not in {CANDIDATE_METADATA_KEY, REVIEW_STATE_KEY}},
    )
    saved = segment.metadata.get(REVIEW_STATE_KEY)
    if saved is not None:
        if not isinstance(saved, dict):
            raise ValueError("E_NATIVE_REVIEW_STATE_INVALID")
        if (saved.get("candidate_digest") != candidate["digest"]
                or saved.get("candidate_generation") != candidate["generation"]):
            raise ValueError("E_NATIVE_REVIEW_STATE_STALE")
        result.attempts = [AttemptRecord(**item) for item in saved.get("attempts", [])]
        result.metadata.update(saved.get("metadata", {}))
        result.errors = list(saved.get("errors", []))
    return result


def _pending_segment(segment: Segment, result: TranslationResult) -> Segment:
    # Carry only review history and categorical selection, never another copy
    # of source, candidate, or context inside metadata.
    state = {
        "candidate_generation": result.candidate_generation,
        "candidate_digest": result.candidate_digest,
        "attempts": [asdict(item) for item in result.attempts],
        "errors": result.errors,
        "metadata": {key: result.metadata[key] for key in (
            "review_tier", "review_category", "strong_review_reasons", "rewrite_issues",
            "target_language_risk",
        ) if key in result.metadata},
    }
    return replace(segment, metadata={**segment.metadata, REVIEW_STATE_KEY: state})


def select_native_review_route(segments: list[Segment], routes: list[str], *, stage: str) -> str:
    """Select the next ordered role route from current-candidate attempt evidence.

    Callers pass only a collector's pending artifact, never its accepted rows.
    An external next route is not silently skipped to reach a native provider.
    """
    selected = set()
    for segment in segments:
        result = _bound_result(segment)
        attempted = {item.provider for item in result.attempts
                     if item.stage == stage and item.candidate_generation == result.candidate_generation}
        active_routes = routes
        if stage == "review-rewrite" and not active_routes:
            active_routes = [next((item.provider for item in reversed(result.attempts)
                                  if item.stage == "review" and
                                  item.candidate_generation == result.candidate_generation), "")]
        selected.add(next((route for route in active_routes if route not in attempted), ""))
    if len(selected) != 1:
        raise ValueError("E_NATIVE_ROUTE_INVALID: pending targets require different role routes")
    provider = selected.pop()
    if not provider.startswith("native-"):
        raise ValueError("E_NATIVE_ROUTE_INVALID: next role route is external or exhausted")
    return provider


def collect_native_review(
    work_dir: Path, source: Path, manifest_path: Path, output: Path, *, tier: str,
    sample_percent: float = 5.0,
    high_risk_categories: list[str] | tuple[str, ...] = DEFAULT_STRONG_REVIEW_CATEGORIES,
    target_language: str = "",
    glossary: list[Any] | None = None, extra_patterns: list[str] | None = None,
    max_length_ratio: float = 4.0,
    reference_cues: tuple[dict[str, Any], ...] = (),
    primary_translator: str = "",
) -> dict[str, Any]:
    """Collect a complete bound pass; preserve accepted rows and route only gaps."""
    require_control_plane_ready(work_dir)
    if tier not in {"triage", "strong"}:
        raise ValueError("E_NATIVE_REVIEW_TIER_INVALID")
    work_dir = work_dir.resolve()
    for path in (source, manifest_path, output):
        path.resolve().relative_to(work_dir)
    segments = read_segments(source)
    _require_candidate_bindings(segments, "review")
    manifest = _load_task_manifest(work_dir, manifest_path)
    if manifest.get("pass_kind") != "review":
        raise ValueError("E_NATIVE_REVIEW_PASS_REQUIRED")
    # No overwrite of any prepared input or prior result. Repeated collection
    # is deterministic and never starts a worker or consumes another attempt.
    if output.resolve() in {source.resolve().parent, manifest_path.resolve().parent}:
        raise ValueError("E_NATIVE_REVIEW_OUTPUT_INVALID")
    output.mkdir(parents=True, exist_ok=True)
    merged = output / "native-verdicts.jsonl"
    summary = merge_native_pass(work_dir, segments, merged, manifest_path=manifest_path)
    if not summary["complete"]:
        raise ValueError("E_NATIVE_REVIEW_PASS_INCOMPLETE")
    rows = _read_jsonl(merged)
    accepted: list[TranslationResult] = []
    strong: list[Segment] = []
    rewrite: list[Segment] = []
    fallback: list[Segment] = []
    invalid_verdicts = 0
    verdict_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    for segment, row in zip(segments, rows, strict=True):
        result = _bound_result(segment)
        if result.candidate_digest != stable_hash(result.text):
            raise ValueError("E_NATIVE_REVIEW_CANDIDATE_STALE")
        provider = (manifest.get("route_binding") or {}).get("provider") or (
            "native-luna" if tier == "triage" else "native-sol")
        stage = "review-triage" if tier == "triage" else "review"
        verdict = None
        if row["status"] == "success":
            try:
                verdict = _compact_verdict(row["text"])
            except (ValueError, TypeError):
                invalid_verdicts += 1
            except Exception:
                # Protocol errors remain private and pending, never approvals.
                invalid_verdicts += 1
        status = ("approved" if verdict and verdict.verdict is ReviewVerdict.OK
                  else verdict.verdict.value if verdict else "error")
        issues = list(verdict.issues) if verdict else []
        verdict_counts[status] += 1
        issue_counts.update(dict.fromkeys(issues, 1))
        result.attempts.append(AttemptRecord(
            stage, provider, status, issues=issues,
            ordinal=1 + sum(item.stage == stage and item.provider == provider
                            for item in result.attempts),
            candidate_generation=result.candidate_generation,
        ))
        integrity = validate_integrity(segment, result.text, glossary or [],
            extra_patterns=extra_patterns, max_length_ratio=max_length_ratio)
        if integrity:
            result.attempts.append(AttemptRecord(
                "integrity", "deterministic-validator", "rejected",
                issues=[item.code for item in integrity],
                candidate_generation=result.candidate_generation,
            ))
        result.metadata["review_category"] = review_category(segment)
        result.status = "failed"
        if tier == "triage":
            reasons = strong_review_selection_reasons(
                segment, sample_percent=sample_percent, high_risk_categories=high_risk_categories,
            )
            origin = segment.metadata.get(CANDIDATE_METADATA_KEY, {}).get("provider", "")
            reasons.extend(provenance_review_reasons(
                segment.id, reference_cues,
                non_primary_translation=bool(primary_translator)
                and origin not in {"", "native-worker", primary_translator},
            ))
            language_reasons = target_language_review_reasons(
                result.text, target_language, segment.metadata
            )
            if language_reasons:
                result.metadata["target_language_risk"] = language_reasons
                reasons.extend(language_reasons)
            if verdict is None or verdict.verdict in {ReviewVerdict.ERROR, ReviewVerdict.REFUSED}:
                reasons.insert(0, "triage_unresolved")
            elif verdict.verdict is ReviewVerdict.REWRITE:
                reasons.insert(0, "triage_flagged")
            if integrity:
                reasons.insert(0, "integrity_invalid")
            if reasons:
                result.metadata["review_tier"] = "strong_pending"
                result.metadata["strong_review_reasons"] = list(dict.fromkeys(reasons))
                strong.append(_pending_segment(segment, result))
                continue
        elif verdict is None or verdict.verdict in {ReviewVerdict.ERROR, ReviewVerdict.REFUSED}:
            result.metadata["review_tier"] = "strong_pending"
            fallback.append(_pending_segment(segment, result))
            continue
        language_reasons = target_language_review_reasons(
            result.text, target_language, segment.metadata
        )
        if language_reasons:
            result.metadata["target_language_risk"] = language_reasons
        if tier == "strong" and language_reasons:
            result.metadata["review_tier"] = "strong_pending"
            result.metadata["rewrite_issues"] = rewrite_issue_codes(
                issues, integrity, language_risk=True
            )
            rewrite.append(_pending_segment(segment, result))
            continue
        if verdict.verdict is ReviewVerdict.REWRITE or integrity:
            result.metadata["review_tier"] = "strong_pending"
            result.metadata["rewrite_issues"] = rewrite_issue_codes(issues, integrity)
            rewrite.append(_pending_segment(segment, result))
            continue
        # Only a parsed current-candidate OK and clean integrity can approve.
        if verdict is None or verdict.verdict is not ReviewVerdict.OK or integrity:
            raise ValueError("E_NATIVE_REVIEW_APPROVAL_INVALID")
        result.status = "reviewed"
        result.reviewer = provider
        result.review_outcome = "approved"
        result.acceptance_authority = "reviewer"
        result.metadata["review_tier"] = tier
        accepted.append(result)
    write_results(output / "accepted.json", accepted)
    for name, items in (("strong-input.jsonl", strong), ("rewrite-input.jsonl", rewrite),
                        ("fallback-input.jsonl", fallback)):
        _atomic_jsonl(output / name, (asdict(item) for item in items))
    # Observational drift signal for batch-size effects on judges (fewer issues per record
    # in large batches). Never a stop, scope-reduction or pass criterion; private data plane.
    _atomic_json(output / "review-metrics.json", {
        "schema": "parr0t.native-review-metrics.v1", "tier": tier, "records": len(segments),
        "task_max_records": manifest.get("task_max_records"),
        "task_max_material_tokens": manifest.get("task_max_material_tokens"),
        "verdicts": dict(sorted(verdict_counts.items())), "invalid_verdicts": invalid_verdicts,
        "issue_code_counts": dict(sorted(issue_counts.items())),
    })
    return {
        "records": len(segments), "accepted": len(accepted), "strong_pending": len(strong),
        "rewrite_pending": len(rewrite), "fallback_pending": len(fallback),
        "invalid_verdicts": invalid_verdicts, "output": output.relative_to(work_dir).as_posix(),
    }


def collect_native_rewrite(
    work_dir: Path, source: Path, manifest_path: Path, output: Path, *,
    glossary: list[Any] | None = None, extra_patterns: list[str] | None = None,
    max_length_ratio: float = 4.0, review_max_rounds: int = 2,
) -> dict[str, Any]:
    """Bind native corrections to their history, requiring a separate judge next.

    This collector starts no provider and grants no provider-role exception.
    It consumes the existing rewrite pass and Segment formats only.
    """
    require_control_plane_ready(work_dir)
    work_dir = work_dir.resolve()
    for path in (source, manifest_path, output):
        path.resolve().relative_to(work_dir)
    segments = read_segments(source)
    _require_candidate_bindings(segments, "rewrite")
    manifest = _load_task_manifest(work_dir, manifest_path)
    if manifest.get("pass_kind") != "rewrite":
        raise ValueError("E_NATIVE_REWRITE_PASS_REQUIRED")
    if output.resolve() in {source.resolve().parent, manifest_path.resolve().parent}:
        raise ValueError("E_NATIVE_REVIEW_OUTPUT_INVALID")
    initial = []
    for segment in segments:
        result = _bound_result(segment)
        if result.candidate_digest != stable_hash(result.text):
            raise ValueError("E_NATIVE_REVIEW_CANDIDATE_STALE")
        native = [a for a in result.attempts if a.stage == "review"
                  and a.candidate_generation == result.candidate_generation]
        if not native or not result.metadata.get("rewrite_issues"):
            raise ValueError("E_NATIVE_REVIEW_REWRITE_NOT_FLAGGED")
        if sum(a.stage.startswith("review-rewrite") and a.status == "success"
               for a in result.attempts) >= max(0, review_max_rounds - 1):
            raise ValueError("E_NATIVE_REWRITE_LIMIT_REACHED")
        initial.append(result)
    output.mkdir(parents=True, exist_ok=True)
    merged = output / "native-corrections.jsonl"
    summary = merge_native_pass(work_dir, segments, merged, manifest_path=manifest_path)
    if not summary["complete"]:
        raise ValueError("E_NATIVE_REWRITE_PASS_INCOMPLETE")
    rows = _read_jsonl(merged)
    provider = (manifest.get("route_binding") or {}).get("provider") or "native-sol"
    confirmation, unresolved, results = [], [], []
    for segment, result, row in zip(segments, initial, rows, strict=True):
        integrity = (validate_integrity(segment, row["text"], glossary or [],
                     extra_patterns=extra_patterns, max_length_ratio=max_length_ratio)
                     if row["status"] == "success" else [])
        valid = row["status"] == "success" and not integrity
        result.attempts.append(AttemptRecord(
            "review-rewrite", provider, "success" if valid else "error",
            issues=[item.code for item in integrity] if integrity else list(row.get("issues", [])),
            ordinal=1 + sum(a.stage == "review-rewrite" and a.provider == provider
                            for a in result.attempts),
            candidate_generation=result.candidate_generation,
        ))
        result.status = "failed"
        result.acceptance_authority = ""
        result.review_outcome = ""
        if valid:
            LocalizationOrchestrator._set_candidate(result, row["text"])
            result.text = row["text"]
            result.metadata["review_tier"] = "strong_pending"
            result.metadata["native_confirmation_pending"] = True
            bound = replace(segment, metadata={**segment.metadata, CANDIDATE_METADATA_KEY: {
                "text": result.text, "generation": result.candidate_generation,
                "digest": result.candidate_digest, "provider": provider,
            }})
            confirmation.append(_pending_segment(bound, result))
        else:
            unresolved.append(_pending_segment(segment, result))
        results.append(result)
    write_results(output / "results.json", results)
    for name, items in (("confirmation-input.jsonl", confirmation),
                        ("unresolved-input.jsonl", unresolved)):
        _atomic_jsonl(output / name, (asdict(item) for item in items))
    return {"records": len(segments), "confirmation_pending": len(confirmation),
            "unresolved": len(unresolved), "accepted": 0,
            "output": output.relative_to(work_dir).as_posix()}


def process_native_review_followup(
    work_dir: Path, source: Path, output: Path, orchestrator: Any, *, operation: str,
    deadline_epoch: float | None = None, wave_size: int = 8,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run only the locked external review role, with durable per-wave results.

    Provider requests, focused prompts, integrity checks, refusal handling and
    usage audit use the existing provider-neutral adapter. This cannot perform
    primary translation, native triage, or native strong review.
    """
    require_control_plane_ready(work_dir)
    if operation not in {"judge", "rewrite"} or not 1 <= wave_size <= 8:
        raise ValueError("E_NATIVE_REVIEW_FOLLOWUP_INVALID")
    work_dir = work_dir.resolve()
    source.resolve().relative_to(work_dir)
    output.resolve().relative_to(work_dir)
    segments = read_segments(source)
    _require_candidate_bindings(segments, "review")
    provider = orchestrator.registry.get("deepseek")
    prepared: list[tuple[Segment, TranslationResult, str]] = []
    for segment in segments:
        result = _bound_result(segment)
        native = [a for a in result.attempts
                  if a.provider == "native-sol" and a.stage == "review"
                  and a.candidate_generation == result.candidate_generation]
        if not native or result.status == "reviewed":
            raise ValueError("E_NATIVE_REVIEW_PRIMARY_BARRIER_REQUIRED")
        if operation == "rewrite" and not result.metadata.get("rewrite_issues"):
            raise ValueError("E_NATIVE_REVIEW_REWRITE_NOT_FLAGGED")
        if operation == "judge" and native[-1].status not in {"error", "refused"}:
            raise ValueError("E_NATIVE_REVIEW_FALLBACK_NOT_FAILED")
        fingerprint = stable_hash({
            "segment": asdict(segment), "operation": operation,
            "provider": provider.fingerprint(),
            "context": asdict(orchestrator.context_pack),
        })
        prepared.append((segment, result, fingerprint))
    output.mkdir(parents=True, exist_ok=True)
    results_path = output / "results.json"
    saved = read_translation_results(results_path) if results_path.is_file() else []
    cached = {item.segment_id: item for item in saved}
    if len(cached) != len(saved) or not set(cached) <= {s.id for s in segments}:
        raise ValueError("E_NATIVE_REVIEW_FOLLOWUP_CACHE_INVALID")
    for segment, _, fingerprint in prepared:
        prior = cached.get(segment.id)
        if prior and (prior.source != segment.source or
                      prior.metadata.get("native_review_request_fingerprint") != fingerprint):
            raise ValueError("E_NATIVE_REVIEW_FOLLOWUP_CACHE_STALE")

    def run(item: tuple[Segment, TranslationResult, str]) -> TranslationResult:
        segment, result, fingerprint = item
        # Do not retransmit internal review history or duplicate the candidate
        # inside metadata. The adapter receives one focused target request.
        clean = replace(segment, metadata={k: v for k, v in segment.metadata.items()
                        if k not in {CANDIDATE_METADATA_KEY, REVIEW_STATE_KEY}})
        result.metadata["native_review_request_fingerprint"] = fingerprint
        result.status = "failed"
        if operation == "judge":
            verdict = orchestrator._judge_with_refusal_retry(
                result, provider, clean, result.text, [],
            )
            integrity = validate_integrity(clean, result.text, orchestrator.glossary,
                extra_patterns=orchestrator.config.extra_protected_patterns,
                max_length_ratio=orchestrator.config.max_length_ratio)
            language_reasons = target_language_review_reasons(
                result.text, orchestrator.project.target_language, segment.metadata
            )
            if language_reasons:
                result.metadata["target_language_risk"] = language_reasons
            if verdict and verdict.verdict is ReviewVerdict.OK and not integrity and not language_reasons:
                result.status = "reviewed"
                result.reviewer = "deepseek"
                result.review_outcome = "approved"
                result.acceptance_authority = "reviewer"
                result.metadata["review_tier"] = "strong"
            elif verdict and verdict.verdict in {ReviewVerdict.OK, ReviewVerdict.REWRITE}:
                # Approval cannot waive integrity or the locked target language.
                result.metadata["rewrite_issues"] = rewrite_issue_codes(
                    verdict.issues, integrity, language_risk=bool(language_reasons)
                )
        else:
            rewrites_done = sum(a.stage.startswith("review-rewrite") and a.status == "success"
                                for a in result.attempts)
            if rewrites_done >= max(0, orchestrator.config.review_max_rounds - 1):
                result.metadata["native_rewrite_limit_reached"] = True
                return result
            response = orchestrator._rewrite_with_refusal_retry(
                result, provider, clean, result.text, result.metadata["rewrite_issues"], [],
            )
            if response and response.status is AgentStatus.SUCCESS:
                orchestrator._set_candidate(result, response.text)
                result.text = response.text
                result.metadata["review_tier"] = "strong_pending"
                result.metadata["native_confirmation_pending"] = True
        return result

    pending = [item for item in prepared if item[0].id not in cached]
    executed = 0
    if progress_callback is not None:
        try:
            progress_callback(
                {
                    "stage": "review_fallback",
                    "completed_records": len(saved),
                    "review_completed": sum(
                        item.status == "reviewed" for item in saved
                    ),
                    "fallback_pending": len(pending),
                    "attempts": 0,
                }
            )
        except (TypeError, ValueError, OSError):
            pass
    for offset in range(0, len(pending), wave_size):
        # Only a user-requested ceiling binds a deadline (AGENTS rule 16).
        if deadline_epoch is not None and time.time() >= deadline_epoch:
            break
        chunk = pending[offset:offset + wave_size]
        with ThreadPoolExecutor(max_workers=wave_size) as pool:
            completed = list(pool.map(run, chunk))
        for result in completed:
            cached[result.segment_id] = result
        executed += len(completed)
        _atomic_json(results_path, {"results": [cached[s.id].to_dict() for s in segments
                                                if s.id in cached]})
        if progress_callback is not None:
            try:
                progress_callback(
                    {
                        "stage": "review_fallback",
                        "completed_records": len(saved) + executed,
                        "review_completed": sum(
                            item.status == "reviewed" for item in cached.values()
                        ),
                        "fallback_pending": max(0, len(pending) - executed),
                        "failed_records": sum(
                            item.status == "failed" for item in cached.values()
                        ),
                        "attempts": executed,
                    }
                )
            except (TypeError, ValueError, OSError):
                pass
    accepted, rewrite, confirmation, unresolved = [], [], [], []
    for segment, initial, _ in prepared:
        result = cached.get(segment.id)
        if result is None:
            unresolved.append(segment)
            continue
        if result.status == "reviewed" and target_language_review_reasons(
            result.text, orchestrator.project.target_language, segment.metadata
        ):
            # A cached approval from before the language gate must not reach collection.
            result.status = "failed"
            result.metadata["rewrite_issues"] = rewrite_issue_codes(language_risk=True)
        if result.status == "reviewed":
            accepted.append(result)
            continue
        if result.metadata.get("native_confirmation_pending") is True:
            metadata = dict(segment.metadata)
            metadata[CANDIDATE_METADATA_KEY] = {
                "text": result.text, "generation": result.candidate_generation,
                "digest": result.candidate_digest, "provider": "deepseek",
            }
            confirmation.append(_pending_segment(replace(segment, metadata=metadata), result))
        elif operation == "judge" and result.metadata.get("rewrite_issues"):
            rewrite.append(_pending_segment(segment, result))
        else:
            unresolved.append(_pending_segment(segment, result))
    write_results(output / "accepted.json", accepted)
    for name, items in (("rewrite-input.jsonl", rewrite),
                        ("confirmation-input.jsonl", confirmation),
                        ("unresolved-input.jsonl", unresolved)):
        _atomic_jsonl(output / name, (asdict(item) for item in items))
    return {"records": len(segments), "executed": executed, "cached": len(saved),
            "accepted": len(accepted), "rewrite_pending": len(rewrite),
            "confirmation_pending": len(confirmation), "unresolved": len(unresolved),
            "deadline_pending": len(pending) - executed,
            "output": output.relative_to(work_dir).as_posix()}
