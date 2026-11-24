"""Lossless, method-isolated Unity occurrence/evidence cache (workspace only)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path, PurePosixPath
from time import perf_counter_ns
from typing import Any, Iterator


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_file(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\0" in value:
        raise ValueError("INVENTORY_FILE_BINDING_INVALID")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("INVENTORY_FILE_BINDING_INVALID")
    return path.as_posix()


def _integer(value: Any) -> int:
    if type(value) is not int:
        raise ValueError("INVENTORY_LOCATION_INTEGER_REQUIRED")
    return value


def _path(value: Any, *, allow_empty: bool = False) -> list[str | int]:
    if not isinstance(value, list) or (not value and not allow_empty) or any(type(part) not in {str, int} for part in value):
        raise ValueError("INVENTORY_LOCATION_PATH_REQUIRED")
    return value


def physical_binding(row: dict[str, Any]) -> dict[str, Any]:
    """Method, source value and incidental heap offsets never determine identity."""
    kind = row["kind"]
    location = row["location"]
    if not isinstance(location, dict):
        raise ValueError("INVENTORY_LOCATION_REQUIRED")
    binding: dict[str, Any] = {"file": relative_file(row["file"]), "kind": kind}
    if kind == "managed_literal":
        binding.update(method_token=_integer(location["method_token"]), il_offset=_integer(location["il_offset"]))
    elif kind == "managed_resource":
        binding.update(resource_token=_integer(location["resource_token"]), entry_index=_integer(location["entry_index"]))
    elif kind in {"serialized_string", "textasset"}:
        binding.update(container_entry=str(location.get("container_entry", "")), object_id=_integer(location["object_id"]), field_path=_path(location["field_path"]))
        parser = location.get("parser")
        if parser is not None:
            if parser not in {"json", "ini", "csv", "tsv"}:
                raise ValueError("INVENTORY_TABLE_PARSER_INVALID")
            binding.update(parser=parser, parser_path=_path(location["parser_path"], allow_empty=True))
            if "entry_index" in location:
                binding["entry_index"] = _integer(location["entry_index"])
    elif kind == "native_literal":
        binding["encoding"] = str(location["encoding"])
        if "use_rva" in location:
            binding["use_rva"] = _integer(location["use_rva"])
        else:
            binding["rva"] = _integer(location["rva"])
    elif kind == "native_resource":
        binding.update(resource_path=_path(location["resource_path"]), entry_index=_integer(location["entry_index"]))
    elif kind == "app_info":
        binding["line_index"] = _integer(location["line_index"])
    else:
        raise ValueError("INVENTORY_KIND_UNSUPPORTED")
    return binding


def occurrence_id(row: dict[str, Any]) -> str:
    return "unity:" + fingerprint(physical_binding(row))


def source_order(row: dict[str, Any]) -> tuple[int, int, int, str]:
    location = row["location"]
    kind = row["kind"]
    if kind == "managed_literal":
        return 0, location["method_token"], location["il_offset"], ""
    if kind == "managed_resource":
        return 1, location["resource_token"], location["entry_index"], ""
    if kind in {"serialized_string", "textasset"}:
        entry = _integer(location.get("entry_index", -1)) + 1
        return 2, int(location.get("object_index", location["object_id"])), int(location.get("field_index", 0)), f"{entry:020d}:" + encoded(physical_binding(row))
    if kind == "native_literal":
        return 3, location.get("use_rva", location["rva"]), 0, ""
    if kind == "native_resource":
        return 3, 0, location["entry_index"], encoded(location["resource_path"])
    return 4, location["line_index"], 0, ""


def is_candidate(row: dict[str, Any]) -> bool:
    return row["classification"] == "display" or (
        row["classification"] == "uncertain" and row.get("display_dependency") is True
    )

def _pending_display_key(file, source_digest, row):
    # A file-level observation stays file-level; a location is never
    # inferred from display text, traversal order, or another method's proof.
    coordinates = ("dependency", "method_token", "type_token", "field_token", "il_offset",
                   "resource_token", "entry_index", "line_index", "resource_path", "rva", "use_rva",
                   "function_rva", "call_rva", "load_rva", "register", "object_id",
                   "container_entry", "field_path", "parser", "parser_path")
    location = row.get("location") or {}
    return fingerprint([file, source_digest, row.get("reason"), row.get("scope"),
                        {key: row[key] for key in coordinates if key in row},
                        {key: location[key] for key in coordinates if key in location}])


class ScanCache:
    """Each result is bound to method, source bytes and implemented scanner bytes."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS proofs (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scans (
                method TEXT NOT NULL, file TEXT NOT NULL, source_digest TEXT NOT NULL,
                scanner_digest TEXT NOT NULL, metadata TEXT NOT NULL,
                PRIMARY KEY(method,file));
            CREATE TABLE IF NOT EXISTS occurrences (
                method TEXT NOT NULL, file TEXT NOT NULL, id TEXT NOT NULL,
                entry TEXT NOT NULL, group_no INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                subordinal INTEGER NOT NULL, tie TEXT NOT NULL, candidate INTEGER NOT NULL,
                classification TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(method,file,id),
                FOREIGN KEY(method,file) REFERENCES scans(method,file) ON DELETE CASCADE);
            CREATE INDEX IF NOT EXISTS occurrence_order ON occurrences
                (method,file,entry,group_no,ordinal,subordinal,tie);
        """)

    def __enter__(self) -> "ScanCache":
        return self

    def __exit__(self, *_: Any) -> None:
        self.connection.close()

    def _proof(self, value: Any) -> str:
        key = fingerprint(value)
        self.connection.execute("INSERT OR IGNORE INTO proofs VALUES (?,?)", (key, encoded(value)))
        return key

    def reusable(self, method: str, file: str, source_digest: str, scanner_digest: str) -> bool:
        record = self.connection.execute(
            "SELECT source_digest,scanner_digest FROM scans WHERE method=? AND file=?", (method, file)
        ).fetchone()
        return record == (source_digest, scanner_digest)

    def source_matches(self, method: str, file: str, source_digest: str) -> bool:
        row = self.connection.execute("SELECT source_digest FROM scans WHERE method=? AND file=?", (method, file)).fetchone()
        return row == (source_digest,)

    def metadata(self, method: str, file: str) -> dict[str, Any]:
        record = self.connection.execute("SELECT metadata FROM scans WHERE method=? AND file=?", (method, file)).fetchone()
        if record is None:
            raise ValueError("INVENTORY_SCAN_NOT_CACHED")
        return json.loads(record[0])

    def retain_files(self, method: str, files: set[str]) -> None:
        with self.connection:
            for (file,) in self.connection.execute("SELECT file FROM scans WHERE method=?", (method,)).fetchall():
                if file not in files:
                    self.connection.execute("DELETE FROM scans WHERE method=? AND file=?", (method, file))

    def store(self, method: str, file: str, source_digest: str, scanner_digest: str, result: dict[str, Any]) -> None:
        file = relative_file(file)
        metadata = {key: value for key, value in result.items() if key not in {"occurrences", "proofs"}}
        proofs = result.get('proofs', {})
        if (isinstance(proofs, dict) and proofs.get('static_complete') is False
                and (any(is_candidate(row) for row in result.get('occurrences', []))
                     or any(edge.get('display_dependency') is True
                            or edge.get('kind') in {'native_display_import_call', 'native_dynamic_display_api_lookup'}
                            for edge in result.get('dependencies', [])))):
            metadata['pending'] = [*metadata.get('pending', []),
                {'reason': 'scanner_static_completeness_unverified', 'scope': 'display_dependency', 'file': file}]
        with self.connection:
            self.connection.execute("DELETE FROM scans WHERE method=? AND file=?", (method, file))
            metadata["proof_ids"] = {str(name): self._proof(value) for name, value in proofs.items()} if isinstance(proofs, dict) else [self._proof(value) for value in proofs]
            self.connection.execute("INSERT INTO scans VALUES (?,?,?,?,?)", (method, file, source_digest, scanner_digest, encoded(metadata)))
            for occurrence in result.get("occurrences", []):
                row = dict(occurrence)
                if row.get("file") != file or row.get("classification") not in {"display", "non_display", "uncertain"}:
                    raise ValueError("INVENTORY_OCCURRENCE_BINDING_INVALID")
                if not isinstance(row.get("source"), str):
                    raise ValueError("INVENTORY_SOURCE_NOT_STRING")
                if is_candidate(row):
                    row["source"].encode("utf-8")
                key = occurrence_id(row)
                proof = row.pop("proof", {})
                row["proof_id"] = self._proof(proof)
                group, ordinal, subordinal, tie = source_order(row)
                self.connection.execute(
                    "INSERT INTO occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (method, file, key, str(row["location"].get("container_entry", "")), group, ordinal, subordinal, tie, int(is_candidate(row)), row["classification"], json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)),
                )

    def occurrences(self, method: str, *, candidates_only: bool = False) -> Iterator[dict[str, Any]]:
        extra = " AND candidate=1" if candidates_only else ""
        query = "SELECT id,payload FROM occurrences WHERE method=?" + extra + " ORDER BY file,entry,group_no,ordinal,subordinal,tie,id"
        for key, payload in self.connection.execute(query, (method,)):
            yield {"id": key, **json.loads(payload)}

    def summary(self, method: str, *, include_pending_keys: bool = False) -> dict[str, Any]:
        counts = {"raw_occurrence_count": 0, "display_candidate_count": 0, "control_only_count": 0, "unclassified_count": 0}
        for classification, candidate, count in self.connection.execute("SELECT classification,candidate,count(*) FROM occurrences WHERE method=? GROUP BY classification,candidate", (method,)):
            counts["raw_occurrence_count"] += count
            counts["display_candidate_count"] += count if candidate else 0
            counts["control_only_count"] += count if classification == "non_display" else 0
            counts["unclassified_count"] += count if classification == "uncertain" and not candidate else 0
        pending = []
        pending_keys = set()
        bindings = []
        for file, source_digest, scanner_digest, metadata in self.connection.execute("SELECT file,source_digest,scanner_digest,metadata FROM scans WHERE method=? ORDER BY file", (method,)):
            value = json.loads(metadata)
            visible = [item for item in value.get("pending", []) if not isinstance(item, dict) or item.get("affects_display", True)]
            pending.extend(visible)
            if include_pending_keys:
                pending_keys.update(_pending_display_key(file, source_digest, item)
                                    for item in visible if isinstance(item, dict))
            bindings.append([file, source_digest, scanner_digest, value.get("proof_ids")])
        return {**counts, "pending": pending, "evidence_fingerprint": fingerprint(bindings),
                **({"pending_display_keys": sorted(pending_keys)} if include_pending_keys else {})}

    def inventory(self, method: str, output: Path, implementation: str, *, telemetry=None) -> dict[str, Any]:
        """Stream JSON to disk; neither model output nor tool RPC carries the table."""
        started = perf_counter_ns()
        summary = self.summary(method, include_pending_keys=telemetry is not None)
        pending_keys = summary.pop("pending_display_keys", None)
        coverage = {
            "declared_complete": not summary["pending"],
            "exclusions": [],
            "display_scope": {"schema": "parr0t.display-scope.v1", **{key: value for key, value in summary.items() if key not in {"pending"}}},
        }
        if summary["control_only_count"]:
            coverage["exclusions"].append({"reason_code": "STRUCTURALLY_NON_DISPLAY", "affects_user_visible_text": False, "target_count": summary["control_only_count"], "evidence_fingerprint": summary["evidence_fingerprint"]})
        if summary["pending"]:
            coverage["exclusions"].append({"reason_code": "UNRESOLVED_DISPLAY_DEPENDENCY", "affects_user_visible_text": True, "target_count": len(summary["pending"]), "evidence_fingerprint": fingerprint(summary["pending"])})
        header = {"schema": "parr0t.source-inventory.v1", "method": "unity-2022.3-" + method, "implementation_fingerprint": implementation}
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded(header)[:-1] + ',"segments":[')
                for order, row in enumerate(self.occurrences(method, candidates_only=True)):
                    if order:
                        stream.write(",")
                    source_sha = hashlib.sha256(row["source"].encode("utf-8")).hexdigest()
                    proof_sha = fingerprint({"method": method, "implementation": implementation, "location": physical_binding(row), "proof_id": row["proof_id"], "proofs": summary["evidence_fingerprint"], "source_sha256": source_sha})
                    metadata = {
                        "unity_kind": row["kind"], "file": row["file"], "location": row["location"],
                        "inventory_comparison_key": row["id"], "display_binding_id": "display:" + row["id"],
                        "display_scope_evidence": {"kind": "positive_display_binding" if row["classification"] == "display" else "possible_display_dependency", "source_sha256": source_sha, "evidence_fingerprint": proof_sha},
                    }
                    for key in ("ui_slot_role", "ui_composed_with_ids", "ui_render_pattern", "display_projection_required"):
                        if key in row:
                            metadata[key] = row[key]
                    stream.write(encoded({"id": row["id"], "order": order, "source": row["source"], "metadata": metadata}))
                if telemetry is not None:
                    coverage["extraction_evidence"] = {
                        "pending_display_keys": pending_keys,
                        "telemetry": {**telemetry, "inventory_ms": (perf_counter_ns() - started) // 1_000_000},
                    }
                stream.write('],"coverage":' + encoded(coverage) + '}')
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return {key: value for key, value in summary.items() if key != "pending"} | {"pending_count": len(summary["pending"]), "static_complete": not summary["pending"]}
