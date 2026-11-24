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
from .errors import ConfigurationError
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
    DEFAULT_STRONG_REVIEW_CATEGORIES, dialogue_review_reasons,
    provenance_review_reasons, review_category, rewrite_issue_codes,
    strong_review_selection_reasons, target_language_review_reasons,
)
from .review_policy import JUDGE_STAGES, judge_calls, review_call_limit, review_worker_limit
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


def _resume_review_queues(
    work_dir: Path, source: Path, approved: Path, queues: list[Path], output: Path,
) -> dict[str, Any]:
    """Recover unambiguous evidence and coalesce ready targets on their next exact route."""
    require_control_plane_ready(work_dir)
    work_dir = work_dir.resolve()
    output = output.resolve()
    for path in (source, approved, output, *queues):
        path.resolve().relative_to(work_dir)
    require_inventory_attestation(work_dir, source)
    remaining = {segment.id: segment.source for segment in read_segments(source)}
    for result in read_translation_results(approved):
        if (remaining.get(result.segment_id) != result.source or result.status != "reviewed"
                or result.acceptance_authority != "reviewer"
                or result.candidate_digest != stable_hash(result.text)):
            raise ValueError("E_NATIVE_REVIEW_ACCEPTED_RESULT_INVALID")
        del remaining[result.segment_id]

    queue_kinds = {
        "strong-input.jsonl": "strong", "rewrite-input.jsonl": "rewrite",
        "confirmation-input.jsonl": "confirmation", "fallback-input.jsonl": "fallback",
        "unresolved-input.jsonl": "rewrite_retry", "awaiting-input.jsonl": "awaiting",
    }
    combined = {"judge", "review-rewrite"}
    queue_kinds.update({kind + ".jsonl": kind for kind in (*queue_kinds.values(), *combined)})
    rewrite_kinds = {"rewrite", "rewrite_retry", "review-rewrite"}

    def operation_for(kind: str) -> str:
        return "rewrite" if kind in rewrite_kinds else "judge"

    def supersedes(a: TranslationResult, b: TranslationResult) -> bool:
        return (len(a.attempts) > len(b.attempts)
                and a.attempts[:len(b.attempts)] == b.attempts
                and a.candidate_generation >= b.candidate_generation
                and (a.candidate_generation != b.candidate_generation
                     or a.candidate_digest == b.candidate_digest))

    latest: dict[str, list[tuple[str, Segment, TranslationResult]]] = {}
    for path in queues:
        kind = queue_kinds.get(path.name)
        if kind is None or output.resolve() == path.resolve().parent:
            raise ValueError("E_NATIVE_REVIEW_QUEUE_INVALID")
        for segment in read_segments(path):
            if segment.id not in remaining:
                continue
            if (segment.source != remaining[segment.id]
                    or (kind != "awaiting" and REVIEW_STATE_KEY not in segment.metadata)):
                raise ValueError("E_NATIVE_REVIEW_QUEUE_INVALID")
            _require_candidate_bindings([segment], "rewrite")
            result = _bound_result(segment)
            states = latest.setdefault(segment.id, [])

            if any(supersedes(previous, result) for _, _, previous in states):
                continue
            states[:] = [(stage, item, previous) for stage, item, previous in states
                         if not supersedes(result, previous)]
            operation = operation_for(kind)
            if kind in combined:
                if any(item == segment and stage != "awaiting" and operation_for(stage) == operation
                       for stage, item, _ in states):
                    continue
            elif kind != "awaiting":
                states[:] = [(stage, item, previous) for stage, item, previous in states
                             if not (stage in combined and item == segment and operation_for(stage) == operation)]
            if not any(stage == kind and item == segment for stage, item, _ in states):
                states.append((kind, segment, result))

    selected: list[tuple[str, Segment, TranslationResult]] = []
    conflicting, missing = 0, 0
    for target in remaining:
        states = latest.get(target, [])
        if not states:
            missing += 1
            continue
        ready = [entry for entry in states if entry[0] != "awaiting"]
        if (len(ready) == 1 and all(s == ready[0][1] for _, s, _ in states)
                or len(states) == 1):
            kind, segment, result = (ready or states)[0]
            # An identical awaiting snapshot is already attached to a pass;
            # only newer evidence may supersede that attachment.
            selected.append(("awaiting" if len(ready) != len(states) else kind, segment, result))
        else:
            conflicting += 1

    config_path = work_dir / "game-l10n.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    routing = config.get("routing", {})
    routes = {"judge": routing.get("reviewers"), "rewrite": routing.get("rewriters")}
    adapters = {item["name"]: item for item in config.get("providers", [])}
    external_roles = config.get("native_workflow", {}).get("external_provider_roles")
    native_models, requested_models = {}, {}
    if any(routes.values()):
        from .model_policy import native_role_bindings
        from .native_runtime import native_runtime

        native_models = (native_runtime(work_dir) or {}).get("models", {})
        requested_models = native_role_bindings(config)
    route_models = {name: item["model"] for name, item in adapters.items()
                    if isinstance(item.get("model"), str) and item["model"]}
    route_models.update(native_models)
    max_rewrites = max(0, routing.get("review_max_rounds", 2) - 1)
    eligible: dict[str, list[Segment]] = {"judge": [], "rewrite": []}
    holds: dict[str, str] = {}
    for kind, segment, result in selected:
        if kind == "awaiting":
            continue
        operation = operation_for(kind)
        if routes[operation] is None:
            continue  # Keep legacy queues usable when no route policy was supplied.
        if not routes[operation]:
            holds[segment.id] = "routes_exhausted"
            continue
        if operation == "rewrite":
            if sum(a.stage.startswith("review-rewrite") and a.status == "success"
                   for a in result.attempts) >= max_rewrites:
                holds[segment.id] = "routes_exhausted"
                continue
            if not result.metadata.get("rewrite_issues"):
                holds[segment.id] = "repair_required"
                continue
        eligible[operation].append(segment)

    next_routes = {
        segment.id: provider
        for operation, segments in eligible.items() if segments
        for provider, items in partition_native_review_routes(
            segments, routes[operation], route_models=route_models,
            stage="review" if operation == "judge" else "review-rewrite",
            attempt_limits=routing.get("review_attempt_limits", {}),
            tier_limit=routing.get("strong_max_calls") if operation == "judge" else None,
        ).items()
        for segment in items
    }
    groups: dict[str, tuple[Path, dict[str, str], list[Segment]]] = {}
    for kind, segment, _ in selected:
        key, path, details = kind, Path(kind + ".jsonl"), {}
        if kind == "awaiting":
            details = {"status": "awaiting"}
        else:
            operation = operation_for(kind)
            if routes[operation] is not None:
                status = holds.get(segment.id)
                provider = next_routes.get(segment.id, "")
                model = route_models.get(provider, "")
                if status is None:
                    if not provider:
                        status = "routes_exhausted"
                    elif provider.startswith("native-"):
                        status = ("native_route_pending" if provider in native_models
                                  and requested_models.get(provider, model) == model else "repair_required")
                    else:
                        role = "selective_rewrite" if operation == "rewrite" else "judge_fallback"
                        adapter = adapters.get(provider, {})
                        authorized = ((external_roles is None or role in external_roles)
                                      and ("allowed_roles" not in adapter or role in adapter["allowed_roles"]))
                        status = "external_route_required" if model and authorized else "repair_required"
                details = {"operation": operation, "status": status}
                if status in {"native_route_pending", "external_route_required"}:
                    key = stable_hash([operation, provider, model])
                    path = Path(key) / ("review-rewrite.jsonl" if operation == "rewrite" else "judge.jsonl")
                    details.update(provider=provider, model=model)
                else:
                    key, path = f"{status}:{kind}", Path(status) / path
        groups.setdefault(key, (path, details, []))[2].append(segment)
    if output.exists():
        raise ValueError("E_NATIVE_REVIEW_OUTPUT_ALREADY_EXISTS")
    output.mkdir(parents=True)
    actions = {}
    for key, (relative, details, segments) in groups.items():
        # The segment reader fills empty context from neighbors. Split only
        # where new adjacency would change a recovered row on the next read.
        starts = [0]
        for index in range(1, len(segments)):
            previous, current = segments[index - 1], segments[index]
            if (not previous.next and current.source) or (not current.previous and previous.source):
                starts.append(index)
        starts.append(len(segments))
        for start, end in zip(starts, starts[1:]):
            part = key if not start else f"{key}:{start}"
            path = output / relative if not start else output / relative.parent / str(start) / relative.name
            _atomic_jsonl(path, (asdict(segments[index]) for index in range(start, end)))
            actions[part] = {"count": end - start, "input": path.relative_to(work_dir).as_posix(), **details}
    return {"unreviewed": len(remaining), "missing": missing, "conflicting": conflicting, "queues": actions}


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
            result.acceptance_authority = "user_policy"
            result.review_outcome = "unresolved"
            result.metadata["delivery_review_status"] = "not_approved"
            result.metadata["delivery_inclusion_authority"] = "user"
            pending_results[segment.id] = result

    covered_ids = set(results) | set(pending_results)
    failures = read_segments(translation_failures)
    failure_ids = {s.id for s in failures}
    if (len(failure_ids) != len(failures)
            or failure_ids != set(by_id) - covered_ids
            or any(s.source != by_id[s.id].source for s in failures)):
        raise ValueError("E_DELIVERY_TRANSLATION_QUEUE_MISMATCH")
    if include_unreviewed:
        results.update(pending_results)

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
    metadata = {
        key: value
        for key, value in segment.metadata.items()
        if key not in {CANDIDATE_METADATA_KEY, REVIEW_STATE_KEY}
    }
    if "content_provenance" in candidate:
        provenance = candidate["content_provenance"]
        if not isinstance(provenance, dict):
            raise ValueError("E_OUTPUT_PROVENANCE_INVALID")
        metadata["content_provenance"] = dict(provenance)
    lineage = candidate.get("content_provenance_lineage")
    if lineage is not None:
        if (
            not isinstance(lineage, list)
            or not lineage
            or not all(isinstance(item, dict) for item in lineage)
        ):
            raise ValueError("E_OUTPUT_PROVENANCE_INVALID")
        metadata["content_provenance_lineage"] = [
            dict(item) for item in lineage
        ]
    result = TranslationResult(
        segment_id=segment.id,
        source=segment.source,
        text=candidate["text"],
        translator=candidate["provider"],
        candidate_generation=candidate["generation"],
        candidate_digest=candidate["digest"],
        metadata=metadata,
    )
    saved = segment.metadata.get(REVIEW_STATE_KEY)
    if saved is not None:
        if not isinstance(saved, dict):
            raise ValueError("E_NATIVE_REVIEW_STATE_INVALID")
        if (
            saved.get("candidate_digest") != candidate["digest"]
            or saved.get("candidate_generation") != candidate["generation"]
        ):
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


