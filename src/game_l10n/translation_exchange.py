from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from .content_source_policy import (
    direct_model_provenance,
    validate_content_source_policy,
    validate_output_provenance,
)
from .control_plane import CONTROL_ENVELOPE_SCHEMA, safe_artifact_path, validate_control_envelope
from .errors import GameL10nError
from .models import AttemptRecord, FailureCode, Segment, TranslationResult
from .privacy import is_within
from .store import stable_hash_sequence
from .validators import empty_target_allowed


EXCHANGE_SCHEMA = "parr0t.translation-exchange/v1"
RECEIPT_SCHEMA = "parr0t.translation-exchange-receipt/v1"
REQUESTS_NAME = "requests.jsonl"
SUCCESS_NAME = "success.jsonl"
FAILURE_NAME = "failure.jsonl"
MANIFEST_NAME = "exchange-manifest.json"
RECEIPT_NAME = "exchange-receipt.json"
ENVELOPE_NAME = "control-envelope.json"
FALLBACK_USAGE_SCHEMA = "parr0t.translation-fallback-usage.v1"
FALLBACK_USAGE_RELATIVE = Path(".game-l10n/provider-usage/translation-fallback.json")
EXCHANGE_MODES = frozenset({"manual", "fallback"})
ADAPTER_LEDGER_NAME = "adapter-ledger.json"
ADAPTER_LEDGER_SCHEMA = "parr0t.translation-adapter-ledger.v1"
TRANSLATION_CONTINUATION_SCHEMA = "parr0t.translation-continuation.v1"
TRANSLATION_PREDECESSOR_SCHEMA = "parr0t.translation-predecessor.v1"
OPERATIONAL_FAILURE_CODES = frozenset({
    FailureCode.PROVIDER_ERROR.value, FailureCode.TIMEOUT.value,
    FailureCode.RATE_LIMITED.value, FailureCode.AUTHENTICATION_ERROR.value,
    FailureCode.QUOTA_EXHAUSTED.value,
})
SOURCE_QUEUES = frozenset({"primary_pending", "translation_fallback_pending"})
FAILURE_CODES = frozenset(
    item.value
    for item in FailureCode
    if item
    not in {
        FailureCode.SOURCE_PRESERVED,
        FailureCode.USER_ACCEPTED_AFTER_REVIEW_REFUSAL,
    }
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class TranslationExchangeError(GameL10nError):
    pass


def _valid_success_text(request: dict[str, Any], text: Any) -> bool:
    if not isinstance(text, str):
        return False
    source = request.get("source")
    if not isinstance(source, str):
        return False
    if text.strip() or (not source.strip() and text == source):
        return True
    if text != "":
        return False
    context = request.get("context")
    if not isinstance(context, dict):
        return False
    try:
        segment = Segment(
            id=request["target_id"],
            source=source,
            speaker=context["speaker"],
            scene=context["scene"],
            previous=context["previous"],
            next=context["next"],
            metadata=context["metadata"],
        )
    except (KeyError, TypeError, ValueError):
        return False
    return empty_target_allowed(segment)


def _fallback_usage_state(
    work_dir: Path,
    *,
    max_input_tokens: int | None,
    baseline_input_tokens: int | None,
    max_elapsed_minutes: int | None,
    started_at_epoch: float | None,
    resolve_uncertain_input_tokens: int | None,
) -> tuple[Path, dict[str, Any]]:
    if max_input_tokens is not None and (not isinstance(max_input_tokens, int) or isinstance(max_input_tokens, bool) or max_input_tokens < 1):
        raise TranslationExchangeError("E_FALLBACK_BUDGET_INVALID")
    if baseline_input_tokens is not None and (
        not isinstance(baseline_input_tokens, int)
        or isinstance(baseline_input_tokens, bool)
        or baseline_input_tokens < 0
    ):
        raise TranslationExchangeError("E_FALLBACK_BUDGET_BASELINE_INVALID")
    if max_elapsed_minutes is not None and (
        not isinstance(max_elapsed_minutes, int)
        or isinstance(max_elapsed_minutes, bool)
        or max_elapsed_minutes < 1
    ):
        raise TranslationExchangeError("E_FALLBACK_ELAPSED_BUDGET_INVALID")
    if started_at_epoch is not None and (
        not isinstance(started_at_epoch, (int, float))
        or isinstance(started_at_epoch, bool)
        or not math.isfinite(started_at_epoch)
        or started_at_epoch <= 0
    ):
        raise TranslationExchangeError("E_FALLBACK_ELAPSED_BUDGET_INVALID")
    path = work_dir / FALLBACK_USAGE_RELATIVE
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TranslationExchangeError("E_FALLBACK_USAGE_STATE_INVALID") from exc
        required = {
            "schema",
            "max_input_tokens",
            "baseline_input_tokens",
            "provider_input_tokens",
            "provider_output_tokens",
            "cached_input_tokens",
            "max_elapsed_minutes",
            "started_at_epoch",
            "next_event",
            "events",
            "active_event",
        }
        if not isinstance(state, dict) or set(state) != required:
            raise TranslationExchangeError("E_FALLBACK_USAGE_STATE_INVALID")
        integer_fields = (
            "baseline_input_tokens",
            "provider_input_tokens",
            "provider_output_tokens",
            "cached_input_tokens",
            "next_event",
        )
        if (
            state.get("schema") != FALLBACK_USAGE_SCHEMA
            or any(
                not isinstance(state.get(field), int)
                or isinstance(state.get(field), bool)
                or state[field] < (1 if field == "next_event" else 0)
                for field in integer_fields
            )
            or state.get("max_input_tokens") is not None
            and (
                not isinstance(state["max_input_tokens"], int)
                or isinstance(state["max_input_tokens"], bool)
                or state["max_input_tokens"] < 1
            )
            or state.get("max_elapsed_minutes") is not None
            and (
                not isinstance(state["max_elapsed_minutes"], int)
                or isinstance(state["max_elapsed_minutes"], bool)
                or state["max_elapsed_minutes"] < 1
            )
            or not isinstance(state.get("started_at_epoch"), (int, float))
            or isinstance(state.get("started_at_epoch"), bool)
            or not math.isfinite(state["started_at_epoch"])
            or state["started_at_epoch"] <= 0
            or not isinstance(state.get("events"), list)
            or state.get("active_event") is not None
            and not isinstance(state.get("active_event"), dict)
        ):
            raise TranslationExchangeError("E_FALLBACK_USAGE_STATE_INVALID")
        if state["max_input_tokens"] != max_input_tokens:
            raise TranslationExchangeError("E_FALLBACK_BUDGET_BINDING_MISMATCH")
        if (
            baseline_input_tokens is not None
            and state["baseline_input_tokens"] != baseline_input_tokens
        ):
            raise TranslationExchangeError("E_FALLBACK_BUDGET_BASELINE_MISMATCH")
        if max_elapsed_minutes is not None and state["max_elapsed_minutes"] != max_elapsed_minutes:
            raise TranslationExchangeError("E_FALLBACK_ELAPSED_BUDGET_MISMATCH")
        if started_at_epoch is not None and state["started_at_epoch"] != started_at_epoch:
            raise TranslationExchangeError("E_FALLBACK_ELAPSED_BUDGET_MISMATCH")
    else:
        if baseline_input_tokens is None:
            if max_input_tokens is not None:
                raise TranslationExchangeError("E_FALLBACK_BUDGET_BASELINE_REQUIRED")
            baseline_input_tokens = 0
        state = {
            "schema": FALLBACK_USAGE_SCHEMA,
            "max_input_tokens": max_input_tokens,
            "baseline_input_tokens": baseline_input_tokens,
            "provider_input_tokens": 0,
            "provider_output_tokens": 0,
            "cached_input_tokens": 0,
            "max_elapsed_minutes": max_elapsed_minutes,
            "started_at_epoch": float(time.time() if started_at_epoch is None else started_at_epoch),
            "next_event": 1,
            "events": [],
            "active_event": None,
        }
        _atomic_json(path, state)

    active = state["active_event"]
    if active is not None:
        known = active.get("known_input_tokens", 0)
        if not isinstance(known, int) or isinstance(known, bool) or known < 0:
            raise TranslationExchangeError("E_FALLBACK_USAGE_STATE_INVALID")
        if resolve_uncertain_input_tokens is None and state["max_input_tokens"] is not None:
            raise TranslationExchangeError("E_FALLBACK_BUDGET_ACCOUNTING_UNCERTAIN")
        if resolve_uncertain_input_tokens is None:
            state["events"].append({**active, "accounting": "unmeasured"})
            state["active_event"] = None
            _atomic_json(path, state)
            return path, state
        if (
            not isinstance(resolve_uncertain_input_tokens, int)
            or isinstance(resolve_uncertain_input_tokens, bool)
            or resolve_uncertain_input_tokens < known
        ):
            raise TranslationExchangeError("E_FALLBACK_BUDGET_RESOLUTION_INVALID")
        state["provider_input_tokens"] += resolve_uncertain_input_tokens - known
        state["events"].append(
            {
                **active,
                "input_tokens": resolve_uncertain_input_tokens,
                "accounting": "user_reconciled",
            }
        )
        state["active_event"] = None
        _atomic_json(path, state)
    elif resolve_uncertain_input_tokens is not None:
        raise TranslationExchangeError("E_FALLBACK_BUDGET_RESOLUTION_NOT_REQUIRED")
    return path, state


def _fallback_budget_issue(state: dict[str, Any]) -> str:
    if state["active_event"] is not None:
        return "E_FALLBACK_BUDGET_ACCOUNTING_UNCERTAIN"
    total = state["baseline_input_tokens"] + state["provider_input_tokens"]
    if state["max_input_tokens"] is not None and total >= state["max_input_tokens"]:
        return "E_FALLBACK_INPUT_BUDGET_EXHAUSTED"
    maximum = state["max_elapsed_minutes"]
    if maximum is not None and (time.time() - state["started_at_epoch"]) / 60.0 >= maximum:
        return "E_FALLBACK_ELAPSED_BUDGET_EXHAUSTED"
    return ""


def _begin_fallback_usage_event(
    path: Path,
    state: dict[str, Any],
    *,
    exchange_id: str,
    target_ids: list[str],
) -> None:
    state["active_event"] = {
        "event_id": f"{exchange_id}-{state['next_event']:06d}",
        "exchange_id": exchange_id,
        "target_count": len(target_ids),
        "target_fingerprint": _sha256(_canonical(target_ids)),
        "started_at_epoch": time.time(),
        "known_input_tokens": 0,
    }
    state["next_event"] += 1
    _atomic_json(path, state)


def _result_usage(results: list[TranslationResult]) -> tuple[dict[str, int], bool]:
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}
    complete = True
    for result in results:
        if result.from_cache:
            continue
        if not result.attempts:
            complete = False
            continue
        for attempt in result.attempts:
            usage = attempt.usage if isinstance(attempt.usage, dict) else {}
            input_tokens = usage.get("input_tokens")
            if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0:
                totals["input_tokens"] += input_tokens
            elif attempt.issue_code not in {
                FailureCode.BUDGET_EXHAUSTED.value, FailureCode.INPUT_CAPACITY_EXCEEDED.value
            }:
                complete = False
            for field in ("output_tokens", "cached_input_tokens"):
                value = usage.get(field, 0)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    totals[field] += value
    return totals, complete


