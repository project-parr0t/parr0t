from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from .control_plane import CONTROL_ENVELOPE_SCHEMA, validate_control_envelope
from .errors import GameL10nError
from .models import FailureCode, Segment, TranslationResult
from .privacy import is_within


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


def _fallback_usage_state(
    work_dir: Path,
    *,
    max_input_tokens: int,
    baseline_input_tokens: int | None,
    max_elapsed_minutes: int | None,
    started_at_epoch: float | None,
    resolve_uncertain_input_tokens: int | None,
) -> tuple[Path, dict[str, Any]]:
    if max_input_tokens < 1:
        raise TranslationExchangeError("E_FALLBACK_BUDGET_INVALID")
    if baseline_input_tokens is not None and baseline_input_tokens < 0:
        raise TranslationExchangeError("E_FALLBACK_BUDGET_BASELINE_INVALID")
    if max_elapsed_minutes is not None and max_elapsed_minutes < 1:
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
            "max_input_tokens",
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
                or state[field] < (1 if field in {"max_input_tokens", "next_event"} else 0)
                for field in integer_fields
            )
            or state.get("max_elapsed_minutes") is not None
            and (
                not isinstance(state["max_elapsed_minutes"], int)
                or isinstance(state["max_elapsed_minutes"], bool)
                or state["max_elapsed_minutes"] < 1
            )
            or not isinstance(state.get("started_at_epoch"), (int, float))
            or isinstance(state.get("started_at_epoch"), bool)
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
    else:
        if baseline_input_tokens is None:
            raise TranslationExchangeError("E_FALLBACK_BUDGET_BASELINE_REQUIRED")
        state = {
            "schema": FALLBACK_USAGE_SCHEMA,
            "max_input_tokens": max_input_tokens,
            "baseline_input_tokens": baseline_input_tokens,
            "provider_input_tokens": 0,
            "provider_output_tokens": 0,
            "cached_input_tokens": 0,
            "max_elapsed_minutes": max_elapsed_minutes,
            "started_at_epoch": float(started_at_epoch or time.time()),
            "next_event": 1,
            "events": [],
            "active_event": None,
        }
        _atomic_json(path, state)

    active = state["active_event"]
    if active is not None:
        if resolve_uncertain_input_tokens is None:
            raise TranslationExchangeError("E_FALLBACK_BUDGET_ACCOUNTING_UNCERTAIN")
        known = active.get("known_input_tokens", 0)
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
    if total >= state["max_input_tokens"]:
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
    zero_usage_codes = {
        FailureCode.BUDGET_EXHAUSTED.value,
        FailureCode.AUTHENTICATION_ERROR.value,
        FailureCode.QUOTA_EXHAUSTED.value,
        FailureCode.RATE_LIMITED.value,
    }
    for result in results:
        if not result.attempts:
            complete = False
            continue
        for attempt in result.attempts:
            usage = attempt.usage if isinstance(attempt.usage, dict) else {}
            input_tokens = usage.get("input_tokens")
            if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0:
                totals["input_tokens"] += input_tokens
            elif attempt.issue_code not in zero_usage_codes:
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
    if complete:
        state["events"].append(
            {
                **active,
                "input_tokens": usage["input_tokens"],
                "accounting": "provider_reported",
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


def _read_jsonl(path: Path, *, allowed_keys: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as error:
        raise TranslationExchangeError(f"Exchange file is not UTF-8: {path.name}") from error
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise TranslationExchangeError(f"Blank JSONL row is not allowed: {path.name}:{line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise TranslationExchangeError(f"Invalid JSONL row: {path.name}:{line_number}") from error
        if not isinstance(row, dict) or set(row) != allowed_keys:
            raise TranslationExchangeError(f"Unexpected exchange row shape: {path.name}:{line_number}")
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


def prepare_translation_exchange(
    work_dir: Path,
    segments: list[Segment],
    output_dir: Path,
    *,
    exchange_id: str,
    mode: str,
    source_queue: str,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    output_dir = output_dir.resolve()
    if not is_within(output_dir, work_dir):
        raise TranslationExchangeError("Translation exchange must stay inside the private workspace")
    if output_dir.exists():
        if not output_dir.is_dir() or any(output_dir.iterdir()):
            raise TranslationExchangeError("Translation exchange directory must be new or empty")
    if not _SAFE_ID.fullmatch(exchange_id):
        raise TranslationExchangeError("exchange_id must be a safe identifier of at most 80 characters")
    if mode not in EXCHANGE_MODES:
        raise TranslationExchangeError("mode must be manual or fallback")
    if source_queue not in SOURCE_QUEUES:
        raise TranslationExchangeError("source_queue is not a supported translation queue")
    if not segments or len(segments) > 2000:
        raise TranslationExchangeError("An exchange must contain 1 to 2000 ordered targets")
    target_ids = [segment.id for segment in segments]
    if len(target_ids) != len(set(target_ids)) or any(
        not isinstance(item, str)
        or not item
        or len(item) > 512
        or any(character.isspace() or ord(character) < 32 for character in item)
        for item in target_ids
    ):
        raise TranslationExchangeError("Exchange target IDs must be unique opaque IDs")

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
    if not isinstance(payload, dict) or set(payload) != expected:
        raise TranslationExchangeError("Exchange manifest shape is invalid")
    if payload["schema"] != EXCHANGE_SCHEMA or payload["mode"] not in EXCHANGE_MODES:
        raise TranslationExchangeError("Exchange manifest schema or mode is unsupported")
    if not isinstance(payload["exchange_id"], str) or not _SAFE_ID.fullmatch(payload["exchange_id"]):
        raise TranslationExchangeError("Exchange ID is invalid")
    if payload["source_queue"] not in SOURCE_QUEUES:
        raise TranslationExchangeError("Exchange source queue is unsupported")
    if not isinstance(payload["target_count"], int) or not 1 <= payload["target_count"] <= 2000:
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


def seal_translation_exchange(work_dir: Path, bundle: Path) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    bundle = bundle.resolve()
    if not is_within(bundle, work_dir) or not bundle.is_dir():
        raise TranslationExchangeError("Translation exchange must be a private workspace directory")
    manifest = _load_manifest(bundle)
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
        allowed_keys={"target_id", "status", "text", "route", "attempt", "source_sha256", "context_sha256"},
    )
    failure_rows = _read_jsonl(
        failure_path,
        allowed_keys={"target_id", "status", "failure_code", "route", "attempt", "source_sha256", "context_sha256", "retryable"},
    )
    resolved: set[str] = set()
    for row in success_rows:
        target_id = row["target_id"]
        if not isinstance(target_id, str):
            raise TranslationExchangeError("Success target ID is invalid")
        request = requests.get(target_id)
        if request is None or target_id in resolved:
            raise TranslationExchangeError("Success target is unknown or duplicated")
        if row["status"] != "success" or not isinstance(row["text"], str) or not row["text"].strip():
            raise TranslationExchangeError("Success rows require non-empty translated text")
        if not isinstance(row["route"], str) or not row["route"] or len(row["route"]) > 120:
            raise TranslationExchangeError("Success route is invalid")
        if not isinstance(row["attempt"], int) or isinstance(row["attempt"], bool) or row["attempt"] < 1:
            raise TranslationExchangeError("Success attempt must be a positive integer")
        if row["source_sha256"] != request["source_sha256"] or row["context_sha256"] != request["context_sha256"]:
            raise TranslationExchangeError("Success row is not bound to the current source and context")
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
        resolved.add(target_id)

    pending = len(requests) - len(resolved)
    unresolved = len(failure_rows) + pending
    response_sha256 = _sha256(success_path.read_bytes() + b"\0" + failure_path.read_bytes())
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
        "control_envelope": envelope_relative,
    }


def process_translation_exchange(
    work_dir: Path,
    bundle: Path,
    translator: Callable[[list[Segment]], list[TranslationResult]],
    *,
    route: str,
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
    if not isinstance(wave_size, int) or isinstance(wave_size, bool) or not 1 <= wave_size <= 256:
        raise TranslationExchangeError("Fallback wave_size must be between 1 and 256")
    if input_tokens_unlimited and (
        max_input_tokens is not None or baseline_input_tokens is not None
        or resolve_uncertain_input_tokens is not None
        or not isinstance(max_elapsed_minutes, int) or isinstance(max_elapsed_minutes, bool)
        or max_elapsed_minutes < 1
        or not isinstance(started_at_epoch, (int, float)) or isinstance(started_at_epoch, bool)
        or started_at_epoch <= 0
    ):
        raise TranslationExchangeError("E_FALLBACK_UNLIMITED_POLICY_INVALID")
    if not input_tokens_unlimited and max_input_tokens is None and any(
        value is not None
        for value in (
            baseline_input_tokens,
            max_elapsed_minutes,
            started_at_epoch,
            resolve_uncertain_input_tokens,
        )
    ):
        raise TranslationExchangeError("E_FALLBACK_BUDGET_REQUIRED")
    manifest = _load_manifest(bundle)
    if manifest["mode"] != "fallback" or manifest["source_queue"] != "translation_fallback_pending":
        raise TranslationExchangeError(
            "Automatic exchange processing requires a translation fallback bundle"
        )

    # Validate all immutable bindings and any already completed rows first.
    seal_translation_exchange(work_dir, bundle)
    request_rows = _read_jsonl(
        bundle / REQUESTS_NAME,
        allowed_keys={"target_id", "ordinal", "source", "source_sha256", "context", "context_sha256"},
    )
    success_rows = _read_jsonl(
        bundle / SUCCESS_NAME,
        allowed_keys={"target_id", "status", "text", "route", "attempt", "source_sha256", "context_sha256"},
    )
    failure_rows = _read_jsonl(
        bundle / FAILURE_NAME,
        allowed_keys={"target_id", "status", "failure_code", "route", "attempt", "source_sha256", "context_sha256", "retryable"},
    )
    success_ids = {row["target_id"] for row in success_rows}
    prior_failures = {row["target_id"]: row for row in failure_rows}
    pending_rows = [
        row
        for row in request_rows
        if row["target_id"] not in success_ids
        and (retry_failures or row["target_id"] not in prior_failures)
    ]
    if not pending_rows:
        sealed = seal_translation_exchange(work_dir, bundle)
        return {**sealed, "processed": 0, "route": route}

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

    retained_failures = {
        row["target_id"]: row
        for row in failure_rows
        if not retry_failures or row["target_id"] not in requests_by_id
    }
    success_by_id = {row["target_id"]: row for row in success_rows}
    retryable_codes = {
        FailureCode.MALFORMED_RESPONSE.value,
        FailureCode.PROVIDER_ERROR.value,
        FailureCode.RATE_LIMITED.value,
        FailureCode.TIMEOUT.value,
        FailureCode.INTEGRITY_INVALID.value,
    }
    request_order = {row["target_id"]: row["ordinal"] for row in request_rows}
    usage_path: Path | None = None
    usage_state: dict[str, Any] | None = None
    if max_input_tokens is not None:
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
        if (input_tokens_unlimited and
                time.time() >= started_at_epoch + max_elapsed_minutes * 60):
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
        results = translator(wave)
        if [result.segment_id for result in results] != [segment.id for segment in wave]:
            raise TranslationExchangeError("Fallback results do not match exchange order and IDs")
        for result in results:
            request = requests_by_id[result.segment_id]
            prior_attempt = int(prior_failures.get(result.segment_id, {}).get("attempt", 0))
            attempt = prior_attempt + 1
            if result.status in {"translated", "reviewed", "accepted_unreviewed"} and result.text.strip():
                success_by_id[result.segment_id] = {
                    "target_id": result.segment_id,
                    "status": "success",
                    "text": result.text,
                    "route": route,
                    "attempt": attempt,
                    "source_sha256": request["source_sha256"],
                    "context_sha256": request["context_sha256"],
                }
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
            retained_failures[result.segment_id] = {
                "target_id": result.segment_id,
                "status": "failed",
                "failure_code": failure_code,
                "route": route,
                "attempt": attempt,
                "source_sha256": request["source_sha256"],
                "context_sha256": request["context_sha256"],
                "retryable": failure_code in retryable_codes,
            }
            run_failed += 1

        ordered_success = sorted(
            success_by_id.values(), key=lambda row: request_order[row["target_id"]]
        )
        ordered_failure = sorted(
            retained_failures.values(), key=lambda row: request_order[row["target_id"]]
        )
        _atomic_bytes(bundle / SUCCESS_NAME, _jsonl_bytes(ordered_success))
        _atomic_bytes(bundle / FAILURE_NAME, _jsonl_bytes(ordered_failure))
        seal_translation_exchange(work_dir, bundle)
        processed += len(results)
        report_progress(
            completed_records=processed,
            translation_completed=run_success,
            fallback_pending=max(0, len(pending_rows) - processed),
            failed_records=run_failed,
            attempts=processed,
        )
        if usage_state is not None:
            assert usage_path is not None
            usage, complete = _result_usage(results)
            _finish_fallback_usage_event(
                usage_path, usage_state, usage, complete=complete
            )
            if not complete:
                issue_code = "E_FALLBACK_BUDGET_ACCOUNTING_UNCERTAIN"
                break

    sealed = seal_translation_exchange(work_dir, bundle)
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
            "remaining_input_tokens": max(
                0, usage_state["max_input_tokens"] - total
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
    return {
        **sealed,
        "processed": processed,
        "route": route,
        "issue_code": issue_code,
        "budget": budget,
    }
