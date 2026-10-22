from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import struct
import threading
import unicodedata
from collections import Counter
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse

from .corrections import CORRECTION_REQUEST_SCHEMA, CorrectionError, apply_user_corrections
from .formats import read_existing_translations, read_segments
from .models import Segment
from .release_checkpoint import canonical_checkpoint_bytes
from .store import stable_hash
from .validators import validate_integrity


STATE_SCHEMA = "parr0t.user-interpretation-store/v1"
TASK_SCHEMA = "parr0t.user-interpretation-task/v1"
PROPOSALS_SCHEMA = "parr0t.user-interpretation-proposals/v1"
MAX_FOCUSED_TARGETS = 32
MAX_SEARCH_RESULTS = 100
MAX_QUERY_CHARS = 4096
MAX_SEARCH_CANDIDATES = 2048
FLAG_KINDS = {"dialogue", "reference", "ui", "logic", "terminology", "other", "dependency"}
ROUTES = {"direct", "agent"}


class CorrectionUIError(CorrectionError):
    pass


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _ngrams(value: str) -> Counter[str]:
    compact = _normalize(value).replace(" ", "")
    grams: Counter[str] = Counter()
    for size in (1, 2, 3):
        grams.update(compact[index : index + size] for index in range(max(0, len(compact) - size + 1)))
    return grams


def _vector_signature(value: str) -> int:
    compact = _normalize(value).replace(" ", "")[:4096]
    features = {
        compact[index : index + size]
        for size in (2, 3)
        for index in range(max(0, len(compact) - size + 1))
    }
    if not features:
        return 0
    minima = [0xFFFF] * 4
    for feature in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        for index, candidate in enumerate(struct.unpack(">4H", digest)):
            if candidate < minima[index]:
                minima[index] = candidate
    return sum(value << (index * 16) for index, value in enumerate(minima))


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


def _buckets(value: int) -> tuple[int, int, int, int]:
    return tuple((value >> (index * 16)) & 0xFFFF for index in range(4))  # type: ignore[return-value]


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    dot = sum(weight * right.get(term, 0) for term, weight in left.items())
    if not dot:
        return 0.0
    left_norm = math.sqrt(sum(weight * weight for weight in left.values()))
    right_norm = math.sqrt(sum(weight * weight for weight in right.values()))
    return dot / (left_norm * right_norm)


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, right_char in enumerate(right, start=1):
        current = [row]
        for column, left_char in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def _character_similarity(query: str, values: Iterable[str]) -> float:
    best = 0.0
    for value in values:
        normalized = _normalize(value)
        if not normalized:
            continue
        distance = _levenshtein(query, normalized)
        best = max(best, 1.0 - (distance / max(len(query), len(normalized))))
    return best