def _finish_fallback_usage_event(
    path: Path,
    state: dict[str, Any],
    usage: dict[str, int],
    *,
    complete: bool,
) -> None:
    active = state["active_event"]
    if not isinstance(active, dict):
        raise TranslationExchangeError("E_FALLBACK_USAGE_STATE_INVALID")
    state["provider_input_tokens"] += usage["input_tokens"]
    state["provider_output_tokens"] += usage["output_tokens"]
    state["cached_input_tokens"] += usage["cached_input_tokens"]
    active["known_input_tokens"] = usage["input_tokens"]
    active["output_tokens"] = usage["output_tokens"]
    active["cached_input_tokens"] = usage["cached_input_tokens"]
    if complete or state["max_input_tokens"] is None:
        state["events"].append(
            {
                **active,
                "input_tokens": usage["input_tokens"],
                "accounting": "provider_reported" if complete else "unmeasured",
            }
        )
        state["active_event"] = None
    _atomic_json(path, state)


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _jsonl_bytes(rows: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(_canonical(row) for row in rows)


def _read_jsonl(
    path: Path,
    *,
    allowed_keys: set[str],
    optional_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    optional_keys = optional_keys or set()
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
        if lines[-1] == "":
            lines.pop()
    except UnicodeError as error:
        raise TranslationExchangeError(f"Exchange file is not UTF-8: {path.name}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise TranslationExchangeError(f"Blank JSONL row is not allowed: {path.name}:{line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise TranslationExchangeError(f"Invalid JSONL row: {path.name}:{line_number}") from error
        keys = set(row) if isinstance(row, dict) else set()
        if (
            not isinstance(row, dict)
            or not allowed_keys <= keys
            or not keys <= allowed_keys | optional_keys
        ):
            raise TranslationExchangeError(
                f"Unexpected exchange row shape: {path.name}:{line_number}"
            )
        rows.append(row)
    return rows


def _context(segment: Segment) -> dict[str, Any]:
    value = {
        "speaker": segment.speaker,
        "scene": segment.scene,
        "previous": segment.previous,
        "next": segment.next,
        "metadata": segment.metadata,
    }
    try:
        _canonical(value)
    except (TypeError, ValueError) as error:
        raise TranslationExchangeError(f"Segment context is not JSON serializable: {segment.id}") from error
    return value


def _validated_translation_route_plan(plan: Any) -> list[dict[str, Any]]:
    if not isinstance(plan, list) or not plan:
        raise TranslationExchangeError("E_TRANSLATION_ROUTE_PLAN_INVALID")
    validated = []
    seen = set()
    for item in plan:
        if (
            not isinstance(item, dict)
            or set(item) != {"provider", "model", "max_retries", "transport"}
            or not isinstance(item["provider"], str) or not item["provider"]
            or item["provider"] in seen
            or not isinstance(item["model"], str) or not item["model"]
            or any(ord(char) < 32 for char in item["provider"] + item["model"])
            or not isinstance(item["max_retries"], int) or isinstance(item["max_retries"], bool)
            or item["max_retries"] < 0 or not isinstance(item["transport"], str)
            or item["transport"] not in {"native", "external"}
        ):
            raise TranslationExchangeError("E_TRANSLATION_ROUTE_PLAN_INVALID: exact unambiguous routes required")
        seen.add(item["provider"])
        validated.append(dict(item))
    return validated


def configured_translation_route_plan(work_dir: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve the entire ordered graph from configured adapter types and exact bindings."""
    from .model_policy import native_role_bindings
    from .native_runtime import native_runtime
    from .native_host import resolve_native_model
    routing = config.get("routing", {})
    routes = routing.get("translators", [])
    if not routes:
        return []
    providers = {entry["name"]: entry for entry in config.get("providers", [])}
    runtime = native_runtime(work_dir) or {}
    models = dict(runtime.get("models", {}))
    selector_host = runtime.get("host", "") if runtime.get("transport", "collaboration") == "collaboration" else ""
    for route, requested in native_role_bindings(config).items():
        desired = resolve_native_model(work_dir, selector_host, requested)
        if route in models and models[route] != desired:
            raise TranslationExchangeError("E_MODEL_POLICY_BINDING_CONFLICT: rebind the changed configured role before continuing")
        models[route] = desired
    retries = routing.get("translator_retries", {})
    plan = []
    for route in routes:
        adapter = providers.get(route)
        if adapter is not None:
            if adapter.get("type") not in {"compatible_chat", "openai_responses", "command"}:
                raise TranslationExchangeError("E_TRANSLATION_ROUTE_PLAN_INVALID: unknown adapter transport")
            model, transport = adapter.get("model"), "external"
        else:
            model, transport = models.get(route), "native"
        plan.append({
            "provider": route, "model": model, "max_retries": retries.get(route, 0),
            "transport": transport,
        })
    return _validated_translation_route_plan(plan)


def _transport_block(plan: list[dict[str, Any]], start: int, transport: str) -> list[dict[str, Any]]:
    if not isinstance(start, int) or isinstance(start, bool) or not 0 <= start < len(plan):
        raise TranslationExchangeError("E_TRANSLATION_CONTINUATION_INVALID")
    end = start
    while end < len(plan) and plan[end]["transport"] == transport:
        end += 1
    if end == start:
        raise TranslationExchangeError("E_TRANSLATION_CONTINUATION_INVALID: wrong transport boundary")
    return plan[start:end]


def _validated_translation_continuation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "full_route_plan", "route_plan_sha256", "external_start_index",
        "external_end_index", "prior_native_cohort",
    }:
        raise TranslationExchangeError("E_TRANSLATION_CONTINUATION_INVALID")
    plan = _validated_translation_route_plan(value["full_route_plan"])
    start, end = value["external_start_index"], value["external_end_index"]
    block = _transport_block(plan, start, "external")
    if (
        value["schema"] != TRANSLATION_CONTINUATION_SCHEMA
        or value["route_plan_sha256"] != _sha256(_canonical(plan))
        or not isinstance(end, int) or isinstance(end, bool) or end != start + len(block)
        or value["prior_native_cohort"] is not None and not isinstance(value["prior_native_cohort"], dict)
    ):
        raise TranslationExchangeError("E_TRANSLATION_CONTINUATION_INVALID")
    return value


def build_translation_continuation(
    work_dir: Path, full_route_plan: list[dict[str, Any]], *,
    native_cohort: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = _validated_translation_route_plan(full_route_plan)
    start = 0
    if native_cohort is not None:
        from .native_translation_retry import verify_native_translation_retry_cohort
        verified = verify_native_translation_retry_cohort(
            work_dir, native_cohort,
            source_path=work_dir / native_cohort["source_artifact"],
            results_path=work_dir / native_cohort["results_artifact"],
        )
        if native_cohort["kind"] not in {"all", "native_exhausted"}:
            raise TranslationExchangeError("E_NATIVE_TRANSLATION_PREFIX_INCOMPLETE")
        native_plan = verified["route_plan"]
        start = next((i for i, item in enumerate(plan)
                      if item["provider"] == native_plan[0]["provider"]), -1)
        native_block = _transport_block(plan, start, "native")
        expected = [{key: item[key] for key in ("provider", "model", "max_retries")} for item in native_block]
        if native_plan != expected:
            raise TranslationExchangeError("E_TRANSLATION_ROUTE_PLAN_CHANGED")
        predecessor = (verified["primary_lineage"] or {}).get("translation_predecessor")
        if any(item["transport"] == "external" for item in plan[:start]):
            if not isinstance(predecessor, dict):
                raise TranslationExchangeError("E_TRANSLATION_PREDECESSOR_REQUIRED")
            previous = verify_translation_predecessor(work_dir, work_dir / predecessor["receipt"])
            if (
                previous["receipt_sha256"] != predecessor["receipt_sha256"]
                or previous["contract_fingerprint"] != predecessor["contract_fingerprint"]
                or previous["next_route_index"] != start
                or previous["full_route_plan"][:start] != plan[:start]
            ):
                raise TranslationExchangeError("E_TRANSLATION_PREDECESSOR_INVALID")
        start += len(native_block)
    if start >= len(plan) or plan[start]["transport"] != "external":
        raise TranslationExchangeError("E_EXTERNAL_TRANSLATION_BLOCK_UNAVAILABLE")
    block = _transport_block(plan, start, "external")
    value = {
        "schema": TRANSLATION_CONTINUATION_SCHEMA, "full_route_plan": plan,
        "route_plan_sha256": _sha256(_canonical(plan)),
        "external_start_index": start, "external_end_index": start + len(block),
        "prior_native_cohort": native_cohort,
    }
    return _validated_translation_continuation(value)


def _verify_native_prefix_history(
    work_dir: Path, cohort: dict[str, Any], route_plan: list[dict[str, Any]], target_ids: list[str],
) -> None:
    from .native_translation_retry import load_native_translation_retry_cohort_history
    history = load_native_translation_retry_cohort_history(work_dir, cohort)["history"]
    for target in target_ids:
        for index, route in enumerate(route_plan):
            events = [event for event in history
                      if event.get("provider") == route["provider"] and target in event.get("target_ids", [])]
            ordinal = 1
            unavailable = False
            for event in events:
                if (
                    event.get("model") != route["model"] or event.get("attempt") != ordinal
                    or not isinstance(event.get("semantic_attempt_consumed_ids"), list)
                    or not isinstance(event.get("incomplete_execution_target_ids"), list)
                ):
                    raise TranslationExchangeError("E_NATIVE_RETRY_HISTORY_REQUIRED: re-export exact consumed-attempt evidence")
                if target in event["semantic_attempt_consumed_ids"]:
                    ordinal += 1
                    unavailable = False
                elif target in event.get("operational_target_ids", []):
                    unavailable = True
                else:
                    raise TranslationExchangeError("E_NATIVE_TRANSLATION_PREFIX_INCOMPLETE")
            if ordinal != route["max_retries"] + 2 and not (unavailable and index + 1 < len(route_plan)):
                raise TranslationExchangeError("E_NATIVE_TRANSLATION_PREFIX_INCOMPLETE")


def _verify_continuation_requests(
    work_dir: Path, value: dict[str, Any], manifest: dict[str, Any], requests: list[dict[str, Any]],
) -> None:
    value = _validated_translation_continuation(value)
    cohort = value["prior_native_cohort"]
    if build_translation_continuation(work_dir, value["full_route_plan"], native_cohort=cohort) != value:
        raise TranslationExchangeError("E_TRANSLATION_CONTINUATION_INVALID")
    if cohort is None:
        return
    from .formats import read_segments
    from .native_translation_retry import verify_native_translation_retry_cohort
    verified = verify_native_translation_retry_cohort(
        work_dir, cohort,
        source_path=work_dir / cohort["source_artifact"],
        results_path=work_dir / cohort["results_artifact"],
    )
    if (
        manifest.get("retry_lineage") != verified["primary_lineage"]
        or manifest.get("content_source_policy") not in verified["content_source_policies"]
        or manifest.get("content_source_policy_history") != verified["content_source_policies"]
    ):
        raise TranslationExchangeError("E_TRANSLATION_CONTINUATION_INVALID")
    sources = {item.id: item for item in read_segments(work_dir / cohort["source_artifact"])}
    from .native_translation_retry import _read_jsonl as read_native_results
    outcomes = {row["target_id"]: row for row in read_native_results(work_dir / cohort["results_artifact"])}
    _verify_native_prefix_history(work_dir, cohort, verified["route_plan"], [row["target_id"] for row in requests])
    for row in requests:
        source, outcome = sources.get(row["target_id"]), outcomes.get(row["target_id"])
        if (
            source is None or outcome is None or source.source != row["source"]
            or _context(source) != row["context"]
            or outcome.get("status") not in {"error", "refused"} or outcome.get("text", "") != ""
            or "pre_fallback_routes_exhausted" not in outcome.get("issues", [])
        ):
            raise TranslationExchangeError("E_NATIVE_TRANSLATION_PREFIX_INCOMPLETE")


def select_external_translation_block(
    work_dir: Path, bundle: Path, config: dict[str, Any],
) -> dict[str, Any]:
    """Return only the next eligible contiguous API block; never filter past native work."""
    manifest = _load_manifest(bundle)
    full_plan = configured_translation_route_plan(work_dir, config)
    value = manifest.get("translation_continuation")
    if value is None:
        value = build_translation_continuation(work_dir, full_plan)
    else:
        _validated_translation_continuation(value)
        if value["full_route_plan"] != full_plan:
            raise TranslationExchangeError("E_TRANSLATION_ROUTE_PLAN_CHANGED")
    start, end = value["external_start_index"], value["external_end_index"]
    return {
        "full_route_plan": full_plan,
        "route_models": {item["provider"]: item["model"] for item in full_plan[start:end]},
        "external_start_index": start, "external_end_index": end,
        "next_route_index": end if end < len(full_plan) else None,
        "next_transport": full_plan[end]["transport"] if end < len(full_plan) else None,
        "translation_continuation": value,
    }


def _workspace_artifact(work_dir: Path, value: str | Path) -> Path:
    path = Path(value)
    resolved = (path if path.is_absolute() else work_dir / path).resolve()
    if not is_within(resolved, work_dir.resolve()):
        raise TranslationExchangeError("E_TRANSLATION_PREDECESSOR_INVALID: artifact escaped workspace")
    return resolved


def _immutable_bytes(path: Path, body: bytes) -> None:
    if path.exists():
        if not path.is_file() or path.read_bytes() != body:
            raise TranslationExchangeError("E_TRANSLATION_PREDECESSOR_INVALID: frozen evidence changed")
        return
    _atomic_bytes(path, body)


def _request_segment(row: dict[str, Any]) -> Segment:
    return Segment(id=row["target_id"], source=row["source"], **row["context"])


def _external_block_failure_ready(row: dict[str, Any], continuation: dict[str, Any]) -> bool:
    """Require each declared semantic budget or confirmed operational handoff."""
    start, end = continuation["external_start_index"], continuation["external_end_index"]
    lineage = row.get("attempt_lineage") or []
    for route in continuation["full_route_plan"][start:end]:
        attempts = [item for item in lineage if item["provider"] == route["provider"]]
        if not attempts:
            return False
        previous = 0
        for item in attempts:
            if item["ordinal"] <= previous:
                return False
            if item.get("operational") is True and item["ordinal"] != previous + 1:
                return False
            previous = item["ordinal"]
            if item.get("operational") is None and item["issue_code"] in OPERATIONAL_FAILURE_CODES:
                return False
        semantic = [item for item in attempts if item.get("operational") is not True]
        maximum = max((item["ordinal"] for item in semantic), default=0)
        operational = {item["ordinal"] for item in attempts if item.get("operational") is True and item["ordinal"] < maximum}
        used = max(len(semantic), maximum - len(operational))
        if used > route["max_retries"] + 1:
            return False
        if attempts[-1].get("operational") is not True and used != route["max_retries"] + 1:
            return False
    return True


def prepare_translation_successor(
    work_dir: Path, bundle: Path, *, continuation: dict[str, Any],
) -> dict[str, Any] | None:
    """Freeze only completed API failures for the immediately following native block."""
    from dataclasses import asdict
    from .formats import read_segments
    from .store import stable_hash_sequence
    work_dir = work_dir.resolve()
    bundle = _workspace_artifact(work_dir, bundle)
    continuation = _validated_translation_continuation(continuation)
    plan, end = continuation["full_route_plan"], continuation["external_end_index"]
    if end == len(plan):
        return None
    native = _transport_block(plan, end, "native")
    seal_translation_exchange(work_dir, bundle)
    manifest = _load_manifest(bundle)
    ledger = _read_adapter_ledger(bundle, manifest)
    if ledger is None or ledger.get("translation_continuation") != continuation:
        raise TranslationExchangeError("E_TRANSLATION_CONTINUATION_HISTORY_REQUIRED")
    failures = _read_jsonl(
        bundle / FAILURE_NAME,
        allowed_keys={"target_id", "status", "failure_code", "route", "attempt", "source_sha256", "context_sha256", "retryable"},
        optional_keys={"model", "attempt_lineage"},
    )
    active = (ledger.get("active_event") or {}).get("prior_attempts", {})
    selected = [
        row for row in failures
        if row.get("attempt_lineage")
        and _external_block_failure_ready(row, continuation)
        and (row["target_id"] not in active or row["attempt"] > active[row["target_id"]])
    ]
    if not selected:
        return None
    for row in selected:
        _validate_adapter_outcome(row, {**ledger, "inherited_outcomes": {"success": {}, "failure": {}}}, success=False)
    selected_by_id = {row["target_id"]: row for row in selected}
    requests = _read_jsonl(
        bundle / REQUESTS_NAME,
        allowed_keys={"target_id", "ordinal", "source", "source_sha256", "context", "context_sha256"},
    )
    source_rows = [asdict(_request_segment(row)) for row in requests if row["target_id"] in selected_by_id]
    if len(source_rows) != len(selected):
        raise TranslationExchangeError("E_TRANSLATION_PREDECESSOR_INVALID")
    source_body = _jsonl_bytes(source_rows)
    names = {
        "manifest": MANIFEST_NAME, "requests": REQUESTS_NAME, "success": SUCCESS_NAME,
        "failure": FAILURE_NAME, "adapter_ledger": ADAPTER_LEDGER_NAME,
    }
    bodies = {key: (bundle / name).read_bytes() for key, name in names.items()}
    base = {
        "schema": TRANSLATION_PREDECESSOR_SCHEMA, "exchange": bundle.relative_to(work_dir).as_posix(),
        "exchange_id": manifest["exchange_id"], "translation_continuation": continuation,
        "next_route_index": end, "source_sha256": _sha256(source_body),
        "snapshot_sha256": {key: _sha256(body) for key, body in bodies.items()},
        "external_attempts": {
            row["target_id"]: row["attempt_lineage"] for row in selected
        },
    }
    directory = bundle / "native-continuations" / _sha256(_canonical(base))
    source = directory / "source.jsonl"
    _immutable_bytes(source, source_body)
    files = {}
    for key, name in names.items():
        path = directory / "exchange" / name
        _immutable_bytes(path, bodies[key])
        files[key] = path.relative_to(work_dir).as_posix()
    unsigned = {
        **base, "source": source.relative_to(work_dir).as_posix(), "files": files,
        "source_fingerprint": stable_hash_sequence(asdict(item) for item in read_segments(source)),
    }
    receipt = {**unsigned, "contract_fingerprint": _sha256(_canonical(unsigned))}
    receipt_path = directory / "receipt.json"
    _immutable_bytes(receipt_path, _canonical(receipt))
    return {
        "receipt": receipt_path.relative_to(work_dir).as_posix(),
        "receipt_sha256": _sha256(receipt_path.read_bytes()),
        "contract_fingerprint": receipt["contract_fingerprint"],
        "source": receipt["source"], "source_sha256": receipt["source_sha256"],
        "source_fingerprint": receipt["source_fingerprint"],
        "next_route_index": end,
        "native_route_plan": [
            {key: item[key] for key in ("provider", "model", "max_retries")} for item in native
        ],
        "records": len(source_rows), "next_action": "prepare_native_pass",
    }


def verify_translation_predecessor(
    work_dir: Path, receipt_path: Path, *, full_route_plan: list[dict[str, Any]] | None = None,
    source_path: Path | None = None, segments: list[Segment] | None = None,
) -> dict[str, Any]:
    """Read-only verification of frozen transport boundaries and their preceding chain."""
    from dataclasses import asdict
    from .formats import read_segments
    from .store import stable_hash_sequence
    work_dir = work_dir.resolve()
    current = _workspace_artifact(work_dir, receipt_path)
    first = None
    seen = set()
    later_start = None
    later_plan = None
    while current is not None:
        if current in seen:
            raise TranslationExchangeError("E_TRANSLATION_PREDECESSOR_INVALID: cyclic lineage")
        seen.add(current)
        try:
            receipt_body = current.read_bytes()
            receipt = json.loads(receipt_body.decode("utf-8"))
            unsigned = {key: value for key, value in receipt.items() if key != "contract_fingerprint"}
            if (
                set(unsigned) != {
                    "schema", "exchange", "exchange_id", "translation_continuation", "next_route_index",
                    "source_sha256", "snapshot_sha256", "external_attempts", "source", "files", "source_fingerprint",
                }
                or receipt["contract_fingerprint"] != _sha256(_canonical(unsigned))
                or receipt["schema"] != TRANSLATION_PREDECESSOR_SCHEMA
            ):
                raise ValueError
            continuation = _validated_translation_continuation(receipt["translation_continuation"])
            plan, start, end = (
                continuation["full_route_plan"], continuation["external_start_index"],
                continuation["external_end_index"],
            )
            if end >= len(plan) or receipt["next_route_index"] != end:
                raise ValueError
            native_block = _transport_block(plan, end, "native")
            if first is None and full_route_plan is not None and plan != _validated_translation_route_plan(full_route_plan):
                raise ValueError
            if later_start is not None and (
                end >= later_start or plan[:end] != later_plan[:end]
            ):
                raise ValueError
            base = {key: value for key, value in unsigned.items()
                    if key not in {"source", "files", "source_fingerprint"}}
            expected_directory = (
                _workspace_artifact(work_dir, receipt["exchange"])
                / "native-continuations" / _sha256(_canonical(base))
            )
            if current != expected_directory / "receipt.json":
                raise ValueError
            source = _workspace_artifact(work_dir, receipt["source"])
            if source != expected_directory / "source.jsonl" or _sha256(source.read_bytes()) != receipt["source_sha256"]:
                raise ValueError
            canonical_segments = read_segments(source)
            if stable_hash_sequence(asdict(item) for item in canonical_segments) != receipt["source_fingerprint"]:
                raise ValueError
            if first is None:
                if source_path is not None and _workspace_artifact(work_dir, source_path) != source:
                    raise ValueError
                if segments is not None and stable_hash_sequence(asdict(item) for item in segments) != receipt["source_fingerprint"]:
                    raise ValueError
            names = {
                "manifest": MANIFEST_NAME, "requests": REQUESTS_NAME, "success": SUCCESS_NAME,
                "failure": FAILURE_NAME, "adapter_ledger": ADAPTER_LEDGER_NAME,
            }
            if set(receipt["files"]) != set(names) or set(receipt["snapshot_sha256"]) != set(names):
                raise ValueError
            for key, name in names.items():
                path = _workspace_artifact(work_dir, receipt["files"][key])
                if path != expected_directory / "exchange" / name or _sha256(path.read_bytes()) != receipt["snapshot_sha256"][key]:
                    raise ValueError
            snapshot = expected_directory / "exchange"
            manifest = _load_manifest(snapshot)
            ledger = _read_adapter_ledger(snapshot, manifest)
            if (
                manifest["exchange_id"] != receipt["exchange_id"] or ledger is None
                or ledger.get("translation_continuation") != continuation
                or _sha256((snapshot / REQUESTS_NAME).read_bytes()) != manifest["request_sha256"]
                or ledger["route_plan"] != [
                    {"provider": item["provider"], "model": item["model"]} for item in plan[start:end]
                ]
            ):
                raise ValueError
            requests = {row["target_id"]: row for row in _read_jsonl(
                snapshot / REQUESTS_NAME,
                allowed_keys={"target_id", "ordinal", "source", "source_sha256", "context", "context_sha256"},
            )}
            failures = {row["target_id"]: row for row in _read_jsonl(
                snapshot / FAILURE_NAME,
                allowed_keys={"target_id", "status", "failure_code", "route", "attempt", "source_sha256", "context_sha256", "retryable"},
                optional_keys={"model", "attempt_lineage"},
            )}
            source_rows = _read_jsonl(
                source, allowed_keys={"id", "source", "speaker", "scene", "previous", "next", "metadata"},
            )
            if len(source_rows) != len(receipt["external_attempts"]) or len({row["id"] for row in source_rows}) != len(source_rows):
                raise ValueError
            active = (ledger.get("active_event") or {}).get("prior_attempts", {})
            for row in source_rows:
                target_id = row["id"]
                request, failure = requests[target_id], failures[target_id]
                if (
                    asdict(_request_segment(request)) != row
                    or _sha256(request["source"].encode("utf-8")) != request["source_sha256"]
                    or _sha256(_canonical(request["context"])) != request["context_sha256"]
                    or failure["source_sha256"] != request["source_sha256"]
                    or failure["context_sha256"] != request["context_sha256"]
                    or failure["attempt_lineage"] != receipt["external_attempts"][target_id]
                    or not failure["attempt_lineage"]
                    or target_id in active and failure["attempt"] <= active[target_id]
                ):
                    raise ValueError
                _validate_adapter_outcome(failure, {**ledger, "inherited_outcomes": {"success": {}, "failure": {}}}, success=False)
                if not _external_block_failure_ready(failure, continuation):
                    raise ValueError
            cohort = continuation["prior_native_cohort"]
            previous = None
            native_start = 0
            if cohort is not None:
                from .native_translation_retry import verify_native_translation_retry_cohort
                verified = verify_native_translation_retry_cohort(
                    work_dir, cohort,
                    source_path=work_dir / cohort["source_artifact"],
                    results_path=work_dir / cohort["results_artifact"],
                )
                native_plan = verified["route_plan"]
                native_start = next(i for i, item in enumerate(plan) if item["provider"] == native_plan[0]["provider"])
                block = _transport_block(plan, native_start, "native")
                if (
                    native_start + len(block) != start
                    or native_plan != [{key: item[key] for key in ("provider", "model", "max_retries")} for item in block]
                    or manifest.get("retry_lineage") != verified["primary_lineage"]
                    or manifest.get("content_source_policy") not in verified["content_source_policies"]
                ):
                    raise ValueError
                _verify_native_prefix_history(
                    work_dir, cohort, native_plan, [row["id"] for row in source_rows],
                )
                cohort_rows = {item.id: item for item in read_segments(work_dir / cohort["source_artifact"])}
                for row in source_rows:
                    if asdict(cohort_rows[row["id"]]) != row:
                        raise ValueError
                previous = (verified["primary_lineage"] or {}).get("translation_predecessor")
            elif start != 0:
                raise ValueError
            if native_start and previous is None:
                raise ValueError
            if first is None:
                first = {
                    "receipt": current.relative_to(work_dir).as_posix(),
                    "receipt_sha256": _sha256(receipt_body),
                    "contract_fingerprint": receipt["contract_fingerprint"],
                    "full_route_plan": plan, "next_route_index": end,
                    "native_route_plan": [
                        {key: item[key] for key in ("provider", "model", "max_retries")} for item in native_block
                    ],
                    "source": receipt["source"], "source_sha256": receipt["source_sha256"],
                    "source_fingerprint": receipt["source_fingerprint"],
                    "prior_native_cohort": cohort, "external_attempts": receipt["external_attempts"],
                    "exchange": receipt["exchange"], "files": receipt["files"],
                    "translation_continuation": continuation,
                    "content_source_policy": manifest.get("content_source_policy"),
                    "content_source_policy_history": manifest.get("content_source_policy_history"),
                }
            if previous is None:
                current = None
            else:
                prior = _workspace_artifact(work_dir, previous["receipt"])
                if (
                    _sha256(prior.read_bytes()) != previous["receipt_sha256"]
                    or json.loads(prior.read_text(encoding="utf-8"))["contract_fingerprint"] != previous["contract_fingerprint"]
                ):
                    raise ValueError
                later_start, later_plan = start, plan
                current = prior
        except (OSError, UnicodeError, ValueError, KeyError, TypeError, StopIteration) as exc:
            raise TranslationExchangeError("E_TRANSLATION_PREDECESSOR_INVALID") from exc
    return first


def _validated_policy_history(
    policy: dict[str, Any] | None, history: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if history is None:
        return [policy] if policy is not None else []
    if not isinstance(history, list) or not history or policy is None:
        raise TranslationExchangeError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    try:
        validated = [validate_content_source_policy(item) for item in history]
    except ValueError as exc:
        raise TranslationExchangeError("E_CONTENT_SOURCE_POLICY_IMMUTABLE") from exc
    if policy not in validated or any(
        item["task_kind"] != "translation" or item["route"] != "native-pass:translation"
        for item in validated
    ):
        raise TranslationExchangeError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    return validated


def prepare_translation_exchange(
    work_dir: Path,
    segments: list[Segment],
    output_dir: Path,
    *,
    exchange_id: str,
    mode: str,
    source_queue: str,
    content_source_policy: dict[str, Any] | None = None,
    retry_lineage: dict[str, Any] | None = None,
    content_source_policy_history: list[dict[str, Any]] | None = None,
    translation_continuation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    output_dir = output_dir.resolve()
    if not is_within(output_dir, work_dir):
        raise TranslationExchangeError("Translation exchange must stay inside the private workspace")
    if not safe_artifact_path(output_dir.relative_to(work_dir)):
        # Its receipt path crosses the control plane; fail before any work, not at seal.
        raise TranslationExchangeError("Translation exchange directory name must use [A-Za-z0-9._-]")
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise TranslationExchangeError("Translation exchange directory must be new or empty")
    if not _SAFE_ID.fullmatch(exchange_id):
        raise TranslationExchangeError("exchange_id must be a safe identifier of at most 80 characters")
    if mode not in EXCHANGE_MODES:
        raise TranslationExchangeError("mode must be manual or fallback")
    if source_queue not in SOURCE_QUEUES:
        raise TranslationExchangeError("source_queue is not a supported translation queue")
    if not segments:
        raise TranslationExchangeError("An exchange must contain at least one ordered target")
    target_ids = [segment.id for segment in segments]
    if len(target_ids) != len(set(target_ids)) or any(
        not isinstance(item, str)
        or not item
        or len(item) > 512
        or any(character.isspace() or ord(character) < 32 for character in item)
        for item in target_ids
    ):
        raise TranslationExchangeError("Exchange target IDs must be unique opaque IDs")
    if content_source_policy is not None:
        content_source_policy = validate_content_source_policy(
            content_source_policy
        )
        if (
            content_source_policy["task_kind"] != "translation"
            or content_source_policy["route"] != "native-pass:translation"
        ):
            raise TranslationExchangeError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    history = _validated_policy_history(content_source_policy, content_source_policy_history)
    if retry_lineage is not None and (
        not isinstance(retry_lineage, dict)
        or (
            content_source_policy is not None
            and retry_lineage.get("content_source_policy_digest") not in {
                item["policy_digest"] for item in history
            }
        )
    ):
        raise TranslationExchangeError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")

    rows: list[dict[str, Any]] = []
    for ordinal, segment in enumerate(segments):
        context = _context(segment)
        rows.append(
            {
                "target_id": segment.id,
                "ordinal": ordinal,
                "source": segment.source,
                "source_sha256": _sha256(segment.source.encode("utf-8")),
                "context": context,
                "context_sha256": _sha256(_canonical(context)),
            }
        )
    if translation_continuation is not None:
        _verify_continuation_requests(
            work_dir, translation_continuation,
            {"retry_lineage": retry_lineage, "content_source_policy": content_source_policy,
             "content_source_policy_history": content_source_policy_history},
            rows,
        )
    requests = _jsonl_bytes(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(output_dir / REQUESTS_NAME, requests)
    _atomic_bytes(output_dir / SUCCESS_NAME, b"")
    _atomic_bytes(output_dir / FAILURE_NAME, b"")
    manifest = {
        "schema": EXCHANGE_SCHEMA,
        "exchange_id": exchange_id,
        "mode": mode,
        "source_queue": source_queue,
        "target_count": len(rows),
        "request_sha256": _sha256(requests),
        "files": {
            "requests": REQUESTS_NAME,
            "success": SUCCESS_NAME,
            "failure": FAILURE_NAME,
            "receipt": RECEIPT_NAME,
            "control_envelope": ENVELOPE_NAME,
        },
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
        **({"content_source_policy_history": content_source_policy_history}
           if content_source_policy_history is not None else {}),
        **({"translation_continuation": translation_continuation}
           if translation_continuation is not None else {}),
    }
    _atomic_json(output_dir / MANIFEST_NAME, manifest)
    return {
        "schema": EXCHANGE_SCHEMA,
        "exchange_id": exchange_id,
        "mode": mode,
        "target_count": len(rows),
        "bundle": output_dir.relative_to(work_dir).as_posix(),
        "next_action": "complete_success_and_failure_jsonl_without_changing_requests_or_manifest",
    }


def _load_manifest(bundle: Path) -> dict[str, Any]:
    path = bundle / MANIFEST_NAME
    if not path.is_file():
        raise TranslationExchangeError("Exchange manifest is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema",
        "exchange_id",
        "mode",
        "source_queue",
        "target_count",
        "request_sha256",
        "files",
    }
    optional = {"content_source_policy", "retry_lineage", "content_source_policy_history", "translation_continuation"}
    if not isinstance(payload, dict) or set(payload) - optional != expected:
        raise TranslationExchangeError("Exchange manifest shape is invalid")
    if "translation_continuation" in payload:
        _validated_translation_continuation(payload["translation_continuation"])
    if "content_source_policy" in payload:
        try:
            policy = validate_content_source_policy(
                payload["content_source_policy"]
            )
        except ValueError as exc:
            raise TranslationExchangeError(
                "E_CONTENT_SOURCE_POLICY_IMMUTABLE"
            ) from exc
        lineage = payload.get("retry_lineage")
        history = _validated_policy_history(policy, payload.get("content_source_policy_history"))
        if (
            policy["task_kind"] != "translation"
            or policy["route"] != "native-pass:translation"
            or (
                lineage is not None
                and (
                    not isinstance(lineage, dict)
                    or lineage.get("content_source_policy_digest") not in {
                        item["policy_digest"] for item in history
                    }
                )
            )
        ):
            raise TranslationExchangeError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    elif "retry_lineage" in payload or "content_source_policy_history" in payload:
        raise TranslationExchangeError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    if payload["schema"] != EXCHANGE_SCHEMA or payload["mode"] not in EXCHANGE_MODES:
        raise TranslationExchangeError("Exchange manifest schema or mode is unsupported")
    if not isinstance(payload["exchange_id"], str) or not _SAFE_ID.fullmatch(payload["exchange_id"]):
        raise TranslationExchangeError("Exchange ID is invalid")
    if payload["source_queue"] not in SOURCE_QUEUES:
        raise TranslationExchangeError("Exchange source queue is unsupported")
    if not isinstance(payload["target_count"], int) or isinstance(payload["target_count"], bool) or payload["target_count"] < 1:
        raise TranslationExchangeError("Exchange target count is invalid")
    if not isinstance(payload["request_sha256"], str) or not _DIGEST.fullmatch(payload["request_sha256"]):
        raise TranslationExchangeError("Exchange request digest is invalid")
    expected_files = {
        "requests": REQUESTS_NAME,
        "success": SUCCESS_NAME,
        "failure": FAILURE_NAME,
        "receipt": RECEIPT_NAME,
        "control_envelope": ENVELOPE_NAME,
    }
    if payload["files"] != expected_files:
        raise TranslationExchangeError("Exchange filenames are fixed and must not be changed")
    return payload


def _validated_route_plan(route_models: dict[str, str]) -> list[dict[str, str]]:
    if not isinstance(route_models, dict) or not route_models or any(
        not isinstance(route, str) or not route or len(route) > 120
        or any(ord(char) < 32 for char in route)
        or not isinstance(model, str) or not model
        or any(ord(char) < 32 for char in model)
        for route, model in route_models.items()
    ):
        raise TranslationExchangeError("E_FALLBACK_ADAPTER_PLAN_INVALID")
    return [{"provider": route, "model": model} for route, model in route_models.items()]


def _read_adapter_ledger(bundle: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    path = bundle / ADAPTER_LEDGER_NAME
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or set(state) - {"translation_continuation"} != {
            "schema", "exchange_id", "request_sha256", "route_plan", "plan_sha256",
            "inherited_outcomes", "next_event", "events", "active_event",
        }:
            raise ValueError
        plan = state["route_plan"]
        if not isinstance(plan, list) or any(
            not isinstance(item, dict) or set(item) != {"provider", "model"} for item in plan
        ):
            raise ValueError
        models = {item["provider"]: item["model"] for item in plan}
        if len(models) != len(plan) or _validated_route_plan(models) != plan:
            raise ValueError
        if state.get("translation_continuation") is not None:
            _validated_translation_continuation(state["translation_continuation"])
        if manifest.get("translation_continuation") is not None and state.get("translation_continuation") != manifest["translation_continuation"]:
            raise ValueError
        if (
            state["schema"] != ADAPTER_LEDGER_SCHEMA
            or state["exchange_id"] != manifest["exchange_id"]
            or state["request_sha256"] != manifest["request_sha256"]
            or state["plan_sha256"] != _sha256(_canonical(plan))
            or not isinstance(state["next_event"], int) or isinstance(state["next_event"], bool)
            or state["next_event"] < 1 or not isinstance(state["events"], list)
            or not isinstance(state["inherited_outcomes"], dict)
            or set(state["inherited_outcomes"]) != {"success", "failure"}
        ):
            raise ValueError
        for inherited in state["inherited_outcomes"].values():
            if not isinstance(inherited, dict) or any(
                not isinstance(target_id, str) or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest) for target_id, digest in inherited.items()
            ):
                raise ValueError
        for ordinal, event in enumerate(state["events"], start=1):
            if (
                not isinstance(event, dict) or set(event) != {"event_id", "prior_attempts", "status"}
                or event["event_id"] != ordinal or isinstance(event["event_id"], bool)
                or event["status"] not in {"completed", "recovered_checkpoint"}
                or not isinstance(event["prior_attempts"], dict)
                or any(
                    not isinstance(key, str) or not isinstance(value, int)
                    or isinstance(value, bool) or value < 0
                    for key, value in event["prior_attempts"].items()
                )
            ):
                raise ValueError
        active = state["active_event"]
        if active is not None and (
            not isinstance(active, dict) or set(active) != {"event_id", "prior_attempts"}
            or not isinstance(active["event_id"], int) or isinstance(active["event_id"], bool)
            or active["event_id"] != len(state["events"]) + 1
            or not isinstance(active["prior_attempts"], dict) or not active["prior_attempts"]
            or any(
                not isinstance(key, str) or not isinstance(value, int)
                or isinstance(value, bool) or value < 0
                for key, value in active["prior_attempts"].items()
            )
        ):
            raise ValueError
        if state["next_event"] != len(state["events"]) + (active is not None) + 1:
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise TranslationExchangeError("E_FALLBACK_ADAPTER_LEDGER_INVALID") from exc
    return state


def _bind_adapter_plan(
    bundle: Path, manifest: dict[str, Any], plan: list[dict[str, str]],
    successes: list[dict[str, Any]], failures: list[dict[str, Any]],
    *, translation_continuation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = _read_adapter_ledger(bundle, manifest)
    if state is not None:
        if state["route_plan"] != plan:
            raise TranslationExchangeError(
                "E_FALLBACK_ADAPTER_PLAN_IMMUTABLE: prepare a fresh exchange for a changed route/model plan"
            )
        if state.get("translation_continuation") != translation_continuation:
            raise TranslationExchangeError("E_FALLBACK_ADAPTER_PLAN_IMMUTABLE: preserve transport-block lineage")
        active = state["active_event"]
        if active is not None:
            outcomes = {row["target_id"]: row for row in [*successes, *failures]}
            if any(
                target_id not in outcomes or outcomes[target_id]["attempt"] <= prior
                for target_id, prior in active["prior_attempts"].items()
            ):
                raise TranslationExchangeError(
                    "E_FALLBACK_ADAPTER_ATTEMPT_UNCERTAIN: preserve completed rows and reconcile unfinished execution before retry"
                )
            state["events"].append({**active, "status": "recovered_checkpoint"})
            state["active_event"] = None
            state["inherited_outcomes"]["success"].update(
                (row["target_id"], _sha256(_canonical(row))) for row in successes
            )
            _atomic_json(bundle / ADAPTER_LEDGER_NAME, state)
        return state
    state = {
        "schema": ADAPTER_LEDGER_SCHEMA, "exchange_id": manifest["exchange_id"],
        "request_sha256": manifest["request_sha256"], "route_plan": plan,
        "plan_sha256": _sha256(_canonical(plan)),
        # Older sealed successes remain immutable, without inventing missing model
        # evidence or claiming that they were executed under this new plan.
        "inherited_outcomes": {
            kind: {row["target_id"]: _sha256(_canonical(row)) for row in rows}
            for kind, rows in (("success", successes), ("failure", failures))
        },
        "next_event": 1, "events": [], "active_event": None,
        "translation_continuation": translation_continuation,
    }
    _atomic_json(bundle / ADAPTER_LEDGER_NAME, state)
    return state


def _validate_adapter_outcome(
    row: dict[str, Any], state: dict[str, Any], *, success: bool,
) -> None:
    kind = "success" if success else "failure"
    locked = state["inherited_outcomes"][kind].get(row["target_id"])
    if locked == _sha256(_canonical(row)):
        return
    if success and locked is not None:
        raise TranslationExchangeError("E_FALLBACK_SUCCESS_CHANGED: preserve the locked successful row")
    models = {item["provider"]: item["model"] for item in state["route_plan"]}
    route, model = row["route"], row.get("model")
    lineage = row.get("attempt_lineage")
    if not isinstance(lineage, list):
        raise TranslationExchangeError("E_FALLBACK_ADAPTER_RESULT_BINDING_INVALID")
    position = -1
    order = {item["provider"]: index for index, item in enumerate(state["route_plan"])}
    for item in lineage:
        if (
            not isinstance(item, dict)
            or set(item) - {"operational", "provider_fingerprint", "retryable"}
            != {"provider", "model", "stage", "status", "ordinal", "issue_code"}
            or not isinstance(item.get("provider"), str)
            or item.get("provider") not in models
            or item.get("model") != models[item["provider"]]
            or not isinstance(item.get("stage"), str) or not item["stage"].startswith("translate")
            or not isinstance(item.get("status"), str)
            or item.get("status") not in {"success", "error", "refused", "rejected"}
            or not isinstance(item.get("ordinal"), int) or isinstance(item["ordinal"], bool)
            or item["ordinal"] < 1
            or not isinstance(item.get("issue_code"), str)
            or item.get("issue_code") not in {"", *FAILURE_CODES}
            or item.get("operational") is not None and type(item["operational"]) is not bool
            or item.get("retryable") is not None and type(item["retryable"]) is not bool
            or not isinstance(item.get("provider_fingerprint", ""), str)
            or order[item["provider"]] < position
            or order[item["provider"]] > position + 1
        ):
            raise TranslationExchangeError("E_FALLBACK_ADAPTER_RESULT_BINDING_INVALID")
        position = order[item["provider"]]
    deterministic = (
        not success and route == "deterministic-validator" and model == "" and not lineage
        and row.get("failure_code") in {
            FailureCode.CONSTRAINT_UNSATISFIABLE.value, FailureCode.INTEGRITY_INVALID.value,
        }
    )
    if deterministic:
        return
    if (
        route not in models or model != models[route] or not lineage
        or lineage[-1]["provider"] != route
        or success and lineage[-1]["status"] != "success"
    ):
        raise TranslationExchangeError("E_FALLBACK_ADAPTER_RESULT_BINDING_INVALID")
    if not success and route != state["route_plan"][-1]["provider"] and state.get("translation_continuation") is None:
        raise TranslationExchangeError("E_FALLBACK_ADAPTER_CHAIN_INCOMPLETE")
    if (
        not success and lineage[-1]["status"] in {"error", "refused"}
        and lineage[-1]["issue_code"]
        and row.get("failure_code") != lineage[-1]["issue_code"]
    ):
        raise TranslationExchangeError("E_FALLBACK_ADAPTER_RESULT_BINDING_INVALID")


def _result_adapter_binding(
    result: TranslationResult, state: dict[str, Any], *, success: bool,
) -> tuple[str, str, list[dict[str, Any]]]:
    lineage = []
    for record in result.attempts:
        if record.status == "queued" or record.provider == "deterministic-validator":
            continue
        lineage.append({
            "provider": record.provider, "model": record.model, "stage": record.stage,
            "status": record.status, "ordinal": record.ordinal,
            "issue_code": record.issue_code,
            "operational": record.operational,
            "provider_fingerprint": record.provider_fingerprint,
            "retryable": record.retryable,
        })
    actual_route = (
        result.translator if success and result.translator
        else lineage[-1]["provider"] if lineage else "deterministic-validator"
    )
    actual_model = lineage[-1]["model"] if lineage else ""
    probe = {
        "target_id": result.segment_id, "route": actual_route, "model": actual_model,
        "attempt_lineage": lineage,
        "failure_code": next((record.issue_code for record in reversed(result.attempts)
                              if record.issue_code in FAILURE_CODES), ""),
    }
    _validate_adapter_outcome(probe, state, success=success)
    return actual_route, actual_model, lineage


def _response_digest(bundle: Path) -> str:
    digest = hashlib.sha256()
    for index, name in enumerate((SUCCESS_NAME, FAILURE_NAME)):
        if index:
            digest.update(b"\0")
        with (bundle / name).open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def seal_translation_exchange(work_dir: Path, bundle: Path, *, write_outputs: bool = True) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    bundle = bundle.resolve()
    if not is_within(bundle, work_dir) or not bundle.is_dir():
        raise TranslationExchangeError("Translation exchange must be a private workspace directory")
    manifest = _load_manifest(bundle)
    adapter_ledger = _read_adapter_ledger(bundle, manifest)
    request_path = bundle / REQUESTS_NAME
    success_path = bundle / SUCCESS_NAME
    failure_path = bundle / FAILURE_NAME
    if not request_path.is_file() or not success_path.is_file() or not failure_path.is_file():
        raise TranslationExchangeError("Exchange request, success, and failure files are required")
    request_bytes = request_path.read_bytes()
    if _sha256(request_bytes) != manifest["request_sha256"]:
        raise TranslationExchangeError("Immutable exchange requests were modified")

    request_rows = _read_jsonl(
        request_path,
        allowed_keys={"target_id", "ordinal", "source", "source_sha256", "context", "context_sha256"},
    )
    if len(request_rows) != manifest["target_count"]:
        raise TranslationExchangeError("Exchange target count no longer matches the manifest")
    requests: dict[str, dict[str, Any]] = {}
    for ordinal, row in enumerate(request_rows):
        target_id = row["target_id"]
        if not isinstance(target_id, str) or not target_id or target_id in requests or row["ordinal"] != ordinal:
            raise TranslationExchangeError("Exchange requests must keep unique IDs and stable order")
        if not isinstance(row["source"], str) or _sha256(row["source"].encode("utf-8")) != row["source_sha256"]:
            raise TranslationExchangeError("Exchange source digest is invalid")
        if not isinstance(row["context"], dict) or _sha256(_canonical(row["context"])) != row["context_sha256"]:
            raise TranslationExchangeError("Exchange context digest is invalid")
        requests[target_id] = row

    success_rows = _read_jsonl(
        success_path,
        allowed_keys={
            "target_id", "status", "text", "route", "attempt",
            "source_sha256", "context_sha256",
        },
        optional_keys={"content_provenance", "model", "attempt_lineage"},
    )
    failure_rows = _read_jsonl(
        failure_path,
        allowed_keys={"target_id", "status", "failure_code", "route", "attempt", "source_sha256", "context_sha256", "retryable"},
        optional_keys={"model", "attempt_lineage"},
    )
    resolved: set[str] = set()
    for row in success_rows:
        target_id = row["target_id"]
        if not isinstance(target_id, str):
            raise TranslationExchangeError("Success target ID is invalid")
        request = requests.get(target_id)
        if request is None or target_id in resolved:
            raise TranslationExchangeError("Success target is unknown or duplicated")
        valid_success_text = _valid_success_text(request, row["text"])
        if row["status"] != "success" or not valid_success_text:
            raise TranslationExchangeError("Success rows require non-empty translated text")
        if not isinstance(row["route"], str) or not row["route"] or len(row["route"]) > 120:
            raise TranslationExchangeError("Success route is invalid")
        if not isinstance(row["attempt"], int) or isinstance(row["attempt"], bool) or row["attempt"] < 1:
            raise TranslationExchangeError("Success attempt must be a positive integer")
        if row["source_sha256"] != request["source_sha256"] or row["context_sha256"] != request["context_sha256"]:
            raise TranslationExchangeError("Success row is not bound to the current source and context")
        if adapter_ledger is not None:
            _validate_adapter_outcome(row, adapter_ledger, success=True)
        policy = manifest.get("content_source_policy")
        provenance = row.get("content_provenance")
        if policy is not None:
            if not isinstance(provenance, dict):
                raise TranslationExchangeError("E_OUTPUT_PROVENANCE_INVALID")
            try:
                validate_output_provenance(
                    provenance,
                    policy=policy,
                    model=row.get("model") or str(provenance.get("model", "")),
                    route=row["route"],
                )
            except ValueError as exc:
                raise TranslationExchangeError(str(exc)) from None
        elif provenance is not None:
            raise TranslationExchangeError("E_OUTPUT_PROVENANCE_INVALID")
        resolved.add(target_id)
    for row in failure_rows:
        target_id = row["target_id"]
        if not isinstance(target_id, str):
            raise TranslationExchangeError("Failure target ID is invalid")
        request = requests.get(target_id)
        if request is None or target_id in resolved:
            raise TranslationExchangeError("Failure target is unknown or duplicated")
        if (
            row["status"] != "failed"
            or not isinstance(row["failure_code"], str)
            or row["failure_code"] not in FAILURE_CODES
        ):
            raise TranslationExchangeError("Failure row status or code is invalid")
        if not isinstance(row["route"], str) or not row["route"] or len(row["route"]) > 120:
            raise TranslationExchangeError("Failure route is invalid")
        if not isinstance(row["attempt"], int) or isinstance(row["attempt"], bool) or row["attempt"] < 1:
            raise TranslationExchangeError("Failure attempt must be a positive integer")
        if not isinstance(row["retryable"], bool):
            raise TranslationExchangeError("Failure retryable must be a boolean")
        if row["source_sha256"] != request["source_sha256"] or row["context_sha256"] != request["context_sha256"]:
            raise TranslationExchangeError("Failure row is not bound to the current source and context")
        if adapter_ledger is not None:
            _validate_adapter_outcome(row, adapter_ledger, success=False)
        resolved.add(target_id)

    pending = len(requests) - len(resolved)
    unresolved = len(failure_rows) + pending
    response_sha256 = _response_digest(bundle)
    receipt_relative = (bundle / RECEIPT_NAME).relative_to(work_dir).as_posix()
    envelope_relative = (bundle / ENVELOPE_NAME).relative_to(work_dir).as_posix()
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "exchange_id": manifest["exchange_id"],
        "mode": manifest["mode"],
        "status": "complete" if unresolved == 0 else "fallback_required",
        "target_count": len(requests),
        "success_count": len(success_rows),
        "failure_count": len(failure_rows),
        "pending_count": pending,
        "request_sha256": manifest["request_sha256"],
        "response_sha256": response_sha256,
        "control_envelope": envelope_relative,
    }
    if write_outputs:
        _atomic_json(bundle / RECEIPT_NAME, receipt)
    transitions: list[dict[str, Any]] = []
    if success_rows:
        transitions.append({"from": manifest["source_queue"], "to": "review_pending", "count": len(success_rows)})
    if unresolved:
        transitions.append(
            {"from": manifest["source_queue"], "to": "translation_fallback_pending", "count": unresolved}
        )
    envelope = {
        "schema": CONTROL_ENVELOPE_SCHEMA,
        "worker_role": "translator",
        "stage": "translation",
        "status": "success" if unresolved == 0 else "fallback_required",
        "target_ids": list(requests),
        "counts": {
            "total": len(requests),
            "success": len(success_rows),
            "error": len(failure_rows),
            "pending": pending,
            "fallback_required": unresolved,
        },
        "issue_codes": [] if unresolved == 0 else ["fallback_required"],
        "artifact_paths": [receipt_relative],
        "queue_transitions": transitions,
        "route_evidence_path": "",
    }
    if write_outputs:
        _atomic_json(bundle / ENVELOPE_NAME, envelope)
        validate_control_envelope(
            work_dir,
            bundle / ENVELOPE_NAME,
            expected_role="translator",
            expected_stage="translation",
        )
    return {
        "schema": RECEIPT_SCHEMA,
        "exchange_id": manifest["exchange_id"],
        "status": receipt["status"],
        "target_count": len(requests),
        "success_count": len(success_rows),
        "failure_count": len(failure_rows),
        "pending_count": pending,
        "request_sha256": manifest["request_sha256"],
        "response_sha256": response_sha256,
        "control_envelope": envelope_relative,
    }


def process_translation_exchange(
    work_dir: Path,
    bundle: Path,
    translator: Callable[[list[Segment]], list[TranslationResult]],
    *,
    route: str,
    model: str = "",
    route_models: dict[str, str] | None = None,
    full_route_plan: list[dict[str, Any]] | None = None,
    resume_translator: Callable[[list[Segment], list[TranslationResult]], list[TranslationResult]] | None = None,
    continuation_status: Callable[[Segment, TranslationResult], dict[str, Any]] | None = None,
    retry_failures: bool = False,
    max_input_tokens: int | None = None,
    baseline_input_tokens: int | None = None,
    max_elapsed_minutes: int | None = None,
    started_at_epoch: float | None = None,
    resolve_uncertain_input_tokens: int | None = None,
    wave_size: int = 32,
    input_tokens_unlimited: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run a configured fallback adapter without exposing exchange text to the parent."""

    work_dir = work_dir.resolve()
    bundle = bundle.resolve()
    if not is_within(bundle, work_dir) or not bundle.is_dir():
        raise TranslationExchangeError("Translation exchange must be a private workspace directory")
    if not isinstance(route, str) or not route or len(route) > 120:
        raise TranslationExchangeError("Fallback route name is invalid")
    if not isinstance(wave_size, int) or isinstance(wave_size, bool) or wave_size < 1:
        raise TranslationExchangeError("Fallback wave_size must be a positive integer")
    if input_tokens_unlimited and (
        max_input_tokens is not None or baseline_input_tokens is not None
        or resolve_uncertain_input_tokens is not None
        or max_elapsed_minutes is not None and (
            not isinstance(max_elapsed_minutes, int) or isinstance(max_elapsed_minutes, bool)
            or max_elapsed_minutes < 1
        )
    ):
        raise TranslationExchangeError("E_FALLBACK_UNLIMITED_POLICY_INVALID")
    if max_input_tokens is None and any(
        value is not None for value in (baseline_input_tokens, resolve_uncertain_input_tokens)
    ):
        raise TranslationExchangeError("E_FALLBACK_TOKEN_BUDGET_REQUIRED")
    if max_elapsed_minutes is not None and (
        not isinstance(max_elapsed_minutes, int) or isinstance(max_elapsed_minutes, bool)
        or max_elapsed_minutes < 1
    ):
        raise TranslationExchangeError("E_FALLBACK_ELAPSED_BUDGET_INVALID")
    if started_at_epoch is not None and (
        not isinstance(started_at_epoch, (int, float)) or isinstance(started_at_epoch, bool)
        or not math.isfinite(started_at_epoch) or started_at_epoch <= 0
    ):
        raise TranslationExchangeError("E_FALLBACK_ELAPSED_BUDGET_INVALID")
    elapsed_started_at = time.time() if started_at_epoch is None else started_at_epoch
    manifest = _load_manifest(bundle)
    plan = _validated_route_plan(route_models) if route_models is not None else (
        _validated_route_plan({route: model}) if model else None
    )
    if plan is not None and (
        plan[0]["provider"] != route or model and plan[0]["model"] != model
    ):
        raise TranslationExchangeError("E_FALLBACK_ADAPTER_PLAN_INVALID")
    if "content_source_policy" in manifest and plan is None:
        raise TranslationExchangeError("E_OUTPUT_PROVENANCE_INVALID")
    continuation = manifest.get("translation_continuation")
    if continuation is None and full_route_plan is not None:
        continuation = build_translation_continuation(work_dir, full_route_plan)
    if continuation is not None:
        _validated_translation_continuation(continuation)
        if full_route_plan is not None and continuation["full_route_plan"] != _validated_translation_route_plan(full_route_plan):
            raise TranslationExchangeError("E_TRANSLATION_ROUTE_PLAN_CHANGED")
        start, end = continuation["external_start_index"], continuation["external_end_index"]
        eligible_plan = [{"provider": item["provider"], "model": item["model"]}
                         for item in continuation["full_route_plan"][start:end]]
        if plan != eligible_plan:
            raise TranslationExchangeError("E_EXTERNAL_TRANSLATION_BLOCK_REQUIRED: do not skip a native boundary")
    existing_ledger = _read_adapter_ledger(bundle, manifest)
    if existing_ledger is not None and (
        plan is None or existing_ledger["route_plan"] != plan
    ):
        raise TranslationExchangeError(
            "E_FALLBACK_ADAPTER_PLAN_IMMUTABLE: preserve the bound route/model plan"
        )
    if manifest["mode"] != "fallback" or manifest["source_queue"] != "translation_fallback_pending":
        raise TranslationExchangeError(
            "Automatic exchange processing requires a translation fallback bundle"
        )

    # Validate all immutable bindings and any already completed rows first.
    sealed = seal_translation_exchange(work_dir, bundle)
    request_rows = _read_jsonl(
        bundle / REQUESTS_NAME,
        allowed_keys={"target_id", "ordinal", "source", "source_sha256", "context", "context_sha256"},
    )
    success_rows = _read_jsonl(
        bundle / SUCCESS_NAME,
        allowed_keys={
            "target_id", "status", "text", "route", "attempt",
            "source_sha256", "context_sha256",
        },
        optional_keys={"content_provenance", "model", "attempt_lineage"},
    )
    failure_rows = _read_jsonl(
        bundle / FAILURE_NAME,
        allowed_keys={"target_id", "status", "failure_code", "route", "attempt", "source_sha256", "context_sha256", "retryable"},
        optional_keys={"model", "attempt_lineage"},
    )
    if continuation is not None:
        _verify_continuation_requests(work_dir, continuation, manifest, request_rows)
    adapter_ledger = _bind_adapter_plan(
        bundle, manifest, plan, success_rows, failure_rows, translation_continuation=continuation,
    ) if plan is not None else None
    prior_failures = {row["target_id"]: row for row in failure_rows}
    prior_results_by_id = {
        row["target_id"]: TranslationResult(
            row["target_id"], row["source"], "", "failed",
            attempts=[AttemptRecord(**item) for item in prior_failures[row["target_id"]].get("attempt_lineage", [])],
            metadata={"failure_code": prior_failures[row["target_id"]]["failure_code"]},
        )
        for row in request_rows if row["target_id"] in prior_failures
    }
    resume_eligible = {
        row["target_id"] for row in request_rows
        if retry_failures and resume_translator is not None and continuation_status is not None
        and row["target_id"] in prior_results_by_id
        and continuation_status(_request_segment(row), prior_results_by_id[row["target_id"]]).get("eligible") is True
    }
    operationally_blocked = {
        row["target_id"] for row in failure_rows
        if adapter_ledger is not None and row["target_id"] not in resume_eligible and (
            not row.get("attempt_lineage") and row["failure_code"] in OPERATIONAL_FAILURE_CODES
            or any(
                isinstance(item, dict) and (
                    item.get("operational") is True
                    or item.get("operational") is None and item.get("issue_code") in OPERATIONAL_FAILURE_CODES
                )
                for item in row.get("attempt_lineage", [])
            )
        )
    }
    unbound_operational_history = any(
        row["target_id"] in operationally_blocked and (
            not row.get("model") or not row.get("attempt_lineage")
            or any(item.get("operational") is None and item.get("issue_code") in OPERATIONAL_FAILURE_CODES
                   for item in row.get("attempt_lineage", []))
        ) for row in failure_rows
    )
    success_ids = {row["target_id"] for row in success_rows}
    pending_rows = [
        row
        for row in request_rows
        if row["target_id"] not in success_ids
        and (retry_failures or row["target_id"] not in prior_failures)
        and row["target_id"] not in operationally_blocked
        and not (
            continuation is not None and row["target_id"] in prior_failures
            and (
                _external_block_failure_ready(prior_failures[row["target_id"]], continuation) and row["target_id"] not in resume_eligible
                or resume_translator is None
            )
        )
    ]

    def report_progress(**statistics: int | str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback({"stage": "translation_fallback", **statistics})
        except (TypeError, ValueError, OSError):
            return

    report_progress(completed_records=0, fallback_pending=len(pending_rows), attempts=0)

    segments: list[Segment] = []
    requests_by_id: dict[str, dict[str, Any]] = {}
    for row in pending_rows:
        context = row["context"]
        if not isinstance(context, dict) or set(context) != {
            "speaker",
            "scene",
            "previous",
            "next",
            "metadata",
        }:
            raise TranslationExchangeError("Exchange request context is invalid")
        try:
            segment = Segment(
                id=row["target_id"],
                source=row["source"],
                speaker=context["speaker"],
                scene=context["scene"],
                previous=context["previous"],
                next=context["next"],
                metadata=context["metadata"],
            )
        except (TypeError, ValueError) as exc:
            raise TranslationExchangeError("Exchange request segment is invalid") from exc
        segments.append(segment)
        requests_by_id[segment.id] = row

    retained_failures = dict(prior_failures)
    success_by_id = {row["target_id"]: row for row in success_rows}
    retryable_codes = {
        FailureCode.MALFORMED_RESPONSE.value,
        FailureCode.OUTPUT_TRUNCATED.value,
        FailureCode.PROVIDER_ERROR.value,
        FailureCode.RATE_LIMITED.value,
        FailureCode.TIMEOUT.value,
        FailureCode.INTEGRITY_INVALID.value,
    }
    request_order = {row["target_id"]: row["ordinal"] for row in request_rows}
    usage_path: Path | None = None
    usage_state: dict[str, Any] | None = None
    if (
        pending_rows
        and not input_tokens_unlimited
        and max_input_tokens is None
        and (work_dir / FALLBACK_USAGE_RELATIVE).is_file()
    ):
        try:
            inherited = json.loads((work_dir / FALLBACK_USAGE_RELATIVE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TranslationExchangeError("E_FALLBACK_USAGE_STATE_INVALID") from exc
        max_input_tokens = inherited.get("max_input_tokens")
        baseline_input_tokens = inherited.get("baseline_input_tokens")
        if max_elapsed_minutes is None:
            max_elapsed_minutes = inherited.get("max_elapsed_minutes")
        if started_at_epoch is None:
            started_at_epoch = inherited.get("started_at_epoch")
    if max_input_tokens is not None or (max_elapsed_minutes is not None and not input_tokens_unlimited):
        usage_path, usage_state = _fallback_usage_state(
            work_dir,
            max_input_tokens=max_input_tokens,
            baseline_input_tokens=baseline_input_tokens,
            max_elapsed_minutes=max_elapsed_minutes,
            started_at_epoch=started_at_epoch,
            resolve_uncertain_input_tokens=resolve_uncertain_input_tokens,
        )

    processed = 0
    run_success = 0
    run_failed = 0
    issue_code = ""
    for offset in range(0, len(segments), wave_size):
        wave = segments[offset : offset + wave_size]
        if (input_tokens_unlimited and max_elapsed_minutes is not None and
                time.time() >= elapsed_started_at + max_elapsed_minutes * 60):
            issue_code = "E_FALLBACK_ELAPSED_BUDGET_EXHAUSTED"
            break
        if usage_state is not None:
            issue_code = _fallback_budget_issue(usage_state)
            if issue_code:
                break
            assert usage_path is not None
            _begin_fallback_usage_event(
                usage_path,
                usage_state,
                exchange_id=manifest["exchange_id"],
                target_ids=[segment.id for segment in wave],
            )
        if adapter_ledger is not None:
            adapter_ledger["active_event"] = {
                "event_id": adapter_ledger["next_event"],
                "prior_attempts": {
                    segment.id: int(prior_failures.get(segment.id, {}).get("attempt", 0))
                    for segment in wave
                },
            }
            adapter_ledger["next_event"] += 1
            _atomic_json(bundle / ADAPTER_LEDGER_NAME, adapter_ledger)
        resuming = continuation is not None and any(segment.id in prior_failures for segment in wave)
        if resuming:
            prior_results = [prior_results_by_id[segment.id] for segment in wave if segment.id in prior_results_by_id]
            assert resume_translator is not None
            results = resume_translator(wave, prior_results)
        else:
            results = translator(wave)
        if [result.segment_id for result in results] != [segment.id for segment in wave]:
            raise TranslationExchangeError("Fallback results do not match exchange order and IDs")
        if any(result.source != requests_by_id[result.segment_id]["source"] for result in results):
            raise TranslationExchangeError("Fallback results do not match immutable exchange sources")
        validated: dict[str, tuple[bool, str, str, list[dict[str, Any]], dict[str, Any] | None]] = {}
        wave_errors: list[TranslationExchangeError] = []
        for result in results:
            try:
                valid = result.status in {"translated", "reviewed", "accepted_unreviewed"} and _valid_success_text(
                    requests_by_id[result.segment_id], result.text
                )
                binding = _result_adapter_binding(result, adapter_ledger, success=valid) if adapter_ledger is not None else (route, model, [])
                actual_route, actual_model, lineage = binding
                if resuming and result.segment_id in prior_failures:
                    original = [
                        {"operational": None, "provider_fingerprint": "", "retryable": None, **item}
                        for item in prior_failures[result.segment_id].get("attempt_lineage", [])
                    ]
                    if lineage[:len(original)] != original:
                        raise TranslationExchangeError("E_FALLBACK_ADAPTER_HISTORY_CHANGED: preserve consumed attempts and ordinals")
                provenance = None
                if valid and "content_source_policy" in manifest:
                    provenance = result.metadata.get("content_provenance")
                    if provenance is None:
                        provenance = direct_model_provenance(
                            manifest["content_source_policy"], actual_model, route=actual_route,
                        )
                    try:
                        provenance = validate_output_provenance(
                            provenance, policy=manifest["content_source_policy"],
                            model=actual_model, route=actual_route,
                        )
                    except ValueError as exc:
                        raise TranslationExchangeError(str(exc)) from None
                validated[result.segment_id] = (valid, actual_route, actual_model, lineage, provenance)
            except TranslationExchangeError as exc:
                wave_errors.append(exc)
        for result in results:
            prepared = validated.get(result.segment_id)
            if prepared is None:
                continue
            valid_success_text, actual_route, actual_model, lineage, provenance = prepared
            request = requests_by_id[result.segment_id]
            prior_attempt = int(prior_failures.get(result.segment_id, {}).get("attempt", 0))
            attempt = prior_attempt + 1
            if (
                result.status in {"translated", "reviewed", "accepted_unreviewed"}
                and valid_success_text
            ):
                success_row = {
                    "target_id": result.segment_id,
                    "status": "success",
                    "text": result.text,
                    "route": actual_route,
                    "attempt": attempt,
                    "source_sha256": request["source_sha256"],
                    "context_sha256": request["context_sha256"],
                }
                if adapter_ledger is not None:
                    success_row.update(model=actual_model, attempt_lineage=lineage)
                if provenance is not None:
                    success_row["content_provenance"] = dict(provenance)
                success_by_id[result.segment_id] = success_row
                retained_failures.pop(result.segment_id, None)
                run_success += 1
                continue
            failure_code = next(
                (
                    record.issue_code
                    for record in reversed(result.attempts)
                    if record.issue_code in FAILURE_CODES
                ),
                FailureCode.EXPLICIT_PROVIDER_REFUSAL.value
                if any(record.status == "refused" for record in result.attempts)
                else FailureCode.PROVIDER_ERROR.value,
            )
            operational_failure = any(
                item.get("operational") is True
                or item.get("operational") is None and item["issue_code"] in OPERATIONAL_FAILURE_CODES
                for item in lineage
            ) or not lineage and failure_code in OPERATIONAL_FAILURE_CODES
            if any(item.get("operational") is None and item["issue_code"] in OPERATIONAL_FAILURE_CODES for item in lineage):
                unbound_operational_history = True
            retained_failures[result.segment_id] = {
                "target_id": result.segment_id,
                "status": "failed",
                "failure_code": failure_code,
                "route": actual_route,
                "attempt": attempt,
                "source_sha256": request["source_sha256"],
                "context_sha256": request["context_sha256"],
                "retryable": failure_code in retryable_codes and not (
                    adapter_ledger is not None and operational_failure
                ),
            }
            if adapter_ledger is not None:
                retained_failures[result.segment_id].update(model=actual_model, attempt_lineage=lineage)
                if operational_failure:
                    operationally_blocked.add(result.segment_id)
            run_failed += 1

        ordered_success = sorted(
            success_by_id.values(), key=lambda row: request_order[row["target_id"]]
        )
        ordered_failure = sorted(
            retained_failures.values(), key=lambda row: request_order[row["target_id"]]
        )
        # Remove superseded failures before committing their successes so an
        # interrupted two-file checkpoint cannot leave overlapping result IDs.
        _atomic_bytes(bundle / FAILURE_NAME, _jsonl_bytes(ordered_failure))
        _atomic_bytes(bundle / SUCCESS_NAME, _jsonl_bytes(ordered_success))
        sealed = seal_translation_exchange(work_dir, bundle)
        if usage_state is not None:
            assert usage_path is not None
            usage, complete = _result_usage([
                replace(result, attempts=result.attempts[
                    len(prior_failures.get(result.segment_id, {}).get("attempt_lineage", [])):
                ]) if resuming else result
                for result in results
            ])
            _finish_fallback_usage_event(
                usage_path, usage_state, usage, complete=complete
            )
            if not complete and usage_state["max_input_tokens"] is not None:
                issue_code = "E_FALLBACK_BUDGET_ACCOUNTING_UNCERTAIN"
        if adapter_ledger is not None:
            if not wave_errors:
                adapter_ledger["events"].append({**adapter_ledger["active_event"], "status": "completed"})
                adapter_ledger["active_event"] = None
            adapter_ledger["inherited_outcomes"]["success"].update(
                (result.segment_id, _sha256(_canonical(success_by_id[result.segment_id])))
                for result in results if result.segment_id in success_by_id
            )
            _atomic_json(bundle / ADAPTER_LEDGER_NAME, adapter_ledger)
        if wave_errors:
            raise wave_errors[0]
        processed += len(results)
        report_progress(
            completed_records=processed,
            translation_completed=run_success,
            fallback_pending=max(0, len(pending_rows) - processed),
            failed_records=run_failed,
            attempts=processed,
        )
        if issue_code:
            break
    if operationally_blocked and not issue_code:
        issue_code = (
            "E_FALLBACK_ADAPTER_HISTORY_UNBOUND" if unbound_operational_history
            else "E_FALLBACK_OPERATIONAL_ROUTES_EXHAUSTED"
        )
    native_continuation = prepare_translation_successor(
        work_dir, bundle, continuation=continuation,
    ) if continuation is not None else None
    if native_continuation is not None and issue_code == "E_FALLBACK_OPERATIONAL_ROUTES_EXHAUSTED":
        issue_code = ""
    capacity_failures = [
        {"target_id": row["target_id"], "route": row["route"], "model": row.get("model", "")}
        for row in retained_failures.values()
        if row["failure_code"] == FailureCode.INPUT_CAPACITY_EXCEEDED.value
    ]
    if capacity_failures and native_continuation is None and issue_code in {"", "E_FALLBACK_OPERATIONAL_ROUTES_EXHAUSTED"}:
        issue_code = "E_FALLBACK_INPUT_CAPACITY_EXCEEDED"
    continuation_repairs = [
        row["target_id"] for row in retained_failures.values()
        if continuation is not None and not _external_block_failure_ready(row, continuation)
    ]
    if continuation_repairs and issue_code in {"", "E_FALLBACK_OPERATIONAL_ROUTES_EXHAUSTED"}:
        issue_code = "E_FALLBACK_ADAPTER_ATTEMPT_POLICY_INCOMPLETE"

    budget = None
    if usage_state is not None:
        total = (
            usage_state["baseline_input_tokens"]
            + usage_state["provider_input_tokens"]
        )
        budget = {
            "status": "blocked" if issue_code else "ready",
            "issue_code": issue_code,
            "input_tokens": total,
            "max_input_tokens": usage_state["max_input_tokens"],
            "input_tokens_known": usage_state["active_event"] is None and all(
                event.get("accounting") in {"provider_reported", "user_reconciled"}
                for event in usage_state["events"]
            ),
            "remaining_input_tokens": (
                max(0, usage_state["max_input_tokens"] - total)
                if usage_state["max_input_tokens"] is not None else None
            ),
            "events": len(usage_state["events"]),
            "state": FALLBACK_USAGE_RELATIVE.as_posix(),
        }
    elif input_tokens_unlimited:
        # Historical uncertain accounting is neither resolved nor erased.
        # The configured adapter retains actual attempt usage in audit.jsonl;
        # an explicitly unlimited input policy has only the original deadline.
        budget = {
            "status": "blocked" if issue_code else "ready",
            "issue_code": issue_code,
            "input_tokens_unlimited": True,
            "historical_usage_unchanged": True,
            "provider_usage_audit": ".game-l10n/audit.jsonl",
        }
    from .native_followup import (
        FOLLOWUP_MANIFEST_NAME, REVIEW_INPUT_NAME, UNRESOLVED_NAME,
        _followup_collection_request, _freeze_collection_inventory,
    )
    collection = None
    parent_path = bundle.parent / FOLLOWUP_MANIFEST_NAME
    if parent_path.is_file():
        try:
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            if bundle.relative_to(work_dir).as_posix() in parent["fallback_bundles"]:
                prior = continuation["prior_native_cohort"] if continuation is not None else None
                if parent.get("cohort_lineage") != prior:
                    raise TranslationExchangeError("E_TRANSLATION_PREDECESSOR_INVALID")
                from .formats import read_segments
                collection = _followup_collection_request(
                    work_dir, bundle.parent, prior,
                    standalone=read_segments(work_dir / parent["source"]) if prior is None else None,
                )
        except (ValueError, OSError, KeyError, TypeError, TranslationExchangeError):
            collection = {"next_action": "repair_chain_inventory",
                          "issue_code": "E_NATIVE_FOLLOWUP_CHAIN_INVENTORY_REPAIR_REQUIRED"}
    if collection is None:
        collection_input, original = _freeze_collection_inventory(
            work_dir, bundle, [_request_segment(row) for row in request_rows],
        )
        collection = {
            "next_action": "collect_translation_exchange_candidates",
            "command": "collect-translation-exchange-candidates",
            "argv": [
                "collect-translation-exchange-candidates", "--workspace", str(work_dir),
                "--input", collection_input.relative_to(work_dir).as_posix(),
                "--bundle", bundle.relative_to(work_dir).as_posix(),
                "--review-output", (bundle / REVIEW_INPUT_NAME).relative_to(work_dir).as_posix(),
                "--unresolved-output", (bundle / UNRESOLVED_NAME).relative_to(work_dir).as_posix(),
            ],
            "input": collection_input.relative_to(work_dir).as_posix(),
            "source_fingerprint": stable_hash_sequence(asdict(item) for item in original),
        }
    return {
        **sealed,
        "processed": processed,
        "route": route,
        "route_models": {item["provider"]: item["model"] for item in plan} if plan is not None else None,
        "adapter_ledger": (bundle / ADAPTER_LEDGER_NAME).relative_to(work_dir).as_posix() if adapter_ledger is not None else None,
        "native_continuation": native_continuation,
        "next_action": "prepare_native_pass" if native_continuation is not None else collection["next_action"],
        "collection": collection,
        "operationally_blocked": len(operationally_blocked),
        "issue_code": issue_code,
        "continuation_repair_target_ids": continuation_repairs,
        "capacity_repair": {
            "records": capacity_failures,
            "next_action": "compact_task_context_or_increase_configured_provider_max_input_chars",
            "user_budget_exhausted": False,
        } if capacity_failures else None,
        "budget": budget,
    }
