from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from ..formats import read_segments


LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
REVIEWED_STATUSES = {"reviewed", "source_preserved"}


def _read_results(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = payload.get("results") if isinstance(payload, dict) else payload
    elif suffix == ".jsonl":
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]
    elif suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        raise ValueError("Reviewed results must use .json, .jsonl, or .csv")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Reviewed result payload must contain a list of objects")
    return rows


def _put_unique(mapping: dict[str, Any], key: str, value: Any, *, location: str) -> None:
    if not key:
        raise ValueError(f"Missing runtime key for {location}")
    if key in mapping and mapping[key] != value:
        raise ValueError(f"Conflicting targets for runtime location: {location}")
    mapping[key] = value


def compile_runtime_payload(
    segments_path: Path,
    reviewed_results_path: Path,
    output_path: Path,
    *,
    locale: str,
    allow_unreviewed: bool = False,
) -> dict[str, Any]:
    if not LOCALE_RE.fullmatch(locale):
        raise ValueError(f"Invalid locale identifier: {locale}")
    output_path = output_path.resolve()
    if output_path in {segments_path.resolve(), reviewed_results_path.resolve()}:
        raise ValueError("Runtime payload output must not overwrite its source inventory or reviewed results")
    segments = read_segments(segments_path)
    rows = _read_results(reviewed_results_path)
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        segment_id = str(row.get("id") or row.get("segment_id") or "")
        if not segment_id or segment_id in by_id:
            raise ValueError(f"Missing or duplicate reviewed result id: {segment_id!r}")
        by_id[segment_id] = row

    expected_ids = {segment.id for segment in segments}
    if set(by_id) != expected_ids:
        missing = len(expected_ids - set(by_id))
        extra = len(set(by_id) - expected_ids)
        raise ValueError(f"Reviewed result set does not match source inventory (missing={missing}, extra={extra})")

    accepted_statuses = set(REVIEWED_STATUSES)
    if allow_unreviewed:
        accepted_statuses.update({"translated", "accepted_unreviewed"})

    string_tables: dict[str, dict[str, dict[str, Any]]] = {}
    text_assets: dict[str, str] = {}
    literal_strings: dict[str, str] = {}
    ui_paths: set[str] = set()
    source_preserved = 0

    for segment in segments:
        row = by_id[segment.id]
        status = str(row.get("status", ""))
        if status not in accepted_statuses:
            raise ValueError(f"Result {segment.id} is not eligible for patching: {status or 'missing status'}")
        if row.get("source") != segment.source:
            raise ValueError(f"Source mismatch for result id: {segment.id}")
        target = row.get("text")
        if not isinstance(target, str):
            raise ValueError(f"Result {segment.id} has no string text")
        if status == "source_preserved":
            source_preserved += 1
            if target != segment.source:
                raise ValueError(f"Source-preserved result changed source text: {segment.id}")

        metadata = segment.metadata
        kind = str(metadata.get("unity_kind", ""))
        ui_path = metadata.get("ui_path")
        if ui_path is not None:
            if not isinstance(ui_path, str) or not ui_path:
                raise ValueError(f"Invalid UI path for result id: {segment.id}")
            ui_paths.add(ui_path)

        if kind == "string_table":
            collection = str(metadata.get("collection", ""))
            entry_id = str(metadata.get("entry_id", ""))
            if not collection or not entry_id:
                raise ValueError(f"String-table metadata is incomplete for result id: {segment.id}")
            table = string_tables.setdefault(collection, {})
            value = {"text": target, "is_smart": bool(metadata.get("is_smart", False))}
            _put_unique(table, entry_id, value, location=f"string_table/{collection}/{entry_id}")
        elif kind == "text_asset":
            asset_key = str(metadata.get("asset_key", ""))
            _put_unique(text_assets, asset_key, target, location=f"text_asset/{asset_key}")
        elif kind == "literal":
            source_key = metadata.get("source_key", segment.source)
            if not isinstance(source_key, str):
                raise ValueError(f"Literal source key is invalid for result id: {segment.id}")
            _put_unique(literal_strings, source_key, target, location=f"literal/{segment.id}")
        else:
            raise ValueError(f"Unsupported or missing unity_kind for result id: {segment.id}")

    payload = {
        "schema_version": 1,
        "locale": locale,
        "string_tables": {
            collection: dict(sorted(entries.items()))
            for collection, entries in sorted(string_tables.items())
        },
        "text_assets": dict(sorted(text_assets.items())),
        "literal_strings": dict(sorted(literal_strings.items())),
        "ui_paths": sorted(ui_paths),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "segments": len(segments),
        "source_preserved": source_preserved,
        "string_tables": len(string_tables),
        "text_assets": len(text_assets),
        "literal_strings": len(literal_strings),
        "ui_paths": len(ui_paths),
    }