class CorrectionWorkspace:
    """One file-backed, private correction index bound to one released inventory."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._lock = threading.RLock()

    @classmethod
    def prepare(
        cls,
        *,
        state_path: Path,
        source_path: Path,
        checkpoint_path: Path,
        base_results_path: Path | None,
        request_output_path: Path,
        corrected_output_path: Path,
        agent_task_path: Path,
        expected_inventory_fingerprint: str,
    ) -> "CorrectionWorkspace":
        source_path = source_path.resolve()
        checkpoint_path = checkpoint_path.resolve()
        base_results_path = base_results_path.resolve() if base_results_path else None
        checkpoint, checkpoint_bytes = canonical_checkpoint_bytes(checkpoint_path)
        if checkpoint["source_inventory"]["fingerprint"] != expected_inventory_fingerprint:
            raise CorrectionUIError("Release checkpoint does not match the verified source inventory")
        binding = {
            "schema": STATE_SCHEMA,
            "inventory_fingerprint": expected_inventory_fingerprint,
            "source_path": str(source_path),
            "source_sha256": _sha256_bytes(source_path.read_bytes()),
            "checkpoint_path": str(checkpoint_path),
            "checkpoint_sha256": _sha256_bytes(checkpoint_bytes),
            "base_results_path": str(base_results_path) if base_results_path else "",
            "base_results_sha256": (
                _sha256_bytes(base_results_path.read_bytes()) if base_results_path else ""
            ),
            "request_output_path": str(request_output_path.resolve()),
            "corrected_output_path": str(corrected_output_path.resolve()),
            "agent_task_path": str(agent_task_path.resolve()),
        }
        binding_sha256 = stable_hash(binding)
        instance = cls(state_path)
        if instance.path.exists():
            if instance._metadata_value("schema") != STATE_SCHEMA:
                raise CorrectionUIError("Correction state schema is unsupported")
            if instance._metadata_value("binding_sha256") != binding_sha256:
                raise CorrectionUIError(
                    "Correction state is bound to different artifacts; choose a new --state file"
                )
            return instance

        segments = read_segments(source_path)
        checkpoint_by_id = {entry["id"]: entry for entry in checkpoint["entries"]}
        if len(checkpoint_by_id) != len(checkpoint["entries"]):
            raise CorrectionUIError("Release checkpoint contains duplicate target IDs")
        ids = {item.id for item in segments}
        if len(ids) != len(segments) or set(checkpoint_by_id) != ids:
            raise CorrectionUIError("Inventory and checkpoint IDs must match exactly")
        if base_results_path:
            existing = read_existing_translations(base_results_path)
        else:
            existing = {
                target_id: {"source": segment.source, "text": checkpoint_by_id[target_id]["target"]}
                for target_id, segment in ((item.id, item) for item in segments)
            }
        if set(existing) != ids:
            raise CorrectionUIError("Inventory, released results, and checkpoint IDs must match exactly")

        instance.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = instance.path.with_name(f".{instance.path.name}.tmp")
        temporary.unlink(missing_ok=True)
        try:
            builder = cls(temporary)
            builder._create_schema()
            with closing(builder._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                for key, value in {**binding, "binding_sha256": binding_sha256}.items():
                    db.execute(
                        "INSERT INTO metadata(key, value) VALUES (?, ?)",
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
                for ordinal, segment in enumerate(segments):
                    entry = checkpoint_by_id[segment.id]
                    current = existing[segment.id]
                    if current["source"] != segment.source:
                        raise CorrectionUIError(f"Released source changed for target {segment.id!r}")
                    if current["text"] != entry["target"]:
                        raise CorrectionUIError(f"Released target changed for target {segment.id!r}")
                    context = " ".join(value for value in (segment.speaker, segment.scene) if value)
                    search_text = " ".join((segment.source, current["text"], context))
                    signature = _vector_signature(search_text)
                    bucket0, bucket1, bucket2, bucket3 = _buckets(signature)
                    db.execute(
                        """INSERT INTO records(
                            target_id, ordinal, source, target, speaker, scene,
                            previous_text, next_text, metadata_json, dependencies_json,
                            normalized_id, normalized_source, normalized_target,
                            normalized_context, source_length, target_length,
                            vector_hash, bucket0, bucket1, bucket2, bucket3
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            segment.id,
                            ordinal,
                            segment.source,
                            current["text"],
                            segment.speaker,
                            segment.scene,
                            segment.previous,
                            segment.next,
                            json.dumps(segment.metadata, ensure_ascii=False, sort_keys=True),
                            json.dumps(entry["dependencies"], ensure_ascii=False),
                            _normalize(segment.id),
                            _normalize(segment.source),
                            _normalize(current["text"]),
                            _normalize(context),
                            len(_normalize(segment.source)),
                            len(_normalize(current["text"])),
                            _signed64(signature),
                            bucket0,
                            bucket1,
                            bucket2,
                            bucket3,
                        ),
                    )
                    for dependency in entry["dependencies"]:
                        if dependency in ids:
                            db.execute(
                                "INSERT OR IGNORE INTO reverse_dependencies(dependency_id, target_id) VALUES (?, ?)",
                                (dependency, segment.id),
                            )
                db.commit()
            temporary.replace(instance.path)
        finally:
            temporary.unlink(missing_ok=True)
        return instance

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _create_schema(self) -> None:
        with closing(self._connect()) as db:
            db.executescript(
                """
                PRAGMA journal_mode = DELETE;
                PRAGMA synchronous = FULL;
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE records(
                    target_id TEXT PRIMARY KEY,
                    ordinal INTEGER NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    previous_text TEXT NOT NULL,
                    next_text TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    normalized_id TEXT NOT NULL,
                    normalized_source TEXT NOT NULL,
                    normalized_target TEXT NOT NULL,
                    normalized_context TEXT NOT NULL,
                    source_length INTEGER NOT NULL,
                    target_length INTEGER NOT NULL,
                    vector_hash INTEGER NOT NULL,
                    bucket0 INTEGER NOT NULL,
                    bucket1 INTEGER NOT NULL,
                    bucket2 INTEGER NOT NULL,
                    bucket3 INTEGER NOT NULL
                );
                CREATE INDEX records_source_exact ON records(normalized_source);
                CREATE INDEX records_target_exact ON records(normalized_target);
                CREATE INDEX records_source_length ON records(source_length);
                CREATE INDEX records_target_length ON records(target_length);
                CREATE INDEX records_bucket0 ON records(bucket0);
                CREATE INDEX records_bucket1 ON records(bucket1);
                CREATE INDEX records_bucket2 ON records(bucket2);
                CREATE INDEX records_bucket3 ON records(bucket3);
                CREATE TABLE annotations(
                    target_id TEXT PRIMARY KEY REFERENCES records(target_id) ON DELETE CASCADE,
                    route TEXT NOT NULL,
                    flag_kind TEXT NOT NULL,
                    note TEXT NOT NULL,
                    reference_text TEXT NOT NULL,
                    replacement TEXT NOT NULL,
                    proposal TEXT NOT NULL,
                    proposal_decision TEXT NOT NULL,
                    status TEXT NOT NULL,
                    normalized_note TEXT NOT NULL,
                    normalized_reference TEXT NOT NULL,
                    note_length INTEGER NOT NULL,
                    reference_length INTEGER NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX annotations_note_exact ON annotations(normalized_note);
                CREATE INDEX annotations_reference_exact ON annotations(normalized_reference);
                CREATE INDEX annotations_note_length ON annotations(note_length);
                CREATE INDEX annotations_reference_length ON annotations(reference_length);
                CREATE TABLE reverse_dependencies(
                    dependency_id TEXT NOT NULL REFERENCES records(target_id),
                    target_id TEXT NOT NULL REFERENCES records(target_id),
                    PRIMARY KEY(dependency_id, target_id)
                );
                CREATE INDEX reverse_dependencies_lookup ON reverse_dependencies(dependency_id);
                """
            )
            db.commit()

    def _metadata_value(self, key: str, default: Any = None) -> Any:
        with self._lock, closing(self._connect()) as db:
            row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set_metadata(self, key: str, value: Any) -> None:
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            db.commit()

    @staticmethod
    def _select_clause() -> str:
        return """r.*, a.route, a.flag_kind, a.note, a.reference_text, a.replacement,
            a.proposal, a.proposal_decision, a.status AS annotation_status, a.normalized_note,
            a.normalized_reference, a.note_length, a.reference_length"""

    def _row_payload(self, row: sqlite3.Row, *, score: float | None = None) -> dict[str, Any]:
        payload = {
            "id": row["target_id"],
            "ordinal": row["ordinal"],
            "source": row["source"],
            "target": row["target"],
            "speaker": row["speaker"],
            "scene": row["scene"],
            "previous": row["previous_text"],
            "next": row["next_text"],
            "metadata": json.loads(row["metadata_json"]),
            "dependencies": json.loads(row["dependencies_json"]),
            "annotation": None,
        }
        if row["route"] is not None:
            payload["annotation"] = {
                "route": row["route"],
                "flag_kind": row["flag_kind"],
                "note": row["note"],
                "reference": row["reference_text"],
                "replacement": row["replacement"],
                "proposal": row["proposal"],
                "proposal_decision": row["proposal_decision"],
                "status": row["annotation_status"],
            }
        if score is not None:
            payload["score"] = round(score, 6)
            payload["distance"] = round(1.0 - score, 6)
        return payload

    def _fetch_rows(self, target_ids: Iterable[str]) -> list[sqlite3.Row]:
        ids = list(dict.fromkeys(target_ids))
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._lock, closing(self._connect()) as db:
            return db.execute(
                f"""SELECT {self._select_clause()} FROM records r
                LEFT JOIN annotations a ON a.target_id = r.target_id
                WHERE r.target_id IN ({placeholders}) ORDER BY r.ordinal""",
                ids,
            ).fetchall()

    def get_record(self, target_id: str) -> dict[str, Any]:
        rows = self._fetch_rows([target_id])
        if not rows:
            raise CorrectionUIError("Unknown correction target")
        return self._row_payload(rows[0])

    def status(self) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as db:
            total = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            counts = {
                row[0]: row[1]
                for row in db.execute(
                    "SELECT status, COUNT(*) FROM annotations GROUP BY status"
                ).fetchall()
            }
        return {
            "schema": STATE_SCHEMA,
            "target_count": total,
            "flags": counts,
            "applied": bool(self._metadata_value("applied_output_sha256", "")),
            "search_modes": ["exact", "character", "vector"],
            "max_focused_targets": MAX_FOCUSED_TARGETS,
        }

    def search(self, query: str, *, mode: str, limit: int = 20) -> list[dict[str, Any]]:
        normalized = _normalize(query)
        if not normalized:
            return []
        if len(normalized) > MAX_QUERY_CHARS:
            raise CorrectionUIError(f"Search query exceeds {MAX_QUERY_CHARS} characters")
        if mode not in {"exact", "character", "vector"}:
            raise CorrectionUIError("Search mode must be exact, character, or vector")
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        if mode == "exact":
            with self._lock, closing(self._connect()) as db:
                rows = db.execute(
                    f"""SELECT {self._select_clause()} FROM records r
                    LEFT JOIN annotations a ON a.target_id = r.target_id
                    WHERE r.normalized_id = ? OR r.normalized_source = ? OR r.normalized_target = ?
                       OR a.normalized_note = ? OR a.normalized_reference = ?
                    ORDER BY CASE
                        WHEN r.normalized_id = ? THEN 0
                        WHEN r.normalized_source = ? THEN 1
                        WHEN r.normalized_target = ? THEN 2
                        ELSE 3 END, r.ordinal LIMIT ?""",
                    (normalized,) * 8 + (limit,),
                ).fetchall()
            return [self._row_payload(row, score=1.0) for row in rows]

        candidate_ids = self._length_candidates(normalized)
        if mode == "vector":
            signature = _vector_signature(normalized)
            bucket0, bucket1, bucket2, bucket3 = _buckets(signature)
            with self._lock, closing(self._connect()) as db:
                vector_ids = [
                    row[0]
                    for row in db.execute(
                        """SELECT target_id FROM records
                        WHERE bucket0 = ? OR bucket1 = ? OR bucket2 = ? OR bucket3 = ?
                        ORDER BY ordinal LIMIT ?""",
                        (bucket0, bucket1, bucket2, bucket3, MAX_SEARCH_CANDIDATES),
                    ).fetchall()
                ]
            candidate_ids = list(dict.fromkeys(vector_ids + candidate_ids))[:MAX_SEARCH_CANDIDATES]
        rows = self._fetch_rows(candidate_ids)
        scored: list[tuple[float, sqlite3.Row]] = []
        if mode == "character":
            for row in rows:
                score = _character_similarity(normalized, self._searchable_values(row))
                scored.append((score, row))
        else:
            query_vector = _ngrams(normalized)
            for row in rows:
                score = max(
                    (_cosine(query_vector, _ngrams(value)) for value in self._searchable_values(row)),
                    default=0.0,
                )
                scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["ordinal"]))
        return [self._row_payload(row, score=score) for score, row in scored[:limit] if score > 0]

    @staticmethod
    def _searchable_values(row: sqlite3.Row) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                row["target_id"],
                row["source"],
                row["target"],
                " ".join((row["speaker"], row["scene"])),
                row["note"] or "",
                row["reference_text"] or "",
            )
            if value
        )

    def _length_candidates(self, normalized: str) -> list[str]:
        query_length = len(normalized)
        window = max(4, math.ceil(query_length * 0.35))
        lower, upper = max(0, query_length - window), query_length + window
        per_field = max(64, MAX_SEARCH_CANDIDATES // 4)
        ids: list[str] = []
        with self._lock, closing(self._connect()) as db:
            for column in ("source_length", "target_length"):
                ids.extend(
                    row[0]
                    for row in db.execute(
                        f"""SELECT target_id FROM records WHERE {column} BETWEEN ? AND ?
                        ORDER BY ABS({column} - ?), ordinal LIMIT ?""",
                        (lower, upper, query_length, per_field),
                    ).fetchall()
                )
            for column in ("note_length", "reference_length"):
                ids.extend(
                    row[0]
                    for row in db.execute(
                        f"""SELECT target_id FROM annotations WHERE {column} BETWEEN ? AND ?
                        ORDER BY ABS({column} - ?), target_id LIMIT ?""",
                        (lower, upper, query_length, per_field),
                    ).fetchall()
                )
        return list(dict.fromkeys(ids))[:MAX_SEARCH_CANDIDATES]

    def stage_flag(
        self,
        *,
        target_id: str,
        route: str,
        flag_kind: str,
        note: str = "",
        reference: str = "",
        replacement: str = "",
    ) -> dict[str, Any]:
        if route not in ROUTES:
            raise CorrectionUIError("Correction route must be direct or agent")
        if flag_kind not in FLAG_KINDS - {"dependency"}:
            raise CorrectionUIError("Correction flag kind is unsupported")
        record = self.get_record(target_id)
        note = note.strip()
        reference = reference.strip()
        if route == "agent" and not (note or reference):
            raise CorrectionUIError("Agent correction requires a user note or reference")
        if route == "direct":
            if not replacement.strip():
                raise CorrectionUIError("Direct correction requires replacement text")
            if replacement == record["target"]:
                raise CorrectionUIError("Direct correction is unchanged")
            self._validate_replacement(record, replacement)
            status = "approved"
        else:
            replacement = ""
            status = "flagged"
        with self._lock, closing(self._connect()) as db:
            db.execute(
                """INSERT INTO annotations(
                    target_id, route, flag_kind, note, reference_text, replacement,
                    proposal, proposal_decision, status, normalized_note, normalized_reference,
                    note_length, reference_length, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', '', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(target_id) DO UPDATE SET
                    route=excluded.route, flag_kind=excluded.flag_kind, note=excluded.note,
                    reference_text=excluded.reference_text, replacement=excluded.replacement,
                    proposal='', proposal_decision='', status=excluded.status,
                    normalized_note=excluded.normalized_note,
                    normalized_reference=excluded.normalized_reference,
                    note_length=excluded.note_length, reference_length=excluded.reference_length,
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    target_id,
                    route,
                    flag_kind,
                    note,
                    reference,
                    replacement,
                    status,
                    _normalize(note),
                    _normalize(reference),
                    len(_normalize(note)),
                    len(_normalize(reference)),
                ),
            )
            self._refresh_vector(db, target_id)
            db.commit()
        return self.get_record(target_id)

    def clear_flag(self, target_id: str) -> dict[str, Any]:
        self.get_record(target_id)
        with self._lock, closing(self._connect()) as db:
            db.execute("DELETE FROM annotations WHERE target_id = ?", (target_id,))
            self._refresh_vector(db, target_id)
            db.commit()
        return self.get_record(target_id)

    def _refresh_vector(self, db: sqlite3.Connection, target_id: str) -> None:
        row = db.execute(
            """SELECT r.source, r.target, r.speaker, r.scene,
                COALESCE(a.note, ''), COALESCE(a.reference_text, '')
            FROM records r LEFT JOIN annotations a ON a.target_id = r.target_id
            WHERE r.target_id = ?""",
            (target_id,),
        ).fetchone()
        if row is None:
            raise CorrectionUIError("Unknown correction target")
        signature = _vector_signature(" ".join(str(value) for value in row if value))
        db.execute(
            """UPDATE records SET vector_hash=?, bucket0=?, bucket1=?, bucket2=?, bucket3=?
            WHERE target_id=?""",
            (_signed64(signature), *_buckets(signature), target_id),
        )

    @staticmethod
    def _validate_replacement(record: dict[str, Any], replacement: str) -> None:
        segment = Segment(
            id=record["id"],
            source=record["source"],
            speaker=record["speaker"],
            scene=record["scene"],
            previous=record["previous"],
            next=record["next"],
            metadata=record["metadata"],
        )
        issues = validate_integrity(segment, replacement, [])
        if issues:
            raise CorrectionUIError(
                "Correction failed deterministic validation: "
                + ", ".join(sorted({issue.code for issue in issues}))
            )

    def export_agent_task(self) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as db:
            selected = [
                row[0]
                for row in db.execute(
                    "SELECT target_id FROM annotations WHERE route='agent' AND status='flagged' ORDER BY target_id"
                ).fetchall()
            ]
        if not selected:
            raise CorrectionUIError("No pending agent correction flags")
        closure = set(selected)
        pending = list(selected)
        with self._lock, closing(self._connect()) as db:
            while pending:
                current = pending.pop()
                dependents = [
                    row[0]
                    for row in db.execute(
                        "SELECT target_id FROM reverse_dependencies WHERE dependency_id = ?",
                        (current,),
                    ).fetchall()
                ]
                for dependent in dependents:
                    if dependent not in closure:
                        closure.add(dependent)
                        pending.append(dependent)
                        if len(closure) > MAX_FOCUSED_TARGETS:
                            raise CorrectionUIError(
                                "Focused correction exceeds 32 targets after dependency closure"
                            )
        rows = self._fetch_rows(closure)
        selected_set = set(selected)
        records = []
        task_bindings: dict[str, dict[str, str]] = {}
        for row in rows:
            payload = self._row_payload(row)
            context = {
                "speaker": payload["speaker"],
                "scene": payload["scene"],
                "previous": payload["previous"],
                "next": payload["next"],
                "metadata": payload["metadata"],
            }
            context_sha256 = stable_hash(context)
            source_sha256 = _sha256_text(payload["source"])
            task_bindings[payload["id"]] = {
                "source_sha256": source_sha256,
                "context_sha256": context_sha256,
            }
            annotation = payload["annotation"] if payload["id"] in selected_set else None
            records.append(
                {
                    "id": payload["id"],
                    "relation": "flagged" if payload["id"] in selected_set else "dependent",
                    "source": payload["source"],
                    "current_target": payload["target"],
                    "context": context,
                    "source_sha256": source_sha256,
                    "context_sha256": context_sha256,
                    "user_interpretation": {
                        "kind": annotation["flag_kind"] if annotation else "dependency",
                        "note": annotation["note"] if annotation else "",
                        "reference": annotation["reference"] if annotation else "",
                    },
                }
            )
        task_core = {
            "schema": TASK_SCHEMA,
            "state_binding_sha256": self._metadata_value("binding_sha256"),
            "selected_ids": selected,
            "records": records,
        }
        task_id = stable_hash(task_core)
        task = {**task_core, "task_id": task_id}
        output = Path(self._metadata_value("agent_task_path"))
        _atomic_json(output, task)
        self._set_metadata("last_agent_task", {"task_id": task_id, "bindings": task_bindings})
        return {
            "schema": TASK_SCHEMA,
            "status": "ready",
            "task_id": task_id,
            "selected_count": len(selected),
            "closure_count": len(records),
            "output": str(output),
            "output_sha256": _sha256_bytes(output.read_bytes()),
        }

    def import_agent_proposals(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"schema", "task_id", "proposals"}:
            raise CorrectionUIError("Agent proposal fields must be schema, task_id, and proposals")
        if payload["schema"] != PROPOSALS_SCHEMA:
            raise CorrectionUIError("Agent proposal schema is unsupported")
        task = self._metadata_value("last_agent_task")
        if not isinstance(task, dict) or payload["task_id"] != task.get("task_id"):
            raise CorrectionUIError("Agent proposals do not match the latest focused task")
        proposals = payload["proposals"]
        if not isinstance(proposals, list) or not proposals or len(proposals) > MAX_FOCUSED_TARGETS:
            raise CorrectionUIError("Agent proposals must contain 1-32 records")
        bindings = task.get("bindings", {})
        seen: set[str] = set()
        for proposal in proposals:
            if not isinstance(proposal, dict) or set(proposal) != {
                "id", "decision", "target", "source_sha256", "context_sha256"
            }:
                raise CorrectionUIError("Agent proposal record is malformed")
            target_id = proposal["id"]
            if target_id in seen or target_id not in bindings:
                raise CorrectionUIError("Agent proposal contains an unknown or duplicate target")
            seen.add(target_id)
            if (
                proposal["source_sha256"] != bindings[target_id]["source_sha256"]
                or proposal["context_sha256"] != bindings[target_id]["context_sha256"]
            ):
                raise CorrectionUIError("Agent proposal binding changed")
            target = proposal["target"]
            if not isinstance(target, str) or not target.strip():
                raise CorrectionUIError("Agent proposal target must be non-empty text")
            record = self.get_record(target_id)
            decision = proposal["decision"]
            if decision not in {"replace", "unchanged"}:
                raise CorrectionUIError("Agent proposal decision must be replace or unchanged")
            if decision == "replace" and target == record["target"]:
                raise CorrectionUIError("Replacement proposal is unchanged")
            if decision == "unchanged" and target != record["target"]:
                raise CorrectionUIError("Unchanged proposal must preserve the current target")
            self._validate_replacement(record, target)
            annotation = record["annotation"] or {}
            note = annotation.get("note", "")
            reference = annotation.get("reference", "")
            flag_kind = annotation.get("flag_kind", "dependency")
            with self._lock, closing(self._connect()) as db:
                db.execute(
                    """INSERT INTO annotations(
                        target_id, route, flag_kind, note, reference_text, replacement,
                        proposal, proposal_decision, status, normalized_note, normalized_reference,
                        note_length, reference_length, updated_at
                    ) VALUES (?, 'agent', ?, ?, ?, '', ?, ?, 'proposal_ready', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(target_id) DO UPDATE SET proposal=excluded.proposal,
                        proposal_decision=excluded.proposal_decision,
                        status='proposal_ready', updated_at=CURRENT_TIMESTAMP""",
                    (
                        target_id,
                        flag_kind,
                        note,
                        reference,
                        target,
                        decision,
                        _normalize(note),
                        _normalize(reference),
                        len(_normalize(note)),
                        len(_normalize(reference)),
                    ),
                )
                db.commit()
        return {"schema": PROPOSALS_SCHEMA, "status": "imported", "proposal_count": len(seen)}

    def approve_proposal(self, target_id: str, replacement: str = "") -> dict[str, Any]:
        record = self.get_record(target_id)
        annotation = record["annotation"]
        if not annotation or annotation["status"] != "proposal_ready":
            raise CorrectionUIError("Target has no pending agent proposal")
        target = replacement if replacement.strip() else annotation["proposal"]
        self._validate_replacement(record, target)
        if replacement.strip():
            decision = "replace"
        else:
            decision = annotation["proposal_decision"]
        if decision == "unchanged" and target != record["target"]:
            raise CorrectionUIError("Unchanged proposal no longer matches the current target")
        status = "reviewed_unchanged" if decision == "unchanged" else "approved"
        with self._lock, closing(self._connect()) as db:
            db.execute(
                """UPDATE annotations SET replacement=?, status=?,
                updated_at=CURRENT_TIMESTAMP WHERE target_id=?""",
                ("" if status == "reviewed_unchanged" else target, status, target_id),
            )
            db.commit()
        return self.get_record(target_id)

    def apply_approved(self) -> dict[str, Any]:
        if self._metadata_value("applied_output_sha256", ""):
            raise CorrectionUIError("This correction state has already produced an output")
        with self._lock, closing(self._connect()) as db:
            rows = db.execute(
                """SELECT a.target_id, a.replacement FROM annotations a
                JOIN records r ON r.target_id=a.target_id
                WHERE a.status='approved' ORDER BY r.ordinal"""
            ).fetchall()
            reviewed_dependencies = {
                row[0]
                for row in db.execute(
                    "SELECT target_id FROM annotations WHERE status='reviewed_unchanged'"
                ).fetchall()
            }
        if not rows:
            raise CorrectionUIError("No user-approved corrections are ready")
        if len(rows) + len(reviewed_dependencies) > MAX_FOCUSED_TARGETS:
            raise CorrectionUIError("Minimal correction apply is limited to 32 targets")
        request_path = Path(self._metadata_value("request_output_path"))
        output_path = Path(self._metadata_value("corrected_output_path"))
        if request_path.exists() or output_path.exists():
            raise CorrectionUIError("Correction request or output already exists; choose a new bound state")
        request = {
            "schema": CORRECTION_REQUEST_SCHEMA,
            "changes": [{"id": row["target_id"], "target": row["replacement"]} for row in rows],
        }
        _atomic_json(request_path, request)
        source_path = Path(self._metadata_value("source_path"))
        base_value = self._metadata_value("base_results_path", "")
        try:
            summary = apply_user_corrections(
                segments=read_segments(source_path),
                base_results_path=Path(base_value) if base_value else None,
                checkpoint_path=Path(self._metadata_value("checkpoint_path")),
                request_path=request_path,
                output_path=output_path,
                expected_inventory_fingerprint=self._metadata_value("inventory_fingerprint"),
                reviewed_dependency_ids=reviewed_dependencies,
            )
        except Exception:
            request_path.unlink(missing_ok=True)
            raise
        with self._lock, closing(self._connect()) as db:
            db.execute(
                "UPDATE annotations SET status='applied' WHERE status IN ('approved', 'reviewed_unchanged')"
            )
            db.commit()
        self._set_metadata("applied_output_sha256", summary["output_sha256"])
        return summary


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Parr0t focused localization correction</title>
<style>
:root{color-scheme:dark;--bg:#101418;--panel:#181e24;--line:#2d3740;--text:#edf3f7;--muted:#9eabb5;--accent:#60d394;--warn:#f4b860}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line)}h1{margin:0;font-size:21px}small,.muted{color:var(--muted)}
.layout{display:grid;grid-template-columns:minmax(320px,40%) 1fr;height:calc(100vh - 78px)}.left,.right{padding:18px;overflow:auto}.left{border-right:1px solid var(--line)}
.search{display:grid;grid-template-columns:1fr 130px 72px;gap:8px;position:sticky;top:0;background:var(--bg);padding-bottom:12px}
input,select,textarea,button{font:inherit;color:var(--text);background:#11171c;border:1px solid var(--line);border-radius:8px;padding:9px}textarea{width:100%;min-height:88px;resize:vertical}button{cursor:pointer}button.primary{background:#173d2b;border-color:#2c8057}button.warn{background:#49361c;border-color:#8e682c}
.result{padding:11px;border:1px solid var(--line);border-radius:9px;margin:8px 0;cursor:pointer}.result:hover{border-color:var(--accent)}.result b{display:block}.score{float:right;color:var(--accent)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}.card h2{font-size:16px;margin:0 0 10px}.text{white-space:pre-wrap;word-break:break-word}.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.status{padding:9px 0;color:var(--muted)}label{display:block;margin-top:9px;color:var(--muted)}code{word-break:break-all}.hidden{display:none}
@media(max-width:850px){.layout{display:block;height:auto}.left{border-right:0;border-bottom:1px solid var(--line)}.grid{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Parr0t focused localization correction</h1><small>Searches and edits remain in one local state file.</small></header>
<main class="layout"><section class="left">
<div class="search"><input id="query" placeholder="Search source, target, reference, or ID"><select id="mode"><option value="exact">Exact</option><option value="character">Character distance</option><option value="vector">Vector distance</option></select><button id="search">Search</button></div>
<div id="summary" class="status"></div><div id="results"></div></section>
<section class="right"><div id="empty" class="card">Search for a record and select it on the left.</div><div id="detail" class="hidden">
<div class="card"><h2 id="recordId"></h2><div class="muted" id="recordMeta"></div><label>Source</label><div class="text" id="source"></div><label>Current target</label><div class="text" id="target"></div><label>Previous / next</label><div class="text muted" id="flow"></div></div>
<div class="card"><h2>User interpretation and flag</h2><div class="grid"><div><label>Kind</label><select id="kind"><option value="dialogue">Dialogue</option><option value="reference">Reference</option><option value="ui">UI</option><option value="logic">Logic</option><option value="terminology">Terminology</option><option value="other">Other</option></select></div><div><label>Route</label><select id="route"><option value="direct">Direct edit</option><option value="agent">Agent proposal</option></select></div></div><label>Interpretation or correction instruction</label><textarea id="note"></textarea><label>Reference</label><textarea id="reference"></textarea><label>Replacement target</label><textarea id="replacement"></textarea><div class="actions"><button class="primary" id="save">Save flag</button><button id="clear">Remove flag</button><button class="primary hidden" id="approve">Approve agent proposal</button></div><div id="message" class="status"></div></div>
<div class="card"><h2>Focused actions</h2><div class="actions"><button id="export">Export flagged records for an agent</button><label style="margin:0"><input type="file" id="proposalFile" accept="application/json" hidden><button type="button" id="import">Import agent proposals</button></label><button class="warn" id="apply">Apply approved edits to a new result</button></div><div id="jobMessage" class="status"></div></div>
</div></section></main>
<script>
const token=location.hash.slice(1),headers={'X-Parr0t-Token':token,'Content-Type':'application/json'};let current=null;
const $=id=>document.getElementById(id);const esc=s=>String(s??'');
async function api(path,options={}){options.headers={...headers,...(options.headers||{})};const r=await fetch(path,options);const j=await r.json();if(!r.ok)throw new Error(j.error||'Request failed');return j}
function showMessage(id,text,error=false){$(id).textContent=text;$(id).style.color=error?'#ff8f8f':''}
async function refreshStatus(){const s=await api('/api/status');$('summary').textContent=`${s.target_count.toLocaleString()} records · ${Object.values(s.flags).reduce((a,b)=>a+b,0)} flags${s.applied?' · applied':''}`}
async function search(){const q=$('query').value.trim();if(!q)return;const rows=await api(`/api/search?q=${encodeURIComponent(q)}&mode=${$('mode').value}&limit=30`);const box=$('results');box.replaceChildren();for(const r of rows){const el=document.createElement('div');el.className='result';const score=document.createElement('span');score.className='score';score.textContent=(r.score*100).toFixed(1)+'%';const title=document.createElement('b');title.textContent=r.id;const src=document.createElement('div');src.textContent=r.source.slice(0,180);const tgt=document.createElement('div');tgt.className='muted';tgt.textContent=r.target.slice(0,180);el.append(score,title,src,tgt);el.onclick=()=>load(r.id);box.append(el)}if(!rows.length)box.textContent='No results.'}
async function load(id){current=await api('/api/record?id='+encodeURIComponent(id));$('empty').classList.add('hidden');$('detail').classList.remove('hidden');$('recordId').textContent=current.id;$('recordMeta').textContent=[current.speaker,current.scene].filter(Boolean).join(' · ');$('source').textContent=current.source;$('target').textContent=current.target;$('flow').textContent=[current.previous,current.next].filter(Boolean).join('\n→ ');const a=current.annotation||{};$('kind').value=a.flag_kind&&a.flag_kind!=='dependency'?a.flag_kind:'dialogue';$('route').value=a.route||'direct';$('note').value=a.note||'';$('reference').value=a.reference||'';$('replacement').value=a.replacement||a.proposal||'';$('approve').classList.toggle('hidden',a.status!=='proposal_ready');showMessage('message',a.status?`Status: ${a.status}`:'')}
async function save(){if(!current)return;try{current=await api('/api/flag',{method:'POST',body:JSON.stringify({id:current.id,kind:$('kind').value,route:$('route').value,note:$('note').value,reference:$('reference').value,replacement:$('replacement').value})});showMessage('message','Saved.');await refreshStatus();await load(current.id)}catch(e){showMessage('message',e.message,true)}}
async function clearFlag(){if(!current)return;try{await api('/api/flag',{method:'DELETE',body:JSON.stringify({id:current.id})});showMessage('message','Flag removed.');await refreshStatus();await load(current.id)}catch(e){showMessage('message',e.message,true)}}
async function approve(){if(!current)return;try{await api('/api/approve',{method:'POST',body:JSON.stringify({id:current.id,replacement:$('replacement').value})});showMessage('message','Proposal approved as a user edit.');await refreshStatus();await load(current.id)}catch(e){showMessage('message',e.message,true)}}
async function exportTask(){try{const s=await api('/api/export-agent',{method:'POST',body:'{}'});showMessage('jobMessage',`Saved a ${s.closure_count}-record agent task: ${s.output}`)}catch(e){showMessage('jobMessage',e.message,true)}}
async function importTask(file){try{const payload=JSON.parse(await file.text());const s=await api('/api/proposals',{method:'POST',body:JSON.stringify(payload)});showMessage('jobMessage',`Imported ${s.proposal_count} proposals.`);await refreshStatus();if(current)await load(current.id)}catch(e){showMessage('jobMessage',e.message,true)}}
async function apply(){if(!confirm('Apply only approved edits to a new result file?'))return;try{const s=await api('/api/apply',{method:'POST',body:'{}'});showMessage('jobMessage',`Applied ${s.changed_count} edits: ${s.output}`);await refreshStatus()}catch(e){showMessage('jobMessage',e.message,true)}}
$('search').onclick=search;$('query').onkeydown=e=>{if(e.key==='Enter')search()};$('save').onclick=save;$('clear').onclick=clearFlag;$('approve').onclick=approve;$('export').onclick=exportTask;$('import').onclick=()=>$('proposalFile').click();$('proposalFile').onchange=e=>e.target.files[0]&&importTask(e.target.files[0]);$('apply').onclick=apply;refreshStatus().catch(e=>showMessage('summary',e.message,true));
</script></body></html>"""


class _CorrectionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], workspace: CorrectionWorkspace, token: str):
        super().__init__(address, _CorrectionHandler)
        self.workspace = workspace
        self.token = token


class _CorrectionHandler(BaseHTTPRequestHandler):
    server: _CorrectionHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Parr0t-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.server.token)

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 2_000_000:
            raise CorrectionUIError("Request body is too large")
        payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if not isinstance(payload, dict):
            raise CorrectionUIError("Request body must be a JSON object")
        return payload

    def _guarded(self, action: Any) -> None:
        if not self._authorized():
            self._json({"error": "Unauthorized local correction session"}, HTTPStatus.UNAUTHORIZED)
            return
        try:
            self._json(action())
        except (CorrectionUIError, CorrectionError, ValueError, OSError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)
            return
        query = parse_qs(parsed.query)
        if parsed.path == "/api/status":
            self._guarded(self.server.workspace.status)
        elif parsed.path == "/api/search":
            self._guarded(
                lambda: self.server.workspace.search(
                    query.get("q", [""])[0],
                    mode=query.get("mode", ["exact"])[0],
                    limit=int(query.get("limit", ["20"])[0]),
                )
            )
        elif parsed.path == "/api/record":
            self._guarded(lambda: self.server.workspace.get_record(query.get("id", [""])[0]))
        else:
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        def action() -> Any:
            payload = self._body()
            if parsed.path == "/api/flag":
                return self.server.workspace.stage_flag(
                    target_id=str(payload.get("id", "")),
                    route=str(payload.get("route", "")),
                    flag_kind=str(payload.get("kind", "")),
                    note=str(payload.get("note", "")),
                    reference=str(payload.get("reference", "")),
                    replacement=str(payload.get("replacement", "")),
                )
            if parsed.path == "/api/export-agent":
                return self.server.workspace.export_agent_task()
            if parsed.path == "/api/proposals":
                return self.server.workspace.import_agent_proposals(payload)
            if parsed.path == "/api/approve":
                return self.server.workspace.approve_proposal(
                    str(payload.get("id", "")), str(payload.get("replacement", ""))
                )
            if parsed.path == "/api/apply":
                return self.server.workspace.apply_approved()
            raise CorrectionUIError("Unknown API route")

        self._guarded(action)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)

        def action() -> Any:
            if parsed.path != "/api/flag":
                raise CorrectionUIError("Unknown API route")
            payload = self._body()
            return self.server.workspace.clear_flag(str(payload.get("id", "")))

        self._guarded(action)


def correction_server(
    workspace: CorrectionWorkspace,
    *,
    port: int = 0,
    token: str | None = None,
) -> tuple[_CorrectionHTTPServer, str]:
    if not 0 <= port <= 65535:
        raise CorrectionUIError("Correction UI port must be between 0 and 65535")
    secret = token or secrets.token_urlsafe(32)
    return _CorrectionHTTPServer(("127.0.0.1", port), workspace, secret), secret


def serve_correction_ui(workspace: CorrectionWorkspace, *, port: int = 0) -> None:
    server, token = correction_server(workspace, port=port)
    address, bound_port = server.server_address[:2]
    print(
        json.dumps(
            {
                "schema": "parr0t.user-interpretation-ui/v1",
                "status": "serving",
                "url": f"http://{address}:{bound_port}/#{token}",
                "state": str(workspace.path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
