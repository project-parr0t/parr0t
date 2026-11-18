from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import FailureCode, Segment, TranslationResult


OPERATIONAL_FAILURE_CODES = {
    FailureCode.MALFORMED_RESPONSE.value,
    FailureCode.OUTPUT_TRUNCATED.value,
    FailureCode.PROVIDER_ERROR.value,
    FailureCode.RATE_LIMITED.value,
    FailureCode.QUOTA_EXHAUSTED.value,
    FailureCode.AUTHENTICATION_ERROR.value,
    FailureCode.TIMEOUT.value,
    FailureCode.BUDGET_EXHAUSTED.value,
    FailureCode.BUDGET_ACCOUNTING_UNCERTAIN.value,
    FailureCode.INPUT_CAPACITY_EXCEEDED.value,
    "provider_operational_error",
    "execution_guard_denied",
}

FAILURE_QUEUE_SCHEMA = "parr0t.failure-queue.v2"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _failure_category(result: TranslationResult) -> tuple[str, list[str]]:
    codes = sorted(
        {
            item.issue_code
            for item in result.attempts
            if item.issue_code
        }
    )
    if result.resolution_code in OPERATIONAL_FAILURE_CODES:
        return "operational", sorted({*codes, result.resolution_code})
    terminal_integrity_codes = {
        FailureCode.INTEGRITY_INVALID.value,
        FailureCode.CONSTRAINT_UNSATISFIABLE.value,
    }
    if result.resolution_code in terminal_integrity_codes:
        return "integrity", sorted({*codes, result.resolution_code})
    if any(code in OPERATIONAL_FAILURE_CODES for code in codes):
        return "operational", codes
    if any(item.status == "refused" for item in result.attempts):
        return "provider_refusal", codes or [FailureCode.EXPLICIT_PROVIDER_REFUSAL.value]
    if any(
        code in {
            FailureCode.INTEGRITY_INVALID.value,
            FailureCode.CONSTRAINT_UNSATISFIABLE.value,
        }
        for code in codes
    ):
        return "integrity", codes
    return ("semantic_pending" if result.attempts else "repair_required"), codes


def _continuation_state(
    entry: dict[str, Any], status: dict[str, Any] | None, *, paused: bool = False,
) -> None:
    proof = {
        "eligible": False, "next_action": "repair", "route_binding": None,
        "issue_code": "continuation_evidence_required",
    }
    if (
        isinstance(status, dict) and set(proof) <= set(status) and type(status.get("eligible")) is bool
        and isinstance(status.get("next_action"), str) and status["next_action"]
        and isinstance(status.get("issue_code"), str)
        and (status.get("route_binding") is None or isinstance(status["route_binding"], dict))
    ):
        proof = {key: status[key] for key in proof}
        if proof["eligible"]:
            binding = proof["route_binding"]
            semantic = proof["next_action"] in {"translate", "review-triage", "review", "review-rewrite", "review-confirm"}
            deterministic = proof["next_action"] in {"accept", "accept-review"}
            valid_binding = (
                isinstance(binding, dict)
                and set(binding) == {"provider", "model", "provider_fingerprint", "ordinal", "candidate_generation", "candidate_digest"}
                and all(isinstance(binding.get(key), str) and binding[key] for key in ("provider", "model", "provider_fingerprint"))
                and type(binding.get("ordinal")) is int and binding["ordinal"] >= 1
                and type(binding.get("candidate_generation")) is int and binding["candidate_generation"] >= 0
                and (binding.get("candidate_digest") is None or isinstance(binding["candidate_digest"], str))
            )
            if not ((semantic and valid_binding) or (deterministic and binding is None)):
                proof = {
                    "eligible": False, "next_action": "repair", "route_binding": None,
                    "issue_code": "continuation_route_evidence_required",
                }
    if paused or entry.get("retryable") is not True or entry.get("continuation_authorized") is not True:
        proof = {
            "eligible": False, "next_action": "stopped", "route_binding": None,
            "issue_code": "user_paused" if paused else "continuation_not_authorized",
        }
    entry["continuation"] = proof
    entry["continuation_eligible"] = proof["eligible"]


