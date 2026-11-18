from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .content_source_policy import (
    content_source_policy,
    validate_content_source_policy,
    validate_output_provenance,
)
from .formats import read_segments
from .models import Segment
from .privacy import is_within
from .store import stable_hash, stable_hash_sequence
from .validators import validate_integrity


RETRY_SCHEMA = "parr0t.native-translation-retry.v1"
STATE_NAME = "retry-state.json"
CANDIDATES_NAME = "locked-candidates.jsonl"
PENDING_NAME = "pending-targets.jsonl"
FINAL_RESULTS_NAME = "pre-fallback-results.jsonl"


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


def _read_jsonl(path: Path, *, expected_sha256: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if expected_sha256 is None:
            lines = path.read_text(encoding="utf-8-sig").split("\n")
        else:
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != expected_sha256:
                raise NativeTranslationRetryError("Native retry immutable result history changed")
            lines = body.decode("utf-8-sig").split("\n")
            del body
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


def _preserve_attempt_results(state_dir: Path, path: Path) -> Path:
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    archived = state_dir / "attempt-results" / f"{digest}.jsonl"
    if archived.exists():
        if archived.read_bytes() != body:
            raise NativeTranslationRetryError("Native retry result evidence changed")
    else:
        _atomic_bytes(archived, body)
    return archived


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
            set(item) not in (
                {"provider", "max_retries"},
                {"provider", "model", "max_retries"},
            )
            or not isinstance(item["provider"], str)
            or not item["provider"]
            or (
                "model" in item
                and (
                    not isinstance(item["model"], str)
                    or not item["model"]
                )
            )
            or not isinstance(item["max_retries"], int)
            or isinstance(item["max_retries"], bool)
            or item["max_retries"] < 0
            for item in plan
        )
    ):
        raise NativeTranslationRetryError("Native translation retry route plan is invalid")
    return plan

def _route_plans_match(
    stored: list[dict[str, Any]], configured: list[dict[str, Any]]
) -> bool:
    if stored == configured:
        return True
    return (
        all("model" not in item for item in stored)
        and [
            {
                "provider": item["provider"],
                "max_retries": item["max_retries"],
            }
            for item in configured
        ]
        == stored
    )


def _validate_result_rows(
    segments: list[Segment],
    results_path: Path,
    *,
    expected_provider: str | None = None,
    allow_legacy_native_provider: bool = False,
    content_policy: dict[str, Any] | None = None,
    expected_model: str | None = None,
    expected_sha256: str | None = None,
) -> tuple[list[dict[str, Any]], list[Segment]]:
    rows = _read_jsonl(results_path, expected_sha256=expected_sha256)
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
        if status == "success" and isinstance(text, str) and (
            bool(text.strip()) or not validate_integrity(segment, text, [])
        ):
            row["candidate_generation"] = 1
            row["candidate_digest"] = stable_hash(text)
            if content_policy is not None:
                row["content_provenance"] = validate_output_provenance(
                    row.get("content_provenance"),
                    policy=content_policy,
                    model=expected_model or str(expected_provider),
                    route=str(expected_provider),
                )
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


def _operational_targets(rows: list[dict[str, Any]]) -> dict[str, str]:
    codes = {
        "provider_operational_error", "execution_guard_denied", "provider_error",
        "rate_limited", "quota_exhausted", "authentication_error", "timeout",
        "capacity_error", "host_dispatch_denied", "host_model_not_found",
        "host_model_unavailable",
    }
    return {
        row["target_id"]: (
            "execution_guard_denied" if "execution_guard_denied" in row.get("issues", [])
            else "provider_operational_error"
        ) for row in rows
        if row.get("status") == "error"
        and bool(set(row.get("issues", [])) & codes)
    }


def _target_successor(
    plan: list[dict[str, Any]], route_index: int, attempt: int, *,
    operational: str | None, policy: dict[str, Any] | None,
    repair_required: bool = False,
) -> dict[str, Any]:
    if operational:
        if policy is not None and operational in policy["fallback_on"] and route_index + 1 < len(plan):
            return {"route_index": route_index + 1, "attempt": 1, "action": "native_retry"}
        return {"route_index": route_index, "attempt": attempt, "action": "repair_native_runtime"}
    if repair_required:
        return {"route_index": route_index, "attempt": attempt, "action": "repair_native_runtime"}
    _, next_attempt, next_index, action = _next_route(plan, route_index, attempt, 1)
    return {"route_index": next_index, "attempt": next_attempt, "action": action}


def _select_pending_cohort(
    state: dict[str, Any], source_segments: list[Segment],
) -> list[Segment]:
    progress = state["target_progress"]
    first = next(
        (progress[item.id] for action in ("native_retry", "repair_native_runtime", "external_fallback")
         for item in source_segments
         if item.id in progress and progress[item.id]["action"] == action),
        None,
    )
    state["next_action"] = first["action"] if first else "complete"
    state["route_index"] = first["route_index"] if first else state["route_index"]
    state["next_attempt"] = first["attempt"] if first and first["action"] == "native_retry" else 0
    state["next_provider"] = (
        state["route_plan"][first["route_index"]]["provider"]
        if first and first["action"] == "native_retry" else ""
    )
    pending = [
        item for item in source_segments
        if item.id in progress and progress[item.id] == first
    ] if first else []
    selected = {item.id for item in pending}
    state["deferred_failure_ids"] = [
        item.id for item in source_segments if item.id in progress and item.id not in selected
    ]
    return pending


def _bound_source_segments(work_dir: Path, state: dict[str, Any]) -> list[Segment]:
    source_path = work_dir / state["source"]
    try:
        segments = read_segments(source_path)
        if source_path.suffix.casefold() == ".jsonl":
            legacy = [Segment(**row) for row in _read_jsonl(source_path)]
            if stable_hash_sequence(asdict(item) for item in legacy) == state["source_fingerprint"]:
                segments = legacy
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise NativeTranslationRetryError("Native translation retry source is unreadable") from exc
    if stable_hash_sequence(asdict(item) for item in segments) != state["source_fingerprint"]:
        raise NativeTranslationRetryError("Native translation retry source changed")
    return segments


