from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .models import AttemptRecord, TranslationResult


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_hash_sequence(values: Iterable[Any]) -> str:
    """Hash a JSON array without materializing its complete encoded form."""

    digest = hashlib.sha256()
    digest.update(b"[")
    first = True
    for value in values:
        if not first:
            digest.update(b",")
        first = False
        digest.update(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    digest.update(b"]")
    return digest.hexdigest()


class ResultStore:
    """Content-addressed cache stored in the agent work directory, never the SDK repository."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS results (
                    cache_key TEXT PRIMARY KEY,
                    segment_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS staged_candidates (
                    batch_key TEXT NOT NULL,
                    segment_index INTEGER NOT NULL,
                    segment_id TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    candidate_text TEXT NOT NULL,
                    result_payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(batch_key, segment_index)
                )"""
            )
            db.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def get(self, cache_key: str) -> TranslationResult | None:
        with self._lock, closing(self._connect()) as db:
            row = db.execute("SELECT payload FROM results WHERE cache_key = ?", (cache_key,)).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        data["attempts"] = [AttemptRecord(**item) for item in data.get("attempts", [])]
        data["from_cache"] = True
        return TranslationResult(**data)

    def put(self, cache_key: str, result: TranslationResult) -> None:
        payload = json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "INSERT OR REPLACE INTO results(cache_key, segment_id, payload) VALUES (?, ?, ?)",
                (cache_key, result.segment_id, payload),
            )
            db.commit()

    def get_staged_candidates(
        self, batch_key: str
    ) -> dict[int, tuple[TranslationResult, str]]:
        with self._lock, closing(self._connect()) as db:
            rows = db.execute(
                """SELECT segment_index, segment_id, candidate_digest,
                          candidate_text, result_payload
                   FROM staged_candidates
                   WHERE batch_key = ?""",
                (batch_key,),
            ).fetchall()
        staged: dict[int, tuple[TranslationResult, str]] = {}
        for index, segment_id, digest, candidate, payload in rows:
            if not isinstance(candidate, str) or stable_hash(candidate) != digest:
                continue
            try:
                data = json.loads(payload)
                data["attempts"] = [
                    AttemptRecord(**item) for item in data.get("attempts", [])
                ]
                data["from_cache"] = False
                result = TranslationResult(**data)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if result.segment_id != segment_id or result.candidate_digest != digest:
                continue
            staged[int(index)] = (result, candidate)
        return staged

    def put_staged_candidate(
        self,
        batch_key: str,
        segment_index: int,
        result: TranslationResult,
        candidate: str,
    ) -> None:
        payload = json.dumps(asdict(result), ensure_ascii=False, sort_keys=True)
        digest = stable_hash(candidate)
        with self._lock, closing(self._connect()) as db:
            db.execute(
                """INSERT OR REPLACE INTO staged_candidates(
                       batch_key, segment_index, segment_id, candidate_digest,
                       candidate_text, result_payload, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    batch_key,
                    segment_index,
                    result.segment_id,
                    digest,
                    candidate,
                    payload,
                ),
            )
            db.commit()

    def clear_staged_candidates(self, batch_key: str) -> None:
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "DELETE FROM staged_candidates WHERE batch_key = ?", (batch_key,)
            )
            db.commit()


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, event: dict[str, Any]) -> None:
        # Deliberately excludes prompts, source, and translation text.
        safe_event = {
            key: value
            for key, value in event.items()
            if key not in {"source", "text", "prompt", "system", "input", "candidate"}
        }
        line = json.dumps(safe_event, ensure_ascii=False, sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