def partition_native_review_routes(
    segments: list[Segment], routes: list[str], *, stage: str,
    route_models: dict[str, str] | None = None,
    attempt_limits: dict[str, int] | None = None,
    tier_limit: int | None = None,
) -> dict[str, list[Segment]]:
    """Partition pending candidates in configured route order, preserving source order.

    The empty route denotes exhaustion; external routes remain visible rather
    than being skipped to reach a native adapter.
    """
    groups: dict[str, list[Segment]] = {route: [] for route in routes}
    groups[""] = []
    for segment in segments:
        result = _bound_result(segment)
        selected = ""
        current_attempts = [
            item for item in result.attempts
            if item.candidate_generation == result.candidate_generation
        ]
        for route in routes:
            model = (route_models or {}).get(route, "")
            attempts = [item for item in current_attempts
                        if item.provider == route
                        and (not model or not item.model or item.model == model)
                        and (item.stage in JUDGE_STAGES if stage == "review"
                             else item.stage == stage or item.stage.startswith(stage + "-"))]
            if stage == "review":
                limit = review_call_limit(
                    route, routes, attempt_limits or {}, model=model, tier_limit=tier_limit
                )
                exhausted = limit is not None and judge_calls(
                    result.attempts, route, model=model
                ) >= limit
                # A failed route advances to its successor; a flagged candidate
                # may return for confirmation within its configured call policy.
                failed = bool(attempts and attempts[-1].status in {"error", "refused"})
                unavailable = exhausted or failed
            elif stage == "review-rewrite":
                unavailable = any(item.operational is not True for item in attempts)
            else:
                unavailable = bool(attempts)
            if not unavailable:
                selected = route
                break
        groups[selected].append(segment)
    return {route: items for route, items in groups.items() if items}