def _validated_translation_predecessor(
    work_dir: Path, descriptor: Any, *, source_path: Path | None = None,
    segments: list[Segment] | None = None, route_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if descriptor is None:
        return None
    if (
        not isinstance(descriptor, dict)
        or set(descriptor) != {"receipt", "receipt_sha256", "contract_fingerprint"}
        or not isinstance(descriptor["receipt"], str)
    ):
        raise NativeTranslationRetryError("E_TRANSLATION_PREDECESSOR_INVALID")
    from .translation_exchange import verify_translation_predecessor

    receipt = (work_dir / descriptor["receipt"]).resolve()
    _relative(work_dir, receipt)
    verified = verify_translation_predecessor(
        work_dir, receipt, source_path=source_path, segments=segments,
    )
    if (
        descriptor["receipt_sha256"] != verified["receipt_sha256"]
        or descriptor["contract_fingerprint"] != verified["contract_fingerprint"]
        or (work_dir / verified["receipt"]).resolve() != receipt
        or route_plan is not None and route_plan != verified["native_route_plan"]
    ):
        raise NativeTranslationRetryError("E_TRANSLATION_PREDECESSOR_INVALID")
    return verified


def native_translation_retry_route_plan(
    work_dir: Path, config: dict[str, Any] | None = None, *,
    predecessor_receipt: Path | None = None, retry_state_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Select the current contiguous native block without skipping external routes."""
    from .translation_exchange import configured_translation_route_plan, verify_translation_predecessor

    work_dir = work_dir.resolve()
    if config is None:
        from .workspace import load_config
        config = load_config(work_dir)
    full_plan = configured_translation_route_plan(work_dir, config)
    if predecessor_receipt is not None:
        predecessor_receipt = (predecessor_receipt if predecessor_receipt.is_absolute() else work_dir / predecessor_receipt).resolve()
    descriptor = None
    if retry_state_path is not None:
        path = (retry_state_path if retry_state_path.is_absolute() else work_dir / retry_state_path).resolve()
        _relative(work_dir, path)
        if path.name == STATE_NAME:
            metadata = _load_state(work_dir, path.parent)
            lineage = metadata.get("primary_lineage")
        elif path.name == "lineage.json":
            metadata = json.loads(path.read_text(encoding="utf-8"))
            verified = verify_native_translation_retry_cohort(
                work_dir, metadata, source_path=work_dir / metadata["source_artifact"],
                results_path=work_dir / metadata["results_artifact"],
            )
            lineage = verified["primary_lineage"]
        else:
            raise NativeTranslationRetryError("Native retry state locator is invalid")
        descriptor = lineage.get("translation_predecessor") if isinstance(lineage, dict) else None
    if descriptor is not None:
        bound_receipt = (work_dir / descriptor["receipt"]).resolve()
        if predecessor_receipt is not None and predecessor_receipt.resolve() != bound_receipt:
            raise NativeTranslationRetryError("E_TRANSLATION_PREDECESSOR_INVALID")
        predecessor_receipt = bound_receipt
    if predecessor_receipt is not None:
        verified = verify_translation_predecessor(
            work_dir, predecessor_receipt, full_route_plan=full_plan,
        )
        if descriptor is not None and (
            verified["receipt_sha256"] != descriptor["receipt_sha256"]
            or verified["contract_fingerprint"] != descriptor["contract_fingerprint"]
        ):
            raise NativeTranslationRetryError("E_TRANSLATION_PREDECESSOR_INVALID")
        return _validated_route_plan(verified["native_route_plan"])
    native = []
    for route in full_plan:
        if route["transport"] != "native":
            break
        native.append({key: route[key] for key in ("provider", "model", "max_retries")})
    return _validated_route_plan(native) if native else []


def _load_state(work_dir: Path, state_dir: Path, *, cohort: str | None = None) -> dict[str, Any]:
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
        "content_source_policy",
        "state_fingerprint",
    }
    if (
        not isinstance(state, dict)
        or set(state) - {"deferred_failure_ids", "primary_lineage", "target_progress", "execution_environment_policy", "artifact_sha256", "active_content_source_policy", "candidate_count", "pending_count"} != required
    ):
        raise NativeTranslationRetryError("Native translation retry state is malformed")
    deferred = state.get("deferred_failure_ids", [])
    if not isinstance(deferred, list) or not all(isinstance(item, str) for item in deferred) or len(set(deferred)) != len(deferred):
        raise NativeTranslationRetryError("Native translation retry deferred targets are invalid")
    plan = _validated_route_plan(state["route_plan"])
    progress = state.get("target_progress")
    if progress is not None and (
        not isinstance(progress, dict)
        or any(
            not isinstance(target, str) or not isinstance(row, dict)
            or set(row) != {"route_index", "attempt", "action"}
            or type(row.get("route_index")) is not int
            or not 0 <= row["route_index"] < len(plan)
            or type(row.get("attempt")) is not int or row["attempt"] < 0
            or row.get("action") not in {"native_retry", "external_fallback", "repair_native_runtime"}
            for target, row in progress.items()
        )
    ):
        raise NativeTranslationRetryError("Native retry target routes are invalid")
    fingerprint_payload = dict(state)
    state_fingerprint = fingerprint_payload.pop("state_fingerprint", None)
    if state_fingerprint != stable_hash(fingerprint_payload):
        raise NativeTranslationRetryError(
            "E_CONTENT_SOURCE_POLICY_IMMUTABLE"
        )
    try:
        validate_content_source_policy(state["content_source_policy"])
        active_policy = validate_content_source_policy(state.get("active_content_source_policy", state["content_source_policy"]))
        if active_policy["task_kind"] != "translation" or active_policy["route"] != "native-pass:translation":
            raise ValueError("E_CONTENT_SOURCE_POLICY_INVALID")
    except ValueError as exc:
        raise NativeTranslationRetryError(
            "E_CONTENT_SOURCE_POLICY_IMMUTABLE"
        ) from exc
    lineage = state.get("primary_lineage")
    if lineage is not None:
        lineage_keys = {
            "manifest",
            "manifest_contract_fingerprint",
            "source_fingerprint",
            "execution_namespace",
            "content_source_policy_digest",
            "route_binding",
            "primary_results_sha256",
        }
        binding = lineage.get("route_binding") if isinstance(lineage, dict) else None
        if (
            not isinstance(lineage, dict)
            or set(lineage) - {"translation_predecessor"} != lineage_keys
            or not isinstance(lineage.get("manifest"), str)
            or not lineage["manifest"]
            or lineage.get("source_fingerprint") != state["source_fingerprint"]
            or lineage.get("content_source_policy_digest")
            != state["content_source_policy"]["policy_digest"]
            or not isinstance(binding, dict)
            or set(binding) != {"provider", "model", "attempt"}
            or binding.get("provider") != state["route_plan"][0]["provider"]
            or binding.get("attempt") != 1
            or (
                "model" in state["route_plan"][0]
                and binding.get("model") != state["route_plan"][0]["model"]
            )
            or not isinstance(binding.get("model"), str)
            or not binding["model"]
            or not isinstance(lineage.get("primary_results_sha256"), str)
            or len(lineage["primary_results_sha256"]) != 64
            or not isinstance(state.get("history"), list)
            or not state["history"]
        ):
            raise NativeTranslationRetryError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
        try:
            from .native_pass import _load_task_manifest

            manifest_path = (work_dir / lineage["manifest"]).resolve()
            manifest = _load_task_manifest(work_dir, manifest_path)
            namespace = manifest_path.parent.relative_to(work_dir).as_posix()
        except (OSError, ValueError, KeyError, TypeError):
            raise NativeTranslationRetryError(
                "E_CONTENT_SOURCE_POLICY_IMMUTABLE"
            ) from None
        if (
            manifest.get("pass_kind", "translation") != "translation"
            or manifest.get("source_fingerprint") != state["source_fingerprint"]
            or manifest.get("contract_fingerprint")
            != lineage["manifest_contract_fingerprint"]
            or manifest.get("execution_namespace") != namespace
            or lineage.get("execution_namespace") != namespace
            or manifest.get("content_source_policy")
            != state["content_source_policy"]
            or manifest.get("route_binding") != binding
            or manifest.get("translation_predecessor") != lineage.get("translation_predecessor")
            or state["history"][0].get("result_sha256")
            != lineage["primary_results_sha256"]
        ):
            raise NativeTranslationRetryError(
                "E_CONTENT_SOURCE_POLICY_IMMUTABLE"
            )
        _validated_translation_predecessor(
            work_dir, lineage.get("translation_predecessor"),
            source_path=work_dir / state["source"], route_plan=plan,
        )
    if (
        state.get("schema") != RETRY_SCHEMA
        or not isinstance(state.get("route_index"), int)
        or isinstance(state.get("route_index"), bool)
        or not 0 <= state["route_index"] < len(plan)
        or state.get("next_action") not in {"native_retry", "external_fallback", "complete", "repair_native_runtime"}
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
    if cohort not in {"successful", "native_exhausted"} and (not pending_path.is_file() or _sha256(pending_path) != state["pending_sha256"]):
        raise NativeTranslationRetryError("Native translation retry pending input changed")
    artifacts = state.get("artifact_sha256", {})
    if not isinstance(artifacts, dict):
        raise NativeTranslationRetryError("Native retry artifact bindings are invalid")
    selected_artifacts = None
    if cohort == "successful":
        selected_artifacts = {state["candidate_artifact"], _relative(work_dir, state_dir / "successful-targets.jsonl")}
    elif cohort == "native_exhausted":
        selected_artifacts = {_relative(work_dir, state_dir / f"native-exhausted-{kind}.jsonl") for kind in ("targets", "results")}
    for relative, digest in artifacts.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise NativeTranslationRetryError("Native retry artifact bindings are invalid")
        if selected_artifacts is not None and relative not in selected_artifacts:
            continue
        path = work_dir / relative
        _relative(work_dir, path)
        if not path.is_file() or _sha256(path) != digest:
            raise NativeTranslationRetryError("Native retry cohort or locked result changed")
    if cohort not in {"successful", "native_exhausted"} and "target_progress" not in state and state["history"] and state["history"][-1].get("transport_pending_count", 0):
        source_segments = _bound_source_segments(work_dir, state)
        route_index = next(index for index, route in enumerate(plan) if route["provider"] == state["last_provider"])
        pending_ids = {row["id"] for row in _read_jsonl(pending_path)}
        state["target_progress"] = {
            target: {"route_index": route_index, "attempt": state["last_attempt"], "action": "repair_native_runtime"}
            for target in pending_ids
        }
        state["target_progress"].update({
            target: _target_successor(plan, route_index, state["last_attempt"], operational=None, policy=None)
            for target in deferred
        })
        state["execution_environment_policy"] = None
        state["history"].append({"event": "legacy_recovery_migration", "execution_complete": False, "success_count": 0})
        candidates = {row["target_id"]: row for row in _read_jsonl(work_dir / state["candidate_artifact"])}
        pending = _select_pending_cohort(state, source_segments)
        _write_stage(work_dir, state_dir, source_segments, candidates, pending, state)
    return state


def _state_summary(work_dir: Path, state_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    pending_count = state["pending_count"] if "pending_count" in state else len(_read_jsonl(work_dir / state["pending_artifact"]))
    candidate_count = state["candidate_count"] if "candidate_count" in state else len(_read_jsonl(work_dir / state["candidate_artifact"]))
    # A transport repair retains its semantic ordinal but needs fresh output
    # locations; reusing that ordinal's completed manifest would replay errors.
    run_name = f"run-{len(state['history']):04d}"
    return {
        "schema": RETRY_SCHEMA,
        "state": _relative(work_dir, state_dir / STATE_NAME),
        "next_action": state["next_action"],
        "next_provider": state["next_provider"],
        "next_attempt": state["next_attempt"],
        "pending_count": pending_count,
        "deferred_failure_count": len(state.get("deferred_failure_ids", [])),
        "unresolved_count": pending_count + len(state.get("deferred_failure_ids", [])),
        "provider_attempt_consumed": state["history"][-1].get("execution_complete", True),
        "candidate_count": candidate_count,
        "pending_input": state["pending_artifact"],
        "pre_fallback_results": state["final_results"],
        "content_source_policy": state.get("active_content_source_policy", state["content_source_policy"]),
        "route_plan": state["route_plan"],
        "successful_input": _relative(work_dir, state_dir / "successful-targets.jsonl"),
        "successful_results": state["candidate_artifact"],
        "native_exhausted_input": _relative(work_dir, state_dir / "native-exhausted-targets.jsonl"),
        "native_exhausted_results": _relative(work_dir, state_dir / "native-exhausted-results.jsonl"),
        "repair_input": _relative(work_dir, state_dir / "repair-targets.jsonl"),
        "repair_results": _relative(work_dir, state_dir / "repair-results.jsonl"),
        "native_exhausted_count": sum(
            row["action"] == "external_fallback"
            for row in state.get("target_progress", {}).values()
        ),
        "cohorts": [
            {
                "provider": state["route_plan"][index]["provider"],
                "attempt": attempt,
                "count": sum(row == {"route_index": index, "attempt": attempt, "action": "native_retry"} for row in state["target_progress"].values()),
                "input": _relative(work_dir, state_dir / "cohorts" / f"route-{index}-attempt-{attempt}.jsonl"),
            }
            for index, attempt in dict.fromkeys(
                (row["route_index"], row["attempt"])
                for row in state.get("target_progress", {}).values() if row["action"] == "native_retry"
            )
        ],
        "execution_environment_policy": state.get("execution_environment_policy"),
        "repair_needed_count": sum(
            row["action"] == "repair_native_runtime"
            for row in state.get("target_progress", {}).values()
        ),
        "repair_actions": (
            ["repair_native_runtime", "extend_approved_native_routes"]
            if any(row["action"] == "repair_native_runtime" for row in state.get("target_progress", {}).values())
            else []
        ),
        **(
            {"primary_lineage": state["primary_lineage"]}
            if "primary_lineage" in state
            else {}
        ),
        "native_pass_output": _relative(work_dir, state_dir / "native-passes" / run_name),
        "native_pass_results": _relative(work_dir, state_dir / "native-passes" / (run_name + "-results")),
    }


def _retry_content_policies(state: dict[str, Any]) -> list[dict[str, Any]]:
    policies = [state["content_source_policy"]]
    policies.extend(event["content_source_policy"] for event in state["history"] if "content_source_policy" in event)
    if "active_content_source_policy" in state:
        policies.append(state["active_content_source_policy"])
    return list({policy["policy_digest"]: validate_content_source_policy(policy) for policy in policies}.values())


def export_native_translation_retry_cohort(
    work_dir: Path, state_dir: Path, *, cohort: str,
) -> dict[str, Any]:
    """Freeze a selected cohort without changing the root launch lineage."""
    work_dir, state_dir = work_dir.resolve(), state_dir.resolve()
    state = _load_state(work_dir, state_dir, cohort=cohort)
    summary = _state_summary(work_dir, state_dir, state)
    if cohort == "all":
        if state["next_action"] not in {"complete", "external_fallback"}:
            raise NativeTranslationRetryError("Unfinished or repair-needed targets cannot enter full-set follow-up")
        source_path, results_path = work_dir / state["source"], work_dir / state["final_results"]
    elif cohort in {"successful", "native_exhausted"}:
        source_path, results_path = work_dir / summary[cohort + "_input"], work_dir / summary[cohort + "_results"]
    else:
        raise NativeTranslationRetryError("Unsupported native retry cohort")
    segments = _bound_source_segments(work_dir, state) if cohort == "all" else read_segments(source_path)
    if not segments:
        raise NativeTranslationRetryError("Selected native retry cohort is empty")
    source_body = "".join(json.dumps(asdict(item), ensure_ascii=False, sort_keys=True) + "\n" for item in segments).encode("utf-8")
    try:
        root_segments = segments if cohort == "all" else _bound_source_segments(work_dir, state)
    except NativeTranslationRetryError:
        root_segments = None
    root_body = (
        "".join(json.dumps(asdict(item), ensure_ascii=False, sort_keys=True) + "\n" for item in root_segments).encode("utf-8")
        if root_segments is not None else None
    )
    results_body = results_path.read_bytes()
    history_body = (json.dumps({
        "schema": "parr0t.native-translation-retry-history.v1",
        "history": state["history"],
        "target_ids": [item.id for item in segments],
        "translation_predecessor": (state.get("primary_lineage") or {}).get("translation_predecessor"),
    }, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    base = {
        "schema": "parr0t.native-translation-retry-cohort.v1",
        "kind": cohort, "state": _relative(work_dir, state_dir / STATE_NAME),
        "state_fingerprint": state["state_fingerprint"],
        "root_source_fingerprint": state["source_fingerprint"],
        "source_fingerprint": stable_hash_sequence(asdict(item) for item in segments),
        "source_sha256": hashlib.sha256(source_body).hexdigest(),
        "results_sha256": hashlib.sha256(results_body).hexdigest(),
        "attempt_history_sha256": hashlib.sha256(history_body).hexdigest(),
        **({"root_source_sha256": hashlib.sha256(root_body).hexdigest()} if root_body is not None else {}),
        "primary_lineage": state.get("primary_lineage"),
        "route_plan": state["route_plan"],
        "content_source_policies": _retry_content_policies(state),
        "content_source_policy": state.get("active_content_source_policy", state["content_source_policy"]),
    }
    directory = state_dir / "cohort-exports" / stable_hash(base)
    source_export, results_export = directory / "source.jsonl", directory / "results.jsonl"
    unsigned = {
        **base, "source_artifact": _relative(work_dir, source_export),
        "results_artifact": _relative(work_dir, results_export),
        "export_path": _relative(work_dir, directory / "lineage.json"),
        "attempt_history_artifact": _relative(work_dir, directory / "attempt-history.json"),
        **({"root_source_artifact": _relative(work_dir, directory / "root-source.jsonl")} if root_body is not None else {}),
    }
    lineage = {**unsigned, "contract_fingerprint": stable_hash(unsigned)}
    for path, body in (
        (source_export, source_body), (results_export, results_body), (directory / "attempt-history.json", history_body),
        *(((directory / "root-source.jsonl", root_body),) if root_body is not None else ()),
    ):
        if path.exists():
            if path.read_bytes() != body:
                raise NativeTranslationRetryError("Immutable native retry cohort changed")
        else:
            _atomic_bytes(path, body)
    export_path = work_dir / lineage["export_path"]
    if export_path.exists():
        if json.loads(export_path.read_text(encoding="utf-8")) != lineage:
            raise NativeTranslationRetryError("Immutable native retry cohort lineage changed")
    else:
        _atomic_json(export_path, lineage)
    return {
        **summary, "cohort_lineage": lineage,
        "cohort_input": lineage["source_artifact"], "cohort_results": lineage["results_artifact"],
        "content_source_policies": lineage["content_source_policies"],
    }


def verify_native_translation_retry_cohort(
    work_dir: Path, cohort_lineage: dict[str, Any], *, source_path: Path, results_path: Path,
) -> dict[str, Any]:
    """Validate only the immutable selected export, not unrelated mutable cohorts."""
    work_dir = work_dir.resolve()
    if not isinstance(cohort_lineage, dict):
        raise NativeTranslationRetryError("Native retry cohort lineage is invalid")
    unsigned = dict(cohort_lineage)
    fingerprint = unsigned.pop("contract_fingerprint", None)
    required = {
        "schema", "kind", "state", "state_fingerprint", "root_source_fingerprint",
        "source_fingerprint", "source_sha256", "results_sha256", "primary_lineage",
        "route_plan", "content_source_policies", "source_artifact", "results_artifact", "export_path",
        "content_source_policy",
    }
    if (
        set(unsigned) - {"attempt_history_artifact", "attempt_history_sha256", "root_source_artifact", "root_source_sha256"} != required
        or fingerprint != stable_hash(unsigned)
        or ("attempt_history_artifact" in unsigned) != ("attempt_history_sha256" in unsigned)
        or ("root_source_artifact" in unsigned) != ("root_source_sha256" in unsigned)
        or unsigned["schema"] != "parr0t.native-translation-retry-cohort.v1"
        or unsigned["kind"] not in {"all", "successful", "native_exhausted"}
    ):
        raise NativeTranslationRetryError("Native retry cohort lineage is invalid")
    base = {
        key: value for key, value in unsigned.items()
        if key not in {"source_artifact", "results_artifact", "export_path", "attempt_history_artifact", "root_source_artifact"}
    }
    state_path = work_dir / unsigned["state"]
    _relative(work_dir, state_path)
    if state_path.name != STATE_NAME:
        raise NativeTranslationRetryError("Native retry cohort state locator is invalid")
    directory = state_path.parent / "cohort-exports" / stable_hash(base)
    expected_paths = {
        "source_artifact": directory / "source.jsonl",
        "results_artifact": directory / "results.jsonl",
        "export_path": directory / "lineage.json",
    }
    if "attempt_history_artifact" in unsigned:
        expected_paths["attempt_history_artifact"] = directory / "attempt-history.json"
    if "root_source_artifact" in unsigned:
        expected_paths["root_source_artifact"] = directory / "root-source.jsonl"
        digest = unsigned["root_source_sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise NativeTranslationRetryError("Native retry frozen root inventory binding is invalid")
    for key, path in expected_paths.items():
        if unsigned[key] != _relative(work_dir, path):
            raise NativeTranslationRetryError("Native retry cohort artifact locator changed")
    if source_path.resolve() != expected_paths["source_artifact"].resolve() or results_path.resolve() != expected_paths["results_artifact"].resolve():
        raise NativeTranslationRetryError("Native retry follow-up does not select the bound cohort")
    if (
        json.loads(expected_paths["export_path"].read_text(encoding="utf-8")) != cohort_lineage
        or _sha256(source_path) != unsigned["source_sha256"]
        or _sha256(results_path) != unsigned["results_sha256"]
        or stable_hash_sequence(asdict(item) for item in read_segments(source_path)) != unsigned["source_fingerprint"]
    ):
        raise NativeTranslationRetryError("Native retry selected cohort changed")
    if "attempt_history_artifact" in unsigned and _sha256(expected_paths["attempt_history_artifact"]) != unsigned["attempt_history_sha256"]:
        raise NativeTranslationRetryError("Native retry frozen attempt history changed")
    policies = [validate_content_source_policy(policy) for policy in unsigned["content_source_policies"]]
    active_policy = validate_content_source_policy(unsigned["content_source_policy"])
    if active_policy not in policies:
        raise NativeTranslationRetryError("Native retry active cohort policy is not in its bound history")
    plan = _validated_route_plan(unsigned["route_plan"])
    lineage = unsigned["primary_lineage"]
    if lineage is not None:
        from .native_pass import _load_task_manifest

        manifest = _load_task_manifest(work_dir, work_dir / lineage["manifest"])
        if (
            lineage["source_fingerprint"] != unsigned["root_source_fingerprint"]
            or manifest["source_fingerprint"] != unsigned["root_source_fingerprint"]
            or manifest["contract_fingerprint"] != lineage["manifest_contract_fingerprint"]
            or manifest.get("route_binding") != lineage["route_binding"]
            or manifest.get("execution_namespace") != lineage["execution_namespace"]
            or manifest["content_source_policy"]["policy_digest"] != lineage["content_source_policy_digest"]
            or manifest["content_source_policy"] not in policies
            or manifest.get("translation_predecessor") != lineage.get("translation_predecessor")
        ):
            raise NativeTranslationRetryError("Native retry root primary lineage changed")
        _validated_translation_predecessor(
            work_dir, lineage.get("translation_predecessor"), route_plan=plan,
        )
    return {
        "cohort_lineage": cohort_lineage, "content_source_policies": policies,
        "primary_lineage": lineage, "route_plan": plan,
        "content_source_policy": active_policy,
        **{key: unsigned[key] for key in ("root_source_artifact", "root_source_sha256") if key in unsigned},
    }


def load_native_translation_retry_cohort_root_source(
    work_dir: Path, cohort_lineage: dict[str, Any],
) -> list[Segment]:
    """Validate the full-root body only when a consumer needs that inventory."""
    work_dir = work_dir.resolve()
    verify_native_translation_retry_cohort(
        work_dir, cohort_lineage,
        source_path=work_dir / cohort_lineage["source_artifact"],
        results_path=work_dir / cohort_lineage["results_artifact"],
    )
    if "root_source_artifact" not in cohort_lineage:
        raise NativeTranslationRetryError("Native retry frozen root inventory is unavailable")
    try:
        root_segments = [
            Segment(**row) for row in _read_jsonl(
                work_dir / cohort_lineage["root_source_artifact"],
                expected_sha256=cohort_lineage["root_source_sha256"],
            )
        ]
    except (NativeTranslationRetryError, OSError, UnicodeError, TypeError, ValueError) as exc:
        raise NativeTranslationRetryError("Native retry frozen root inventory changed") from exc
    selected = read_segments(work_dir / cohort_lineage["source_artifact"])
    selected_ids = {item.id for item in selected}
    if (
        stable_hash_sequence(asdict(item) for item in root_segments) != cohort_lineage["root_source_fingerprint"]
        or [asdict(item) for item in root_segments if item.id in selected_ids] != [asdict(item) for item in selected]
    ):
        raise NativeTranslationRetryError("Native retry frozen root inventory changed")
    return root_segments


def load_native_translation_retry_cohort_history(
    work_dir: Path, cohort_lineage: dict[str, Any],
) -> dict[str, Any]:
    """Read private consumed-ordinal evidence for Core transport continuation only."""
    work_dir = work_dir.resolve()
    verify_native_translation_retry_cohort(
        work_dir, cohort_lineage,
        source_path=work_dir / cohort_lineage["source_artifact"],
        results_path=work_dir / cohort_lineage["results_artifact"],
    )
    relative = cohort_lineage.get("attempt_history_artifact")
    if not isinstance(relative, str):
        raise NativeTranslationRetryError("E_NATIVE_RETRY_HISTORY_REQUIRED")
    payload = json.loads((work_dir / relative).read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema", "history", "target_ids", "translation_predecessor"}
        or payload["schema"] != "parr0t.native-translation-retry-history.v1"
        or not isinstance(payload["history"], list)
        or not all(isinstance(event, dict) for event in payload["history"])
        or payload["target_ids"] != [item.id for item in read_segments(work_dir / cohort_lineage["source_artifact"])]
        or payload["translation_predecessor"] != (cohort_lineage.get("primary_lineage") or {}).get("translation_predecessor")
    ):
        raise NativeTranslationRetryError("Native retry frozen attempt history is invalid")
    return payload


def load_native_translation_retry_cohort_successes(
    work_dir: Path, cohort_lineage: dict[str, Any], *, segments: list[Segment],
) -> dict[str, dict[str, Any]]:
    """Recover locked native rows from immutable history, never mutable caches."""
    work_dir = work_dir.resolve()
    history = load_native_translation_retry_cohort_history(work_dir, cohort_lineage)["history"]
    if (
        not history or len({item.id for item in segments}) != len(segments)
        or stable_hash_sequence(asdict(item) for item in segments) != cohort_lineage["root_source_fingerprint"]
    ):
        raise NativeTranslationRetryError("Native retry success recovery requires the complete bound root inventory")
    segment_by_id = {item.id: item for item in segments}
    policies = {
        policy["policy_digest"]: validate_content_source_policy(policy)
        for policy in cohort_lineage["content_source_policies"]
    }
    routes = {item["provider"]: item for item in _validated_route_plan(cohort_lineage["route_plan"])}
    state_dir = (work_dir / cohort_lineage["state"]).parent
    primary = cohort_lineage.get("primary_lineage")
    locked: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(history):
        if "event" in event:
            kind = event["event"]
            fields = {"event", "execution_complete", "success_count"}
            if kind == "content_scope_revision":
                fields.add("content_source_policy")
                policy = validate_content_source_policy(event.get("content_source_policy"))
                if policies.get(policy["policy_digest"]) != policy:
                    raise NativeTranslationRetryError("Native retry historical policy revision changed")
            elif kind == "approved_route_extension":
                fields.add("target_ids")
                targets = event.get("target_ids")
                if (
                    not isinstance(targets, list) or not targets
                    or not all(isinstance(target, str) and target in segment_by_id for target in targets)
                    or len(set(targets)) != len(targets)
                ):
                    raise NativeTranslationRetryError("Native retry historical route extension changed")
            elif kind != "legacy_recovery_migration":
                raise NativeTranslationRetryError("Native retry historical control event is unknown")
            if (
                index == 0 or set(event) != fields or event["execution_complete"] is not False
                or type(event["success_count"]) is not int or event["success_count"] != 0
            ):
                raise NativeTranslationRetryError("Native retry historical control event binding is invalid")
            continue
        provider = event.get("provider")
        route = routes.get(provider) if isinstance(provider, str) else None
        model = event.get("model", route.get("model") if route else None)
        target_ids = event.get("target_ids")
        digest = event.get("result_sha256")
        relative = event.get("result_artifact")
        if (
            route is None or not isinstance(model, str) or not model
            or ("model" in route and route["model"] != model)
            or type(event.get("attempt")) is not int or event["attempt"] < 1
            or not isinstance(target_ids, list) or not target_ids
            or not all(isinstance(item, str) and item in segment_by_id for item in target_ids)
            or len(set(target_ids)) != len(target_ids)
            or not isinstance(digest, str) or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not isinstance(relative, str) or not relative
        ):
            raise NativeTranslationRetryError("Native retry immutable result history binding is invalid")
        selected_ids = set(target_ids)
        if target_ids != [item.id for item in segments if item.id in selected_ids]:
            raise NativeTranslationRetryError("Native retry history target order changed")
        if index == 0 and (
            target_ids != [item.id for item in segments]
            or provider != cohort_lineage["route_plan"][0]["provider"]
            or event["attempt"] != 1
            or (primary is not None and (
                digest != primary["primary_results_sha256"]
                or model != primary["route_binding"]["model"]
            ))
        ):
            raise NativeTranslationRetryError("Native retry history does not bind the original primary result")
        path = (work_dir / relative).resolve()
        _relative(work_dir, path)
        if path != (state_dir / "attempt-results" / f"{digest}.jsonl").resolve():
            raise NativeTranslationRetryError("Native retry immutable result artifact locator changed")
        successes, _ = _validate_result_rows(
            [segment_by_id[item] for item in target_ids], path,
            expected_provider=provider, allow_legacy_native_provider=index == 0,
            expected_model=model, expected_sha256=digest,
        )
        for row in successes:
            provenance = row.get("content_provenance")
            policy = policies.get(provenance.get("policy_digest")) if isinstance(provenance, dict) else None
            if policy is None or (row.get("model") is not None and row["model"] != model):
                raise NativeTranslationRetryError("Native retry historical success policy or model changed")
            row["content_provenance"] = validate_output_provenance(
                provenance, policy=policy, model=model, route=provider,
            )
            locked.setdefault(row["target_id"], row)
    return locked


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
    failure_ids = {segment.id for segment in pending} | set(state.get("deferred_failure_ids", []))
    final_rows = []
    last_providers = {}
    for event in state["history"]:
        if "provider" in event:
            last_providers.update({target: event["provider"] for target in event.get("target_ids", [])})
    for segment in source_segments:
        if segment.id in candidates:
            final_rows.append(candidates[segment.id])
        elif segment.id in failure_ids:
            final_rows.append(
                {
                    "target_id": segment.id,
                    "status": "error",
                    "text": "",
                    "provider": last_providers.get(segment.id, "native-control"),
                    "issues": [{
                        "native_retry": "native_retry_pending",
                        "repair_native_runtime": "native_route_repair_required",
                        "external_fallback": "pre_fallback_routes_exhausted",
                    }[state.get("target_progress", {}).get(segment.id, {}).get("action", "external_fallback")]],
                }
            )
    _atomic_jsonl(final_path, final_rows)
    progress = state.get("target_progress", {})
    _atomic_jsonl(
        state_dir / "successful-targets.jsonl",
        (asdict(item) for item in source_segments if item.id in candidates),
    )
    for action, stem in (("external_fallback", "native-exhausted"), ("repair_native_runtime", "repair")):
        ids = {target for target, row in progress.items() if row["action"] == action}
        _atomic_jsonl(state_dir / f"{stem}-targets.jsonl", (asdict(item) for item in source_segments if item.id in ids))
        _atomic_jsonl(state_dir / f"{stem}-results.jsonl", (row for row in final_rows if row["target_id"] in ids))
    for index, attempt in dict.fromkeys(
        (row["route_index"], row["attempt"]) for row in progress.values()
        if row["action"] == "native_retry"
    ):
        binding = {"route_index": index, "attempt": attempt, "action": "native_retry"}
        _atomic_jsonl(
            state_dir / "cohorts" / f"route-{index}-attempt-{attempt}.jsonl",
            (asdict(item) for item in source_segments if progress.get(item.id) == binding),
        )
    artifacts = [
        candidate_path, pending_path, final_path, state_dir / "successful-targets.jsonl",
        *(state_dir / f"{stem}-{kind}.jsonl"
          for stem in ("native-exhausted", "repair") for kind in ("targets", "results")),
        *(state_dir / "cohorts" / f"route-{index}-attempt-{attempt}.jsonl"
          for index, attempt in dict.fromkeys(
              (row["route_index"], row["attempt"]) for row in progress.values()
              if row["action"] == "native_retry"
          )),
        *(work_dir / event["result_artifact"] for event in state["history"] if "result_artifact" in event),
    ]
    state["artifact_sha256"] = {_relative(work_dir, path): _sha256(path) for path in artifacts}
    state.update(
        {
            "candidate_artifact": _relative(work_dir, candidate_path),
            "pending_artifact": _relative(work_dir, pending_path),
            "pending_sha256": _sha256(pending_path),
            "final_results": _relative(work_dir, final_path),
            "candidate_count": len(candidates),
            "pending_count": len(pending),
        }
    )
    state.pop("state_fingerprint", None)
    state["state_fingerprint"] = stable_hash(state)
    _atomic_json(state_dir / STATE_NAME, state)
    return _state_summary(work_dir, state_dir, state)


def start_native_translation_retries(
    work_dir: Path,
    source_path: Path,
    segments: list[Segment],
    first_results: Path,
    state_dir: Path,
    *,
    route_plan: Iterable[dict[str, Any]] | None = None,
    content_policy: dict[str, Any] | None = None,
    primary_lineage: dict[str, Any] | None = None,
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
    predecessor = _validated_translation_predecessor(
        work_dir, primary_lineage.get("translation_predecessor") if primary_lineage else None,
        source_path=source_path, segments=segments,
    )
    plan = _validated_route_plan(
        route_plan if route_plan is not None
        else predecessor["native_route_plan"] if predecessor is not None
        else native_translation_retry_route_plan(work_dir)
    )
    if any("model" not in route for route in plan):
        raise NativeTranslationRetryError("E_NATIVE_RETRY_ROUTE_BINDING_REQUIRED: each approved route needs its exact model")
    if predecessor is not None and plan != predecessor["native_route_plan"]:
        raise NativeTranslationRetryError("Native retry plan does not select the predecessor's next native block")
    if content_policy is None:
        content_policy = content_source_policy(
            work_dir,
            task_kind="translation",
            route="native-pass:translation",
        )
    else:
        content_policy = validate_content_source_policy(content_policy)
        if (
            content_policy["task_kind"] != "translation"
            or content_policy["route"] != "native-pass:translation"
        ):
            raise NativeTranslationRetryError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    if primary_lineage is not None:
        route_binding = primary_lineage.get("route_binding")
        if (
            not isinstance(route_binding, dict)
            or route_binding.get("provider") != str(plan[0]["provider"])
            or route_binding.get("attempt") != 1
            or not isinstance(route_binding.get("model"), str)
            or not route_binding["model"]
            or route_binding.get("model") != plan[0]["model"]
            or primary_lineage.get("source_fingerprint")
            != stable_hash_sequence(asdict(segment) for segment in segments)
            or primary_lineage.get("content_source_policy_digest")
            != content_policy["policy_digest"]
        ):
            raise NativeTranslationRetryError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
        primary_lineage = {
            **primary_lineage,
            "primary_results_sha256": _sha256(first_results),
        }
    first_model = plan[0]["model"]
    if state_dir.exists() and any(state_dir.iterdir()):
        state = _load_state(work_dir, state_dir)
        expected = stable_hash_sequence(asdict(segment) for segment in segments)
        if (
            state["source_fingerprint"] != expected
            or not _route_plans_match(state["route_plan"], plan)
            or not state["history"]
            or state["history"][0].get("result_sha256") != _sha256(first_results)
            or state["content_source_policy"] != content_policy
            or state.get("primary_lineage") != primary_lineage
        ):
            raise NativeTranslationRetryError("Existing native retry state is stale")
        return {**_state_summary(work_dir, state_dir, state), "cached": True}
    successes, failures = _validate_result_rows(
        segments,
        first_results,
        expected_provider=str(plan[0]["provider"]),
        allow_legacy_native_provider=True,
        content_policy=content_policy,
        expected_model=first_model,
    )
    candidates = {row["target_id"]: row for row in successes}
    from .native_runtime import active_execution_environment_policy

    execution_policy = active_execution_environment_policy(work_dir)
    first_results = _preserve_attempt_results(state_dir, first_results)
    result_rows = _read_jsonl(first_results)
    transport_ids = _operational_targets(result_rows)
    incomplete_ids = {
        row["target_id"] for row in result_rows
        if row.get("status") == "error" and row.get("provider") in {"native-control", "native-worker"}
    } | set(transport_ids)
    progress = {
        segment.id: _target_successor(
            plan, 0, 1, operational=transport_ids.get(segment.id), policy=execution_policy,
            repair_required=segment.id in incomplete_ids,
        )
        for segment in failures
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": RETRY_SCHEMA,
        "source": _relative(work_dir, source_path),
        "source_fingerprint": stable_hash_sequence(asdict(segment) for segment in segments),
        "route_plan": plan,
        "content_source_policy": content_policy,
        "execution_environment_policy": execution_policy,
        "target_progress": progress,
        "route_index": 0,
        "last_provider": str(plan[0]["provider"]),
        "last_attempt": 1,
        "next_provider": "",
        "next_attempt": 0,
        "next_action": "complete",
        "candidate_artifact": "",
        "pending_artifact": "",
        "pending_sha256": "",
        "final_results": "",
        "history": [
            {
                "provider": str(plan[0]["provider"]),
                "model": first_model,
                "attempt": 1,
                "result_sha256": _sha256(first_results),
                "result_artifact": _relative(work_dir, first_results),
                "target_ids": [segment.id for segment in segments],
                "semantic_attempt_consumed_ids": [segment.id for segment in segments if segment.id not in incomplete_ids],
                "incomplete_execution_target_ids": sorted(incomplete_ids),
                "operational_target_ids": sorted(transport_ids),
                "success_count": len(successes),
                "failure_count": len(failures),
                "transport_pending_count": len(incomplete_ids),
                "operational_failure_codes": transport_ids,
                "deferred_failure_count": 0,
                "execution_complete": not bool(incomplete_ids),
            }
        ],
        "deferred_failure_ids": [],
        **(
            {"primary_lineage": primary_lineage}
            if primary_lineage is not None
            else {}
        ),
    }
    failures = _select_pending_cohort(state, segments)
    return {
        **_write_stage(work_dir, state_dir, segments, candidates, failures, state),
        "cached": False,
    }


def advance_native_translation_retries(
    work_dir: Path, state_dir: Path, attempt_results: Path, *,
    route_plan: Iterable[dict[str, Any]] | None = None,
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
    if route_plan is not None and not _route_plans_match(
        state["route_plan"], _validated_route_plan(route_plan)
    ):
        raise NativeTranslationRetryError("Configured native translation routes changed")
    if state["next_action"] != "native_retry":
        raise NativeTranslationRetryError("No native translation retry is pending")
    source_segments = _bound_source_segments(work_dir, state)
    attempt_results = _preserve_attempt_results(state_dir, attempt_results)
    pending_segments = [
        Segment(**row) for row in _read_jsonl(work_dir / state["pending_artifact"])
    ]
    plan = _validated_route_plan(state["route_plan"])
    current_route = plan[int(state["route_index"])]
    if not isinstance(current_route.get("model"), str) or not current_route["model"]:
        raise NativeTranslationRetryError("E_NATIVE_RETRY_ROUTE_BINDING_REQUIRED: active route needs its exact approved model")
    successes, failures = _validate_result_rows(
        pending_segments,
        attempt_results,
        expected_provider=str(state["next_provider"]),
        content_policy=state.get("active_content_source_policy", state["content_source_policy"]),
        expected_model=current_route["model"],
    )
    candidate_rows = _read_jsonl(work_dir / state["candidate_artifact"])
    candidates = {row["target_id"]: row for row in candidate_rows}
    for row in successes:
        if row["target_id"] in candidates:
            raise NativeTranslationRetryError("A locked translation was reprocessed")
        candidates[row["target_id"]] = row

    provider = state["next_provider"]
    attempt = state["next_attempt"]
    route_index = int(state["route_index"])
    if provider != plan[route_index]["provider"]:
        raise NativeTranslationRetryError("Native translation retry route binding changed")
    result_rows = _read_jsonl(attempt_results)
    transport_ids = _operational_targets(result_rows)
    incomplete_ids = {
        row["target_id"] for row in result_rows
        if row.get("status") == "error" and row.get("provider") in {"native-control", "native-worker"}
    } | set(transport_ids)
    if "target_progress" not in state:
        state["target_progress"] = {
            segment.id: {"route_index": route_index, "attempt": attempt, "action": "native_retry"}
            for segment in source_segments
            if segment.id in {item.id for item in pending_segments} | set(state.get("deferred_failure_ids", []))
        }
    progress = state["target_progress"]
    for row in successes:
        progress.pop(row["target_id"], None)
    for segment in failures:
        progress[segment.id] = _target_successor(
            plan, route_index, attempt, operational=transport_ids.get(segment.id),
            policy=state.get("execution_environment_policy"),
            repair_required=segment.id in incomplete_ids,
        )
    state.pop("state_fingerprint", None)
    state.update(last_provider=provider, last_attempt=attempt)
    failures = _select_pending_cohort(state, source_segments)
    state["history"].append(
        {
            "provider": provider,
            "model": current_route["model"],
            "attempt": attempt,
            "result_sha256": _sha256(attempt_results),
            "result_artifact": _relative(work_dir, attempt_results),
            "target_ids": [segment.id for segment in pending_segments],
            "semantic_attempt_consumed_ids": [segment.id for segment in pending_segments if segment.id not in incomplete_ids],
            "incomplete_execution_target_ids": sorted(incomplete_ids),
            "operational_target_ids": sorted(transport_ids),
            "success_count": len(successes),
            "failure_count": len(progress),
            "transport_pending_count": len(incomplete_ids),
            "operational_failure_codes": transport_ids,
            "deferred_failure_count": len(state["deferred_failure_ids"]),
            "execution_complete": not bool(incomplete_ids),
        }
    )
    return _write_stage(
        work_dir, state_dir, source_segments, candidates, failures, state
    )


def revise_native_translation_retry_content(
    work_dir: Path, state_dir: Path, *, grant_path: Path, scope_id: str,
) -> dict[str, Any]:
    """Bind a validated explicit grant to unfinished work, never rewrite prior candidates."""
    work_dir, state_dir = work_dir.resolve(), state_dir.resolve()
    _relative(work_dir, state_dir)
    state = _load_state(work_dir, state_dir)
    summary = _state_summary(work_dir, state_dir, state)
    if state["next_action"] == "complete":
        raise NativeTranslationRetryError("No unfinished translation scope remains")
    if (work_dir / summary["native_pass_output"] / "manifest.json").exists():
        raise NativeTranslationRetryError("E_NATIVE_RETRY_ATTEMPT_ACTIVE: collect the current bound attempt before revising scope")
    proposed = content_source_policy(
        work_dir, task_kind="translation", route="native-pass:translation",
        scope_id=scope_id, grant_path=grant_path,
    )
    if proposed == summary["content_source_policy"]:
        return {**summary, "cached": True}
    source_segments = _bound_source_segments(work_dir, state)
    state["active_content_source_policy"] = proposed
    state["history"].append({
        "event": "content_scope_revision", "content_source_policy": proposed,
        "execution_complete": False, "success_count": 0,
    })
    candidates = {row["target_id"]: row for row in _read_jsonl(work_dir / state["candidate_artifact"])}
    if "target_progress" in state:
        pending = _select_pending_cohort(state, source_segments)
    else:
        pending = [Segment(**row) for row in _read_jsonl(work_dir / state["pending_artifact"])]
    return {
        **_write_stage(work_dir, state_dir, source_segments, candidates, pending, state),
        "cached": False,
    }


def repair_native_translation_retries(
    work_dir: Path, state_dir: Path, *, route_plan: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Bind an explicitly approved route extension for operationally unfinished targets."""
    from .native_runtime import active_execution_environment_policy

    work_dir, state_dir = work_dir.resolve(), state_dir.resolve()
    _relative(work_dir, state_dir)
    state = _load_state(work_dir, state_dir)
    active_manifest = work_dir / _state_summary(work_dir, state_dir, state)["native_pass_output"] / "manifest.json"
    if state["next_action"] == "native_retry" and active_manifest.exists():
        raise NativeTranslationRetryError("E_NATIVE_RETRY_ATTEMPT_ACTIVE: collect the current bound attempt before revising routes")
    plan = _validated_route_plan(route_plan)
    if any("model" not in route for route in plan):
        raise NativeTranslationRetryError("E_NATIVE_RETRY_ROUTE_BINDING_REQUIRED: each approved route needs its exact model")
    previous = state["route_plan"]
    if plan[:len(previous)] != previous:
        raise NativeTranslationRetryError("Existing native route bindings cannot be rewritten")
    _validated_translation_predecessor(
        work_dir, (state.get("primary_lineage") or {}).get("translation_predecessor"), route_plan=plan,
    )
    policy = active_execution_environment_policy(work_dir)
    progress = state.get("target_progress", {})
    failure_codes = {}
    for event in state["history"]:
        failure_codes.update(event.get("operational_failure_codes", {}))
    repaired = [
        target for target, row in progress.items()
        if row["route_index"] + 1 < len(plan) and (
            row["action"] == "external_fallback"
            or (
                row["action"] == "repair_native_runtime" and policy is not None
                and failure_codes.get(target) in policy["fallback_on"]
            )
        )
    ]
    if not repaired:
        return {**_state_summary(work_dir, state_dir, state), "repaired_count": 0}
    source_segments = _bound_source_segments(work_dir, state)
    state["route_plan"] = plan
    if policy is not None:
        state["execution_environment_policy"] = policy
    for target in repaired:
        row = progress[target]
        progress[target] = {"route_index": row["route_index"] + 1, "attempt": 1, "action": "native_retry"}
    state["history"].append({
        "event": "approved_route_extension", "target_ids": repaired,
        "execution_complete": False, "success_count": 0,
    })
    candidates = {row["target_id"]: row for row in _read_jsonl(work_dir / state["candidate_artifact"])}
    pending = _select_pending_cohort(state, source_segments)
    return {
        **_write_stage(work_dir, state_dir, source_segments, candidates, pending, state),
        "repaired_count": len(repaired),
    }


def require_pre_fallback_retry_state(
    work_dir: Path, state_path: Path, source_path: Path, final_results: Path, *,
    route_plan: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    state_path = state_path.resolve()
    state_dir = state_path.parent
    if state_path.name == "lineage.json":
        lineage = json.loads(state_path.read_text(encoding="utf-8"))
        verified = verify_native_translation_retry_cohort(
            work_dir, lineage, source_path=source_path, results_path=final_results,
        )
        if state_path != (work_dir / lineage["export_path"]).resolve():
            raise NativeTranslationRetryError("Native retry cohort receipt locator changed")
        rows = _read_jsonl(final_results)
        failures = sum(row.get("status") != "success" for row in rows)
        configured = (
            _validated_route_plan(route_plan) if route_plan is not None
            else native_translation_retry_route_plan(work_dir, retry_state_path=state_path)
        ) if failures else None
        if failures and not _route_plans_match(verified["route_plan"], configured):
            raise NativeTranslationRetryError("Configured native routes changed; collect their approved successors before external fallback")
        return {
            "schema": RETRY_SCHEMA, "state": lineage["state"],
            "next_action": "external_fallback" if failures else "complete",
            "next_provider": "", "next_attempt": 0,
            "pending_count": failures, "candidate_count": len(rows) - failures,
            "deferred_failure_count": 0, "unresolved_count": failures,
            "native_exhausted_count": failures, "repair_needed_count": 0,
            "cohort_input": lineage["source_artifact"], "cohort_results": lineage["results_artifact"],
            **verified,
        }
    if state_path.name != STATE_NAME:
        raise NativeTranslationRetryError("Native retry state path is not canonical")
    requested_cohort = (
        "successful" if source_path.resolve() == state_dir / "successful-targets.jsonl" and final_results.resolve() == state_dir / CANDIDATES_NAME
        else "native_exhausted" if source_path.resolve() == state_dir / "native-exhausted-targets.jsonl" and final_results.resolve() == state_dir / "native-exhausted-results.jsonl"
        else None
    )
    state = _load_state(work_dir, state_dir, cohort=requested_cohort)
    selected_cohort = "all"
    if (
        source_path.resolve() == state_dir / "successful-targets.jsonl"
        and final_results.resolve() == (work_dir / state["candidate_artifact"]).resolve()
        and _read_jsonl(final_results)
    ):
        selected_cohort = "successful"
    elif (
        source_path.resolve() == state_dir / "native-exhausted-targets.jsonl"
        and final_results.resolve() == state_dir / "native-exhausted-results.jsonl"
        and any(row["action"] == "external_fallback" for row in state.get("target_progress", {}).values())
    ):
        selected_cohort = "native_exhausted"
    if selected_cohort == "all":
        if state["next_action"] not in {"external_fallback", "complete"}:
            raise NativeTranslationRetryError(
                "Configured native pre-fallback translation attempts are not complete"
            )
        if (work_dir / state["source"]).resolve() != source_path.resolve():
            raise NativeTranslationRetryError("Native retry state targets another source")
        if (work_dir / state["final_results"]).resolve() != final_results.resolve():
            raise NativeTranslationRetryError("Native retry state targets another result set")
    plan = _validated_route_plan(state["route_plan"])
    failures = any(row.get("status") != "success" for row in _read_jsonl(final_results))
    configured = (
        _validated_route_plan(route_plan) if route_plan is not None
        else native_translation_retry_route_plan(work_dir, retry_state_path=state_path)
    ) if failures else None
    if failures and not _route_plans_match(plan, configured):
        raise NativeTranslationRetryError(
            "Native retry route plan does not match configured routes"
        )
    return export_native_translation_retry_cohort(work_dir, state_dir, cohort=selected_cohort)
