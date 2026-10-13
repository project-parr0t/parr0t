from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..coverage import require_inventory_attestation


SOURCE_TEXT_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
SPEAKER_RE = re.compile(r"^@mes(?:\s+(.*))?$")
VISIBLE_PATTERNS = (
    (re.compile(r"^@putselect\s+(.*)$"), "choice"),
    (re.compile(r"^@select\s+\S+\s+\S+\s+(.*)$"), "choice_prompt"),
    (re.compile(r"^@toki-teach\s+(.*)$"), "notification"),
    (re.compile(r"^@placename\s+(.*)$"), "place_name"),
    (re.compile(r"^@subtitle\s+\S+\s+(.*)$"), "subtitle"),
    (re.compile(r"^@jimaku\s+(.*)$"), "caption"),
    (re.compile(r"^@mono_txt\s+(.*)$"), "monologue"),
)


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


def _coverage(source_root: Path, excluded_json_names: Iterable[str]) -> dict[str, Any]:
    excluded = sorted({str(name) for name in excluded_json_names})
    exclusions = [
        {
            "reason_code": "WOLF_OPERATOR_EXCLUDED_JSON",
            "affects_user_visible_text": True,
            "scope": name,
        }
        for name in excluded
    ]
    text_root = source_root / "text"
    if text_root.is_dir() and any(
        "debug" in {part.casefold() for part in path.relative_to(text_root).parts}
        for path in text_root.rglob("__strings.json")
    ):
        exclusions.append(
            {
                "reason_code": "WOLF_DEBUG_DIRECTORY",
                "affects_user_visible_text": False,
            }
        )
    return {"declared_complete": not excluded, "exclusions": exclusions}


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
                    },
                }
            )
    return rows


def _visible_payload(value: str) -> tuple[str, str, str, str] | None:
    if not value or value.startswith(("#", "::", "\ufeff::")):
        return None
    if not value.startswith("@"):
        return ("dialogue", value, "", "") if SOURCE_TEXT_RE.search(value) else None
    for pattern, kind in VISIBLE_PATTERNS:
        match = pattern.match(value)
        if match and SOURCE_TEXT_RE.search(match.group(1)):
            source = match.group(1)
            prefix = value[: len(value) - len(source)]
            return kind, source, prefix, ""
    if value.startswith("@sumo_actionlist ") and ",msg," in value:
        prefix, tail = value.split(",msg,", 1)
        body, separator, suffix = tail.partition(";")
        if SOURCE_TEXT_RE.search(body):
            return (
                "sumo_message",
                body,
                prefix + ",msg,",
                separator + suffix if separator else "",
            )
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
    records: dict[str, dict[str, str]],
    *,
    kind: str,
    speaker: str,
    source: str,
    ref: dict[str, Any],
    scene: str = "",
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
    }


def _independent_rows(
    source_root: Path, excluded_json_names: Iterable[str]
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, str]] = {}
    names_path = source_root / "names.json"
    if names_path.is_file():
        payload = json.loads(names_path.read_text(encoding="utf-8-sig"))
        for index, node in enumerate(payload.get("names", [])):
            source = str(node.get("source", ""))
            if node.get("safety") == "safe" and SOURCE_TEXT_RE.search(source):
                _add(
                    records,
                    kind="name",
                    speaker="name/database",
                    source=source,
                    ref={
                        "json": "names.json",
                        "type": "name",
                        "index": index,
                    },
                )

    excluded = {"names.json", "Text_Script.json", *excluded_json_names}
    for path in sorted(source_root.glob("*.json")):
        if path.name in excluded:
            continue
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        relative = path.relative_to(source_root).as_posix()
        for node_path, node in _walk_nodes(payload):
            source = node["source"]
            if SOURCE_TEXT_RE.search(source):
                _add(
                    records,
                    kind="structured",
                    speaker=str(node.get("speaker") or node.get("speaker_src") or "UI/system"),
                    source=source,
                    scene=relative,
                    ref={
                        "json": relative,
                        "type": "structured",
                        "path": list(node_path),
                    },
                )

    text_root = source_root / "text"
    if text_root.is_dir():
        for path in sorted(text_root.rglob("__strings.json")):
            if "debug" in {part.casefold() for part in path.relative_to(text_root).parts}:
                continue
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            relative = path.relative_to(source_root).as_posix()
            for file_index, file_entry in enumerate(payload.get("files", [])):
                speaker = "unknown"
                scene = str(file_entry.get("file", f"file-{file_index}"))
                for line_index, line in enumerate(file_entry.get("lines", [])):
                    source_line = str(line.get("source", ""))
                    speaker_match = SPEAKER_RE.match(source_line)
                    if speaker_match:
                        speaker = (speaker_match.group(1) or "unknown").strip()
                        continue
                    visible = _visible_payload(source_line)
                    if visible is not None:
                        kind, source, prefix, suffix = visible
                        _add(
                            records,
                            kind=kind,
                            speaker=speaker,
                            source=source,
                            scene=scene,
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
        {
            "id": row["id"],
            "source": row["source"],
            "speaker": row["speaker"],
            "scene": row["scene"],
        }
        for row in ordered
    ]


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
    coverage = _coverage(source_root, excluded_json_names)
    _atomic_json(
        primary_path,
        {
            "schema": "parr0t.source-inventory.v1",
            "method": "wolf-semantic-batch-collector",
            "implementation_fingerprint": "wolf-semantic-batch-collector-v2",
            "coverage": coverage,
            "segments": _primary_rows(batch_dir),
        },
    )
    _atomic_json(
        independent_path,
        {
            "schema": "parr0t.source-inventory.v1",
            "method": "wolf-independent-json-shape-audit",
            "implementation_fingerprint": "wolf-independent-json-shape-audit-v1",
            "coverage": coverage,
            "segments": _independent_rows(source_root, excluded_json_names),
        },
    )
    return primary_path, independent_path


def require_wolf_patch_gate(work_dir: Path, primary_path: Path) -> dict[str, Any]:
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
        raise ValueError("WOLF patch output is held while a blocking translation target remains unresolved")
    return inventory