def select_native_review_route(
    segments: list[Segment], routes: list[str], *, stage: str,
    route_models: dict[str, str] | None = None,
    attempt_limits: dict[str, int] | None = None,
    tier_limit: int | None = None,
) -> str:
    groups = partition_native_review_routes(
        segments, routes, stage=stage, route_models=route_models,
        attempt_limits=attempt_limits, tier_limit=tier_limit,
    )
    if len(groups) != 1:
        raise ValueError("E_NATIVE_ROUTE_PARTITION_REQUIRED: partition pending role routes before dispatch")
    provider = next(iter(groups))
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
    dialogue_contexts: dict[str, dict[str, Any]] | None = None,
    primary_translator: str = "",
) -> dict[str, Any]:
    """Collect validated independent cohorts while preserving unfinished siblings."""
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
    bound_tier = manifest.get("execution_contract", {}).get("review_tier")
    if bound_tier is not None and tier != bound_tier:
        raise ValueError("E_NATIVE_REVIEW_TIER_INVALID: preserve the bound review tier")
    review_policy = manifest.get("content_source_policy")
    expected_task_kind = "triage" if tier == "triage" else "strong_review"
    if isinstance(review_policy, dict) and (
        review_policy.get("task_kind") != expected_task_kind
        and not (
            review_policy.get("task_kind") == "review"
            and review_policy.get("authorization") == "default_deny"
        )
    ):
        raise ValueError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    # No overwrite of any prepared input or prior result. Repeated collection
    # is deterministic and never starts a worker or consumes another attempt.
    if output.resolve() in {source.resolve().parent, manifest_path.resolve().parent}:
        raise ValueError("E_NATIVE_REVIEW_OUTPUT_INVALID")
    output.mkdir(parents=True, exist_ok=True)
    merged = output / "native-verdicts.jsonl"
    summary = merge_native_pass(work_dir, segments, merged, manifest_path=manifest_path)
    rows = _read_jsonl(merged)
    accepted: list[TranslationResult] = []
    strong: list[Segment] = []
    rewrite: list[Segment] = []
    fallback: list[Segment] = []
    awaiting: list[Segment] = []
    invalid_verdicts = 0
    verdict_counts: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    from .native_runtime import native_runtime
    runtime = native_runtime(work_dir)
    route_models = dict((runtime or {}).get("models", {}))
    config_path = work_dir / "game-l10n.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    route_models.update({
        item["name"]: item["model"] for item in config.get("providers", [])
        if isinstance(item.get("model"), str) and item["model"]
    })
    routing = config.get("routing", {})
    routes = routing.get("review_triagers" if tier == "triage" else "reviewers", [])
    rows_by_id = {row["target_id"]: row for row in rows}
    for segment in segments:
        row = rows_by_id.get(segment.id)
        if row is None or (row["status"] != "success" and not summary["review_split_complete"]):
            awaiting.append(segment)
            continue
        result = _bound_result(segment)
        if result.candidate_digest != stable_hash(result.text):
            raise ValueError("E_NATIVE_REVIEW_CANDIDATE_STALE")
        provider = (manifest.get("route_binding") or {}).get("provider") or (
            row.get("provider") or next(iter(routes), "native-worker")
        )
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
            ordinal=judge_calls(result.attempts, provider, stage_prefix=stage)
                    + int(row.get("native_review_attempts", row.get("semantic_attempts", 1))),
            candidate_generation=result.candidate_generation,
            model=(manifest.get("route_binding") or {}).get("model") or route_models.get(provider, ""),
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
        limit = review_call_limit(
            provider, routes or [provider], routing.get("review_attempt_limits", {}),
            model=(manifest.get("route_binding") or {}).get("model") or route_models.get(provider, ""),
            tier_limit=routing.get("triage_max_calls" if tier == "triage" else "strong_max_calls"),
        )
        limit_reached = limit is not None and judge_calls(
            result.attempts, provider, stage_prefix=stage
        ) >= limit
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
            reasons.extend(dialogue_review_reasons(segment.id, dialogue_contexts))
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
            (fallback if limit_reached else rewrite).append(_pending_segment(segment, result))
            continue
        if verdict.verdict is ReviewVerdict.REWRITE or integrity:
            result.metadata["review_tier"] = "strong_pending"
            result.metadata["rewrite_issues"] = rewrite_issue_codes(issues, integrity)
            (fallback if limit_reached else rewrite).append(_pending_segment(segment, result))
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
                        ("fallback-input.jsonl", fallback), ("awaiting-input.jsonl", awaiting)):
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
        "awaiting": len(awaiting), "complete": summary["complete"],
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
    rows = _read_jsonl(merged)
    provider = (manifest.get("route_binding") or {}).get("provider")
    confirmation, unresolved, results, awaiting = [], [], [], []
    rows_by_id = {row["target_id"]: row for row in rows}
    for segment, result in zip(segments, initial, strict=True):
        row = rows_by_id.get(segment.id)
        if row is None:
            awaiting.append(segment)
            results.append(result)
            continue
        row_provider = provider or row.get("provider") or "native-worker"
        integrity = (validate_integrity(segment, row["text"], glossary or [],
                     extra_patterns=extra_patterns, max_length_ratio=max_length_ratio)
                     if row["status"] == "success" else [])
        valid = row["status"] == "success" and not integrity
        interrupted = row["provider"] == "native-control" and row.get("issues") in (
            ["worker_not_started"], ["worker_interrupted"],
        )
        result.attempts.append(AttemptRecord(
            "review-rewrite", row_provider, "success" if valid else "error",
            issues=[item.code for item in integrity] if integrity else list(row.get("issues", [])),
            ordinal=1 + sum(a.stage == "review-rewrite" and a.provider == row_provider
                            and a.operational is not True for a in result.attempts),
            candidate_generation=result.candidate_generation,
            operational=True if interrupted else None,
        ))
        result.status = "failed"
        result.acceptance_authority = ""
        result.review_outcome = ""
        if valid:
            LocalizationOrchestrator._set_candidate(result, row["text"])
            result.text = row["text"]
            result.metadata["review_tier"] = "strong_pending"
            result.metadata["native_confirmation_pending"] = True
            rewritten_candidate = {
                "text": result.text,
                "generation": result.candidate_generation,
                "digest": result.candidate_digest,
                "provider": row_provider,
            }
            if "content_provenance" in row:
                current_provenance = dict(row["content_provenance"])
                prior_lineage = segment.metadata.get(
                    CANDIDATE_METADATA_KEY, {}
                ).get("content_provenance_lineage", [])
                rewritten_candidate["content_provenance"] = current_provenance
                rewritten_candidate["content_provenance_lineage"] = [
                    *[dict(item) for item in prior_lineage],
                    current_provenance,
                ]
                result.metadata["content_provenance"] = current_provenance
                result.metadata["content_provenance_lineage"] = list(
                    rewritten_candidate["content_provenance_lineage"]
                )
            bound = replace(
                segment,
                metadata={
                    **segment.metadata,
                    CANDIDATE_METADATA_KEY: rewritten_candidate,
                },
            )
            confirmation.append(_pending_segment(bound, result))
        else:
            unresolved.append(_pending_segment(segment, result))
        results.append(result)
    write_results(output / "results.json", results)
    for name, items in (("confirmation-input.jsonl", confirmation),
                        ("unresolved-input.jsonl", unresolved), ("awaiting-input.jsonl", awaiting)):
        _atomic_jsonl(output / name, (asdict(item) for item in items))
    return {"records": len(segments), "confirmation_pending": len(confirmation),
            "unresolved": len(unresolved), "accepted": 0,
            "awaiting": len(awaiting), "complete": summary["complete"],
            "output": output.relative_to(work_dir).as_posix()}


