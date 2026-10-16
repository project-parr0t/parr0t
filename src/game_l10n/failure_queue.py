from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import FailureCode, Segment, TranslationResult


OPERATIONAL_FAILURE_CODES = {
    FailureCode.MALFORMED_RESPONSE.value,
    FailureCode.PROVIDER_ERROR.value,
    FailureCode.RATE_LIMITED.value,
    FailureCode.QUOTA_EXHAUSTED.value,
    FailureCode.AUTHENTICATION_ERROR.value,
    FailureCode.TIMEOUT.value,
    FailureCode.BUDGET_EXHAUSTED.value,
    FailureCode.BUDGET_ACCOUNTING_UNCERTAIN.value,
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
    if any(code in OPERATIONAL_FAILURE_CODES for code in codes):
        return "operational", codes
    if any(item.status == "refused" for item in result.attempts):
        return "provider_refusal", codes or [FailureCode.EXPLICIT_PROVIDER_REFUSAL.value]
    if FailureCode.INTEGRITY_INVALID.value in codes:
        return "integrity", codes
    return "route_exhausted", codes


def update_failure_queue(
    work_dir: Path,
    segments: list[Segment],
    results: Iterable[TranslationResult],
    *,
    inventory_fingerprint: str,
) -> dict[str, Any]:
    segment_by_id = {item.id: item for item in segments}
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
            "retryable_on_user_resume": not terminal,
            "resolution": result.status,
        }
        (terminal_entries if terminal else entries).append(entry)
    all_entries = [*entries, *terminal_entries]
    state = {
        "schema": FAILURE_QUEUE_SCHEMA,
        "inventory_fingerprint": inventory_fingerprint,
        "status": (
            "pending"
            if entries
            else "terminal_unresolved"
            if terminal_entries
            else "empty"
        ),
        "pending_count": len(entries),
        "terminal_count": len(terminal_entries),
        "operational_count": sum(
            item["category"] == "operational" for item in all_entries
        ),
        "blocking_count": sum(item["blocking"] for item in all_entries),
        "entries": entries,
        "terminal_entries": terminal_entries,
    }
    _atomic_json(work_dir / ".game-l10n" / "failure-queue.json", state)
    return state


def load_failure_queue(work_dir: Path, inventory_fingerprint: str) -> dict[str, Any]:
    path = work_dir / ".game-l10n" / "failure-queue.json"
    if not path.is_file():
        raise ValueError("No resumable failure queue exists")
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != FAILURE_QUEUE_SCHEMA:
        raise ValueError("Failure queue schema is unsupported")
    if state.get("inventory_fingerprint") != inventory_fingerprint:
        raise ValueError("Failure queue is stale for the current source inventory")
    entries = state.get("entries", [])
    if not entries and not state.get("terminal_entries"):
        raise ValueError("Failure queue is empty")
    return state


def held_checkpoint_path(work_dir: Path) -> Path:
    return work_dir / ".game-l10n" / "held-results.json"
