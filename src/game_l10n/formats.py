from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .models import Segment, TranslationResult


def read_segments(path: Path) -> list[Segment]:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    elif suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = payload["segments"] if isinstance(payload, dict) else payload
    elif suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        raise ValueError(f"Unsupported segment format: {suffix}; use .jsonl, .json, or .csv")

    segments: list[Segment] = []
    seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        segment_id = str(row.get("id", "")).strip()
        source = row.get("source")
        if not segment_id or not isinstance(source, str):
            raise ValueError(f"Row {index} requires string fields 'id' and 'source'")
        if segment_id in seen:
            raise ValueError(f"Duplicate segment id: {segment_id}")
        seen.add(segment_id)
        known = {"id", "source", "speaker", "scene", "previous", "next", "metadata"}
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        metadata = {**metadata, **{key: value for key, value in row.items() if key not in known}}
        segments.append(
            Segment(
                id=segment_id,
                source=source,
                speaker=str(row.get("speaker", "") or ""),
                scene=str(row.get("scene", "") or ""),
                previous=str(row.get("previous", "") or ""),
                next=str(row.get("next", "") or ""),
                metadata=metadata,
            )
        )
    enriched: list[Segment] = []
    for index, segment in enumerate(segments):
        previous = segment.previous
        following = segment.next
        if not previous and index > 0:
            previous = segments[index - 1].source
        if not following and index + 1 < len(segments):
            following = segments[index + 1].source
        enriched.append(replace(segment, previous=previous, next=following))
    return enriched


def write_results(path: Path, results: Iterable[TranslationResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    items = list(results)
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        body = "\n".join(json.dumps(item.to_dict(), ensure_ascii=False) for item in items)
        path.write_text(body + ("\n" if body else ""), encoding="utf-8")
    elif suffix == ".json":
        path.write_text(
            json.dumps({"results": [item.to_dict() for item in items]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif suffix == ".csv":
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["id", "source", "text", "status", "translator", "reviewer", "from_cache", "errors"],
            )
            writer.writeheader()
            for item in items:
                writer.writerow(
                    {
                        "id": item.segment_id,
                        "source": item.source,
                        "text": item.text,
                        "status": item.status,
                        "translator": item.translator,
                        "reviewer": item.reviewer,
                        "from_cache": item.from_cache,
                        "errors": " | ".join(item.errors),
                    }
                )
    else:
        raise ValueError(f"Unsupported output format: {suffix}; use .jsonl, .json, or .csv")