def process_native_review_followup(
    work_dir: Path, source: Path, output: Path, orchestrator: Any, *, operation: str,
    deadline_epoch: float | None = None, wave_size: int = 8,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    primary_review_provider: str = "", primary_review_limit: int | None = None,
) -> dict[str, Any]:
    """Run only the locked external review role, with durable per-wave results.

    Provider requests, focused prompts, integrity checks, refusal handling and
    usage audit use the existing provider-neutral adapter. This cannot perform
    primary translation, native triage, or native strong review.
    """
    require_control_plane_ready(work_dir)
    if operation not in {"judge", "rewrite"} or not isinstance(wave_size, int) or isinstance(wave_size, bool) or wave_size < 1:
        raise ValueError("E_NATIVE_REVIEW_FOLLOWUP_INVALID")
    work_dir = work_dir.resolve()
    source.resolve().relative_to(work_dir)
    output.resolve().relative_to(work_dir)
    segments = read_segments(source)
    _require_candidate_bindings(segments, "review")
    routes = (orchestrator.config.reviewer_chain if operation == "judge"
              else orchestrator.config.rewriter_chain)
    if not routes:
        raise ValueError("E_NATIVE_REVIEW_ROUTE_REQUIRED: configure the requested review role")
    wave_size = review_worker_limit(asdict(orchestrator.config), wave_size, tier="strong")
    from .native_runtime import native_runtime
    from .model_policy import NATIVE_ROLE_ROUTES, native_role_models
    runtime_models = dict((native_runtime(work_dir) or {}).get("models", {}))
    config_path = work_dir / "game-l10n.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    configured_reviewers = config.get("routing", {}).get("reviewers", [])
    configured_models = {
        item["name"]: item["model"] for item in config.get("providers", [])
        if isinstance(item.get("model"), str) and item["model"]
    }
    runtime_models.update(configured_models)
    model_bindings = getattr(orchestrator.registry, "model_bindings", None)
    if callable(model_bindings):
        actual_models = model_bindings()
        if any(route in configured_models and configured_models[route] != model
               for route, model in actual_models.items()):
            raise ValueError("E_MODEL_POLICY_BINDING_CONFLICT: rebuild adapters from the changed configured model")
        runtime_models.update(actual_models)
    if not primary_review_provider:
        if configured_reviewers:
            primary_review_provider = configured_reviewers[0]
        elif "strong-review" in native_role_models(config):
            primary_review_provider = NATIVE_ROLE_ROUTES["strong-review"]
    primary_model = runtime_models.get(primary_review_provider, "")
    stage = "review" if operation == "judge" else "review-rewrite"

    def next_route(segment: Segment, result: TranslationResult) -> str:
        groups = partition_native_review_routes(
            [_pending_segment(segment, result)], routes, stage=stage,
            route_models=runtime_models,
            attempt_limits=orchestrator.config.review_attempt_limits,
            tier_limit=orchestrator.config.strong_max_calls,
        )
        return next(iter(groups))

    def external_provider(route: str) -> Any:
        try:
            return orchestrator.registry.get(route)
        except ConfigurationError:
            if route.startswith("native-"):
                raise ValueError("E_NATIVE_REVIEW_NATIVE_FALLBACK_REQUIRED: prepare the configured native role route") from None
            raise

    results_path = output / "results.json"
    saved = read_translation_results(results_path) if results_path.is_file() else []
    cached = {item.segment_id: item for item in saved}
    if len(cached) != len(saved) or not set(cached) <= {s.id for s in segments}:
        raise ValueError("E_NATIVE_REVIEW_FOLLOWUP_CACHE_INVALID")
    providers: dict[str, Any] = {}
    prepared: list[tuple[Segment, TranslationResult, str]] = []
    for segment in segments:
        result = _bound_result(segment)
        native = [a for a in result.attempts
                  if a.provider == primary_review_provider
                  and a.stage in JUDGE_STAGES
                  and a.candidate_generation == result.candidate_generation
                  and (not primary_model or a.model == primary_model)]
        if not native or result.status == "reviewed":
            raise ValueError("E_NATIVE_REVIEW_PRIMARY_BARRIER_REQUIRED")
        if operation == "rewrite" and not result.metadata.get("rewrite_issues"):
            raise ValueError("E_NATIVE_REVIEW_REWRITE_NOT_FLAGGED")
        if (operation == "judge" and native[-1].status not in {"error", "refused"}
                and (primary_review_limit is None
                     or judge_calls(native, primary_review_provider) < primary_review_limit)):
            raise ValueError("E_NATIVE_REVIEW_FALLBACK_NOT_FAILED")
        route = next_route(segment, result)
        provider = external_provider(route) if route else None
        fingerprint = stable_hash({
            "segment": asdict(segment), "operation": operation,
            "provider": provider.fingerprint() if provider is not None else None,
            "context": asdict(orchestrator.context_pack) if orchestrator.context_pack is not None else None,
        })
        prior = cached.get(segment.id)
        if prior and (prior.source != segment.source or
                      prior.metadata.get("native_review_request_fingerprint") != fingerprint):
            raise ValueError("E_NATIVE_REVIEW_FOLLOWUP_CACHE_STALE")
        if prior is not None:
            result = prior
            if result.status == "reviewed" or result.metadata.get("native_confirmation_pending") is True:
                # A locked success keeps its original provider and candidate.
                route = ""
            else:
                route = next_route(segment, result)
                provider = external_provider(route) if route else None
        providers[segment.id] = provider
        prepared.append((segment, result, fingerprint))
    output.mkdir(parents=True, exist_ok=True)

    def run(item: tuple[Segment, TranslationResult, str]) -> TranslationResult | None:
        segment, original, fingerprint = item
        result = (replace(original, metadata=dict(original.metadata), attempts=list(original.attempts),
                          errors=list(original.errors)) if segment.id in cached else original)
        prior_attempts = len(result.attempts)
        result.metadata.pop("provider_authentication_blocked", None)
        provider = providers[segment.id]
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
                result.reviewer = provider.name
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
            limit_reached = rewrites_done >= max(0, orchestrator.config.review_max_rounds - 1)
            if limit_reached:
                result.metadata["native_rewrite_limit_reached"] = True
                return result
            response = orchestrator._rewrite_with_refusal_retry(
                result, provider, clean, result.text, result.metadata["rewrite_issues"], [],
            )
            if response and response.status is AgentStatus.SUCCESS:
                orchestrator._set_candidate(result, response.text)
                result.text = response.text
                prior_provenance = result.metadata.pop(
                    "content_provenance", None
                )
                if isinstance(prior_provenance, dict):
                    lineage = result.metadata.get(
                        "content_provenance_lineage", []
                    )
                    normalized_lineage = (
                        [dict(item) for item in lineage]
                        if isinstance(lineage, list)
                        else []
                    )
                    if (
                        not normalized_lineage
                        or normalized_lineage[-1] != prior_provenance
                    ):
                        normalized_lineage.append(dict(prior_provenance))
                    result.metadata["content_provenance_lineage"] = (
                        normalized_lineage
                    )
                result.metadata["review_tier"] = "strong_pending"
                result.metadata["native_confirmation_pending"] = True
        if (result.metadata.get("provider_authentication_blocked") is True
                and len(result.attempts) == prior_attempts):
            return None
        return result

    pending = [
        item for item in prepared
        if providers[item[0].id] is not None
        and item[1].status != "reviewed"
        and item[1].metadata.get("native_confirmation_pending") is not True
    ]
    executed = authentication_pending = 0
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
            completed = [result for result in pool.map(run, chunk) if result is not None]
        authentication_pending += len(chunk) - len(completed)
        if not completed:
            continue
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
            fallback_candidate = {
                "text": result.text,
                "generation": result.candidate_generation,
                "digest": result.candidate_digest,
                "provider": next(
                    (attempt.provider for attempt in reversed(result.attempts)
                     if attempt.stage.startswith("review-rewrite") and attempt.status == "success"),
                    providers[segment.id].name,
                ),
            }
            current = result.metadata.get("content_provenance")
            prior = segment.metadata.get(CANDIDATE_METADATA_KEY, {}).get(
                "content_provenance"
            )
            prior_lineage = segment.metadata.get(
                CANDIDATE_METADATA_KEY, {}
            ).get("content_provenance_lineage", [])
            lineage = (
                [dict(item) for item in prior_lineage]
                if isinstance(prior_lineage, list)
                else []
            )
            if not lineage and isinstance(prior, dict):
                lineage = [dict(prior)]
            if isinstance(current, dict):
                fallback_candidate["content_provenance"] = dict(current)
                if not lineage or lineage[-1] != current:
                    lineage.append(dict(current))
            if lineage:
                fallback_candidate["content_provenance_lineage"] = lineage
                result.metadata["content_provenance_lineage"] = [
                    dict(item) for item in lineage
                ]
            metadata[CANDIDATE_METADATA_KEY] = fallback_candidate
            confirmation.append(
                _pending_segment(replace(segment, metadata=metadata), result)
            )
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
            "deadline_pending": len(pending) - executed - authentication_pending,
            "authentication_pending": authentication_pending,
            "output": output.relative_to(work_dir).as_posix()}
