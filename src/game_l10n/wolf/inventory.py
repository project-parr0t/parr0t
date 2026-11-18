from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..coverage import require_inventory_attestation
from ..tooling import require_incomplete_artifact
from .scope import (
    VISIBLE_SCRIPT_COMMANDS, occurrence, scope_coverage, structured_role, target_metadata,
)


SOURCE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
SPEAKER_RE = re.compile(r"^@mes(?:\s+(.*))?$")
VISIBLE_PATTERNS = tuple((re.compile(pattern), kind) for pattern, kind in VISIBLE_SCRIPT_COMMANDS)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _stable_id(
    kind: str, speaker: str, source: str, ref: dict[str, Any]
) -> str:
    location = json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = f"{kind}\0{speaker}\0{source}\0{location}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]




def _primary_rows(batch_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for batch_path in sorted(batch_dir.glob("batch_*.json")):
        payload = json.loads(batch_path.read_text(encoding="utf-8-sig"))
        if payload.get("format") not in {
            None,
            "wolf-rpg-agent-batch-v1",
            "wolf-rpg-agent-batch-v2",
        }:
            raise ValueError(f"Unsupported WOLF batch format: {batch_path.name}")
        for item in payload.get("items", []):
            target_id = str(item.get("id", ""))
            source = item.get("source")
            if not target_id or not isinstance(source, str) or target_id in seen:
                raise ValueError(f"Invalid or duplicate WOLF batch target: {target_id!r}")
            seen.add(target_id)
            context = item.get("context") if isinstance(item.get("context"), dict) else {}
            rows.append(
                {
                    "id": target_id,
                    "source": source,
                    "speaker": str(item.get("speaker", "")),
                    "scene": str(context.get("file", context.get("note", ""))),
                    "previous": str(context.get("previous", "")),
                    "next": str(context.get("next", "")),
                    "metadata": {
                        "wolf_kind": str(item.get("kind", "")),
                        "refs": item.get("refs", []),
                        **item.get("metadata", {}),
                    },
                }
            )
    return rows


def _visible_payload(value: str) -> tuple[str, str, str, str] | None:
    if not value or value.startswith(("#", "::", "\ufeff::")):
        return None
    if not value.startswith("@"):
        return "dialogue", value, "", ""
    for pattern, kind in VISIBLE_PATTERNS:
        match = pattern.match(value)
        if match:
            return kind, match.group(2), match.group(1), ""
    if value.startswith("@sumo_actionlist ") and ",msg," in value:
        prefix, tail = value.split(",msg,", 1)
        body, separator, suffix = tail.partition(";")
        return "sumo_message", body, prefix + ",msg,", separator + suffix if separator else ""
    return None


def _walk_nodes(
    value: Any,
) -> Iterable[tuple[tuple[Any, ...], dict[str, Any]]]:
    pending: list[tuple[tuple[Any, ...], Any]] = [((), value)]
    while pending:
        path, current = pending.pop()
        if isinstance(current, dict):
            if isinstance(current.get("source"), str) and isinstance(current.get("text"), str):
                yield path, current
            pending.extend(
                reversed(
                    [(path + (key,), child) for key, child in current.items()]
                )
            )
        elif isinstance(current, list):
            pending.extend(
                reversed(
                    [(path + (index,), child) for index, child in enumerate(current)]
                )
            )


def _add(
    records: dict[str, dict[str, Any]],
    *,
    kind: str,
    speaker: str,
    source: str,
    ref: dict[str, Any],
    scene: str = "",
    role: str,
) -> None:
    target_id = _stable_id(kind, speaker, source, ref)
    if target_id in records:
        raise ValueError(f"Duplicate WOLF audit source location: {target_id}")
    records[target_id] = {
        "id": target_id,
        "source": source,
        "speaker": speaker,
        "scene": scene,
        "kind": kind,
        "metadata": target_metadata(source, ref, role),
    }


def _independent_rows(
    source_root: Path, excluded_json_names: Iterable[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: dict[str, dict[str, Any]] = {}
    census: list[dict[str, Any]] = []
    names_path = source_root / "names.json"
    if names_path.is_file():
        payload = json.loads(names_path.read_text(encoding="utf-8-sig"))
        for index, node in enumerate(payload.get("names", [])):
            source = str(node.get("source", ""))
            ref = {"json": "names.json", "type": "name", "index": index}
            classification = ("unclassified" if node.get("safety") != "safe"
                              else "display_candidate" if SOURCE_TEXT_RE.search(source)
                              else "language_not_selected")
            census.append(occurrence(ref, source, "safe_name_dependency", classification))
            if node.get("safety") == "safe" and SOURCE_TEXT_RE.search(source):
                _add(
                    records,
                    kind="name",
                    speaker="name/database",
                    source=source,
                    role="safe_name_dependency",
                    ref=ref,
                )

    excluded = {"names.json", *excluded_json_names}
    for path in sorted(source_root.glob("*.json")):
        if path.name in excluded:
            continue
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        relative = path.relative_to(source_root).as_posix()
        for node_path, node in _walk_nodes(payload):
            source = node["source"]
            ref = {"json": relative, "type": "structured", "path": list(node_path)}
            role = structured_role(node_path)
            visible = _visible_payload(source) if role else None
            if visible is not None:
                classification = ("display_candidate" if SOURCE_TEXT_RE.search(visible[1])
                                  else "language_not_selected")
            elif role and (SPEAKER_RE.match(source) or not source or source.startswith(("#", "::", "\ufeff::"))):
                classification = "control_only"
            else:
                classification = "unclassified"
            census.append(occurrence(ref, source, role or "unsupported_structured", classification))
            if classification == "display_candidate":
                _, text, prefix, suffix = visible
                if prefix or suffix:
                    ref.update(prefix=prefix, suffix=suffix)
                _add(records, kind="structured",
                     speaker=str(node.get("speaker") or node.get("speaker_src") or "UI/system"),
                     source=text, scene=relative, ref=ref, role=role)

    text_root = source_root / "text"
    if text_root.is_dir():
        for path in sorted(text_root.rglob("__strings.json")):
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            relative = path.relative_to(source_root).as_posix()
            for file_index, file_entry in enumerate(payload.get("files", [])):
                speaker = "unknown"
                scene = str(file_entry.get("file", f"file-{file_index}"))
                for line_index, line in enumerate(file_entry.get("lines", [])):
                    source_line = str(line.get("source", ""))
                    raw_ref = {"json": relative, "type": "text", "file_index": file_index,
                               "line_index": line_index}
                    speaker_match = SPEAKER_RE.match(source_line)
                    if speaker_match:
                        speaker = (speaker_match.group(1) or "unknown").strip()
                        census.append(occurrence(raw_ref, source_line, "speaker_context", "control_only"))
                        continue
                    visible = _visible_payload(source_line)
                    if visible is not None:
                        classification = ("display_candidate" if SOURCE_TEXT_RE.search(visible[1])
                                          else "language_not_selected")
                    elif not source_line or source_line.startswith(("#", "::", "\ufeff::")):
                        classification = "control_only"
                    else:
                        classification = "unclassified"
                    census.append(occurrence(raw_ref, source_line, "text_script", classification))
                    if classification == "display_candidate":
                        kind, source, prefix, suffix = visible
                        _add(
                            records,
                            kind=kind,
                            speaker=speaker,
                            source=source,
                            scene=scene,
                            role="text_script_" + kind,
                            ref={
                                "json": relative,
                                "type": "text",
                                "file_index": file_index,
                                "line_index": line_index,
                                "prefix": prefix,
                                "suffix": suffix,
                            },
                        )

    ordered = list(records.values())
    return [
        {key: row[key] for key in ("id", "source", "speaker", "scene", "metadata")}
        for row in ordered
    ], census


def write_inventory_pair(
    source_root: Path,
    batch_dir: Path,
    primary_path: Path,
    independent_path: Path,
    *,
    excluded_json_names: Iterable[str] = (),
) -> tuple[Path, Path]:
    source_root = source_root.resolve()
    primary_path = primary_path.resolve()
    independent_path = independent_path.resolve()
    if primary_path == independent_path:
        raise ValueError("WOLF primary and independent inventories must use different files")
    excluded_json_names = tuple(excluded_json_names)
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8-sig"))
    rows = _primary_rows(batch_dir)
    independent_rows, independent_census = _independent_rows(source_root, excluded_json_names)
    _atomic_json(
        primary_path,
        {
            "schema": "parr0t.source-inventory.v1",
            "method": "wolf-semantic-batch-collector",
            "implementation_fingerprint": "wolf-semantic-batch-collector-v3",
            "coverage": scope_coverage(manifest["raw_occurrences"], len(rows), list(excluded_json_names)),
            "raw_occurrences": manifest["raw_occurrences"],
            "segments": rows,
        },
    )
    _atomic_json(
        independent_path,
        {
            "schema": "parr0t.source-inventory.v1",
            "method": "wolf-independent-json-shape-audit",
            "implementation_fingerprint": "wolf-independent-json-role-audit-v2",
            "coverage": scope_coverage(independent_census, len(independent_rows), list(excluded_json_names)),
            "raw_occurrences": independent_census,
            "segments": independent_rows,
        },
    )
    return primary_path, independent_path


def require_wolf_patch_gate(
    work_dir: Path, primary_path: Path, *, result_path: Path | None = None,
) -> dict[str, Any]:
    inventory = require_inventory_attestation(work_dir, primary_path)
    queue_path = work_dir / ".game-l10n" / "failure-queue.json"
    if not queue_path.is_file():
        return inventory
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    if queue.get("schema") != "parr0t.failure-queue.v1":
        raise ValueError("Failure queue schema is unsupported")
    if queue.get("inventory_fingerprint") != inventory["primary_fingerprint"]:
        raise ValueError("Failure queue is stale for the verified WOLF inventory")
    if int(queue.get("blocking_count", 0)) > 0:
        require_incomplete_artifact(work_dir, inventory, result_path=result_path)
    return inventory