def update_failure_queue(
    work_dir: Path,
    segments: list[Segment],
    results: Iterable[TranslationResult],
    *,
    inventory_fingerprint: str,
    continuation_status: dict[str, dict[str, Any]] | None = None,
    paused: bool | None = None,
) -> dict[str, Any]:
    segment_by_id = {item.id: item for item in segments}
    queue_path = work_dir / ".game-l10n" / "failure-queue.json"
    previous = json.loads(queue_path.read_text(encoding="utf-8")) if queue_path.exists() else {}
    same_inventory = previous.get("inventory_fingerprint") == inventory_fingerprint
    prior_entries = {
        entry["target_id"]: entry
        for entry in (previous.get("entries", []) if same_inventory else [])
    }
    paused = (same_inventory and (previous.get("status") == "paused" or previous.get("paused") is True)) if paused is None else paused
    entries: list[dict[str, Any]] = []
    terminal_entries: list[dict[str, Any]] = []
    for result in results:
        if result.status not in {"failed", "source_preserved", "accepted_unreviewed"}:
            continue
        segment = segment_by_id[result.segment_id]
        category, codes = _failure_category(result)
        terminal = result.status in {"source_preserved", "accepted_unreviewed"}
        entry = {
            "target_id": result.segment_id,
            "category": category,
            "failure_codes": codes,
            "blocking": bool(segment.metadata.get("blocking_on_failure", False)),
            "retryable": not terminal,
            "continuation_authorized": not terminal,
            "resolution": result.status,
        }
        prior = prior_entries.get(result.segment_id, {})
        if not terminal:
            entry["retryable"] = prior.get("retryable", prior.get("retryable_on_user_resume", True))
            entry["continuation_authorized"] = prior.get("continuation_authorized", entry["retryable"])
        _continuation_state(entry, (continuation_status or {}).get(result.segment_id), paused=paused)
        (terminal_entries if terminal else entries).append(entry)
    all_entries = [*entries, *terminal_entries]
    state = {
        "schema": FAILURE_QUEUE_SCHEMA,
        "inventory_fingerprint": inventory_fingerprint,
        "paused": bool(paused),
        "status": (
            "paused" if paused else "pending"
            if entries
            else "terminal_unresolved"
            if terminal_entries
            else "empty"
        ),
        "pending_count": len(entries),
        "terminal_count": len(terminal_entries),
        "runnable_count": sum(item["continuation_eligible"] for item in entries),
        "operational_count": sum(
            item["category"] == "operational" for item in all_entries
        ),
        "blocking_count": sum(item["blocking"] for item in all_entries),
        "entries": entries,
        "terminal_entries": terminal_entries,
    }
    _atomic_json(queue_path, state)
    return state


def load_failure_queue(
    work_dir: Path, inventory_fingerprint: str, *,
    continuation_status: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    path = work_dir / ".game-l10n" / "failure-queue.json"
    if not path.is_file():
        raise ValueError("No resumable failure queue exists")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != FAILURE_QUEUE_SCHEMA:
        raise ValueError("Failure queue schema is unsupported")
    if state.get("inventory_fingerprint") != inventory_fingerprint:
        raise ValueError("Failure queue is stale for the current source inventory")
    paused = state.get("status") == "paused" or state.get("paused") is True
    state["paused"] = paused
    entries = state.get("entries", [])
    if not entries and not state.get("terminal_entries"):
        raise ValueError("Failure queue is empty")
    for entry in [*entries, *state.get("terminal_entries", [])]:
        terminal = entry.get("resolution") in {"source_preserved", "accepted_unreviewed"}
        legacy_retryable = entry.pop("retryable_on_user_resume", None)
        if terminal:
            entry["retryable"] = False
            entry["continuation_authorized"] = False
        else:
            entry.setdefault("retryable", legacy_retryable if type(legacy_retryable) is bool else True)
            entry.setdefault("continuation_authorized", entry["retryable"])
        _continuation_state(
            entry, (continuation_status or {}).get(entry["target_id"]),
            paused=paused,
        )
    state["runnable_count"] = sum(entry["continuation_eligible"] for entry in entries)
    return state


def held_checkpoint_path(work_dir: Path) -> Path:
    return work_dir / ".game-l10n" / "held-results.json"
