from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .formats import read_segments
from .models import Segment
from .privacy import is_within
from .store import stable_hash, stable_hash_sequence


RETRY_SCHEMA = "parr0t.native-translation-retry.v1"
STATE_NAME = "retry-state.json"
CANDIDATES_NAME = "locked-candidates.jsonl"
PENDING_NAME = "pending-targets.jsonl"
FINAL_RESULTS_NAME = "pre-fallback-results.jsonl"
DEFAULT_ROUTE_PLAN = (
    {"provider": "native-luna", "max_retries": 5},
    {"provider": "native-sol", "max_retries": 3},
)


class NativeTranslationRetryError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
            "utf-8"
        ),
    )


def _atomic_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    _atomic_bytes(
        path,
        b"".join(
            (
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            for row in rows
        ),
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise NativeTranslationRetryError("Native retry input is unreadable") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise NativeTranslationRetryError(
                f"Native retry input has invalid JSONL at row {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise NativeTranslationRetryError("Native retry rows must be objects")
        rows.append(row)
    return rows


def _relative(work_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(work_dir.resolve()).as_posix()
    except ValueError as exc:
        raise NativeTranslationRetryError(
            "Native retry artifacts must stay inside the private workspace"
        ) from exc


def _validated_route_plan(route_plan: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    plan = [dict(item) for item in route_plan]
    if (
        not plan
        or len({item.get("provider") for item in plan}) != len(plan)
        or any(
            set(item) != {"provider", "max_retries"}
            or not isinstance(item["provider"], str)
            or not item["provider"]
            or not isinstance(item["max_retries"], int)
            or isinstance(item["max_retries"], bool)
            or item["max_retries"] < 0
            for item in plan
        )
    ):
        raise NativeTranslationRetryError("Native translation retry route plan is invalid")
    return plan


def _validate_result_rows(
    segments: list[Segment],
    results_path: Path,
    *,
    expected_provider: str | None = None,
    allow_legacy_native_provider: bool = False,
) -> tuple[list[dict[str, Any]], list[Segment]]:
    rows = _read_jsonl(results_path)
    if [row.get("target_id") for row in rows] != [segment.id for segment in segments]:
        raise NativeTranslationRetryError(
            "Native retry results must cover the pending targets once in stable order"
        )
    successes: list[dict[str, Any]] = []
    failures: list[Segment] = []
    for segment, raw in zip(segments, rows, strict=True):
        row = dict(raw)
        status = row.get("status")
        text = row.get("text", "")
        provider = row.get("provider")
        allowed_providers = {expected_provider, "native-worker", "native-control"}
        if allow_legacy_native_provider:
            allowed_providers.add("native")
        if expected_provider is not None and provider not in allowed_providers:
            raise NativeTranslationRetryError(
                "Native retry result provider does not match the bound route"
            )
        semantic_providers = {expected_provider}
        if allow_legacy_native_provider:
            semantic_providers.add("native")
        if expected_provider is not None and status != "error" and provider not in semantic_providers:
            raise NativeTranslationRetryError(
                "Native semantic result provider does not match the bound route"
            )
        valid_success_text = isinstance(text, str) and (
            bool(text.strip())
            or (not segment.source.strip() and text == segment.source)
        )
        if status == "success" and valid_success_text:
            row["candidate_generation"] = 1
            row["candidate_digest"] = stable_hash(text)
            successes.append(row)
        elif status in {"refused", "error", "invalid"} and text == "":
            failures.append(segment)
        else:
            raise NativeTranslationRetryError(
                "Native retry results contain an invalid terminal row"
            )
    return successes, failures


def _next_route(
    plan: list[dict[str, Any]], route_index: int, attempt: int, pending: int
) -> tuple[str, int, int, str]:
    if pending == 0:
        return "", 0, route_index, "complete"
    if attempt <= int(plan[route_index]["max_retries"]):
        return str(plan[route_index]["provider"]), attempt + 1, route_index, "native_retry"
    if route_index + 1 < len(plan):
        return str(plan[route_index + 1]["provider"]), 1, route_index + 1, "native_retry"
    return "", 0, route_index, "external_fallback"


def _load_state(work_dir: Path, state_dir: Path) -> dict[str, Any]:
    state_path = state_dir / STATE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeTranslationRetryError("Native translation retry state is missing") from exc
    required = {
        "schema",
        "source",
        "source_fingerprint",
        "route_plan",
        "route_index",
        "last_provider",
        "last_attempt",
        "next_provider",
        "next_attempt",
        "next_action",
        "candidate_artifact",
        "pending_artifact",
        "pending_sha256",
        "final_results",
        "history",
    }
    if not isinstance(state, dict) or set(state) != required:
        raise NativeTranslationRetryError("Native translation retry state is malformed")
    plan = _validated_route_plan(state["route_plan"])
    if (
        state.get("schema") != RETRY_SCHEMA
        or not isinstance(state.get("route_index"), int)
        or isinstance(state.get("route_index"), bool)
        or not 0 <= state["route_index"] < len(plan)
        or state.get("next_action") not in {"native_retry", "external_fallback", "complete"}
        or not isinstance(state.get("history"), list)
    ):
        raise NativeTranslationRetryError("Native translation retry state is invalid")
    for key in ("source", "candidate_artifact", "pending_artifact", "final_results"):
        value = state.get(key)
        if not isinstance(value, str):
            raise NativeTranslationRetryError("Native translation retry paths are invalid")
        if value:
            _relative(work_dir, work_dir / value)
    pending_path = work_dir / state["pending_artifact"]
    if not pending_path.is_file() or _sha256(pending_path) != state["pending_sha256"]:
        raise NativeTranslationRetryError("Native translation retry pending input changed")
    return state


def _state_summary(work_dir: Path, state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    pending_count = len(_read_jsonl(work_dir / state["pending_artifact"]))
    candidate_count = len(_read_jsonl(work_dir / state["candidate_artifact"]))
    return {
        "schema": RETRY_SCHEMA,
        "state": _relative(work_dir, state_dir / STATE_NAME),
        "next_action": state["next_action"],
        "next_provider": state["next_provider"],
        "next_attempt": state["next_attempt"],
        "pending_count": pending_count,
        "candidate_count": candidate_count,
        "pending_input": state["pending_artifact"],
        "pre_fallback_results": state["final_results"],
    }


def _write_stage(
    work_dir: Path,
    state_dir: Path,
    source_segments: list[Segment],
    candidates: dict[str, dict[str, Any]],
    pending: list[Segment],
    state: dict[str, Any],
) -> dict[str, Any]:
    candidate_path = state_dir / CANDIDATES_NAME
    pending_path = state_dir / PENDING_NAME
    final_path = state_dir / FINAL_RESULTS_NAME
    _atomic_jsonl(
        candidate_path,
        (candidates[segment.id] for segment in source_segments if segment.id in candidates),
    )
    _atomic_jsonl(pending_path, (asdict(segment) for segment in pending))
    failure_ids = {segment.id for segment in pending}
    final_rows = []
    for segment in source_segments:
        if segment.id in candidates:
            final_rows.append(candidates[segment.id])
        elif segment.id in failure_ids:
            final_rows.append(
                {
                    "target_id": segment.id,
                    "status": "error",
                    "text": "",
                    "provider": state["last_provider"],
                    "issues": ["pre_fallback_routes_exhausted"],
                }
            )
    _atomic_jsonl(final_path, final_rows)
    state.update(
        {
            "candidate_artifact": _relative(work_dir, candidate_path),
            "pending_artifact": _relative(work_dir, pending_path),
            "pending_sha256": _sha256(pending_path),
            "final_results": _relative(work_dir, final_path),
        }
    )
    _atomic_json(state_dir / STATE_NAME, state)
    return _state_summary(work_dir, state_dir, state)


def start_native_translation_retries(
    work_dir: Path,
    source_path: Path,
    segments: list[Segment],
    first_results: Path,
    state_dir: Path,
    *,
    route_plan: Iterable[dict[str, Any]] = DEFAULT_ROUTE_PLAN,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    source_path = source_path.resolve()
    first_results = first_results.resolve()
    state_dir = state_dir.resolve()
    for path in (source_path, first_results, state_dir):
        if not is_within(path, work_dir):
            raise NativeTranslationRetryError(
                "Native translation retry paths must stay inside the workspace"
            )
    if not segments or len({segment.id for segment in segments}) != len(segments):
        raise NativeTranslationRetryError("Native retry source must contain unique targets")
    plan = _validated_route_plan(route_plan)
    if state_dir.exists() and any(state_dir.iterdir()):
        state = _load_state(work_dir, state_dir)
        expected = stable_hash_sequence(asdict(segment) for segment in segments)
        if (
            state["source_fingerprint"] != expected
            or state["route_plan"] != plan
            or not state["history"]
            or state["history"][0].get("result_sha256") != _sha256(first_results)
        ):
            raise NativeTranslationRetryError("Existing native retry state is stale")
        return {**_state_summary(work_dir, state_dir, state), "cached": True}
    successes, failures = _validate_result_rows(
        segments,
        first_results,
        expected_provider=str(plan[0]["provider"]),
        allow_legacy_native_provider=True,
    )
    candidates = {row["target_id"]: row for row in successes}
    next_provider, next_attempt, route_index, next_action = _next_route(
        plan, 0, 1, len(failures)
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": RETRY_SCHEMA,
        "source": _relative(work_dir, source_path),
        "source_fingerprint": stable_hash_sequence(asdict(segment) for segment in segments),
        "route_plan": plan,
        "route_index": route_index,
        "last_provider": str(plan[0]["provider"]),
        "last_attempt": 1,
        "next_provider": next_provider,
        "next_attempt": next_attempt,
        "next_action": next_action,
        "candidate_artifact": "",
        "pending_artifact": "",
        "pending_sha256": "",
        "final_results": "",
        "history": [
            {
                "provider": str(plan[0]["provider"]),
                "attempt": 1,
                "result_sha256": _sha256(first_results),
                "success_count": len(successes),
                "failure_count": len(failures),
            }
        ],
    }
    return {
        **_write_stage(work_dir, state_dir, segments, candidates, failures, state),
        "cached": False,
    }


def advance_native_translation_retries(
    work_dir: Path, state_dir: Path, attempt_results: Path
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    state_dir = state_dir.resolve()
    attempt_results = attempt_results.resolve()
    for path in (state_dir, attempt_results):
        if not is_within(path, work_dir):
            raise NativeTranslationRetryError(
                "Native translation retry paths must stay inside the workspace"
            )
    state = _load_state(work_dir, state_dir)
    if state["next_action"] != "native_retry":
        raise NativeTranslationRetryError("No native translation retry is pending")
    source_path = work_dir / state["source"]
    try:
        source_segments = read_segments(source_path)
        if source_path.suffix.casefold() == ".jsonl":
            legacy_segments = [
                Segment(**row) for row in _read_jsonl(source_path)
            ]
            legacy_fingerprint = stable_hash_sequence(
                asdict(segment) for segment in legacy_segments
            )
            if legacy_fingerprint == state["source_fingerprint"]:
                source_segments = legacy_segments
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise NativeTranslationRetryError(
            "Native translation retry source is unreadable"
        ) from exc
    if (
        stable_hash_sequence(asdict(segment) for segment in source_segments)
        != state["source_fingerprint"]
    ):
        raise NativeTranslationRetryError("Native translation retry source changed")
    pending_segments = [
        Segment(**row) for row in _read_jsonl(work_dir / state["pending_artifact"])
    ]
    successes, failures = _validate_result_rows(
        pending_segments,
        attempt_results,
        expected_provider=str(state["next_provider"]),
    )
    candidate_rows = _read_jsonl(work_dir / state["candidate_artifact"])
    candidates = {row["target_id"]: row for row in candidate_rows}
    for row in successes:
        if row["target_id"] in candidates:
            raise NativeTranslationRetryError("A locked translation was reprocessed")
        candidates[row["target_id"]] = row

    provider = state["next_provider"]
    attempt = state["next_attempt"]
    plan = _validated_route_plan(state["route_plan"])
    route_index = int(state["route_index"])
    if provider != plan[route_index]["provider"]:
        raise NativeTranslationRetryError("Native translation retry route binding changed")
    next_provider, next_attempt, next_route_index, next_action = _next_route(
        plan, route_index, attempt, len(failures)
    )
    state.update(
        {
            "route_index": next_route_index,
            "last_provider": provider,
            "last_attempt": attempt,
            "next_provider": next_provider,
            "next_attempt": next_attempt,
            "next_action": next_action,
        }
    )
    state["history"].append(
        {
            "provider": provider,
            "attempt": attempt,
            "result_sha256": _sha256(attempt_results),
            "success_count": len(successes),
            "failure_count": len(failures),
        }
    )
    return _write_stage(
        work_dir, state_dir, source_segments, candidates, failures, state
    )


def require_pre_fallback_retry_state(
    work_dir: Path, state_path: Path, source_path: Path, final_results: Path
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    state_path = state_path.resolve()
    state_dir = state_path.parent
    if state_path.name != STATE_NAME:
        raise NativeTranslationRetryError("Native retry state path is not canonical")
    state = _load_state(work_dir, state_dir)
    if state["next_action"] not in {"external_fallback", "complete"}:
        raise NativeTranslationRetryError(
            "Luna and Sol pre-fallback translation attempts are not complete"
        )
    if (work_dir / state["source"]).resolve() != source_path.resolve():
        raise NativeTranslationRetryError("Native retry state targets another source")
    if (work_dir / state["final_results"]).resolve() != final_results.resolve():
        raise NativeTranslationRetryError("Native retry state targets another result set")
    plan = _validated_route_plan(state["route_plan"])
    if plan != [dict(item) for item in DEFAULT_ROUTE_PLAN]:
        raise NativeTranslationRetryError("Native retry route plan does not match the preset")
    return _state_summary(work_dir, state_dir, state)
