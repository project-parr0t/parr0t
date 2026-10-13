from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


BATCH_FORMAT = "wolf-rpg-agent-batch-v2"
LEGACY_BATCH_FORMAT = "wolf-rpg-agent-batch-v1"
MANUAL_QUEUE_FORMAT = "wolf-rpg-manual-translation-queue-v1"
SOURCE_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
CONTROL_RE = re.compile(
    r"\\(?:[A-Za-z_]+(?:\[[^\]]*\])?|[><!\.\^\\])"
    r"|<[A-Za-z][A-Za-z0-9_:+?=/\-]*>|@[0-9]+|\$[A-Za-z_][A-Za-z0-9_]*"
)
MES_RE = re.compile(r"^@mes(?:\s+(.*))?$")
VISIBLE_COMMANDS = (
    (re.compile(r"^(@putselect\s+)(.*)$"), "choice"),
    (re.compile(r"^(@select\s+\S+\s+\S+\s+)(.*)$"), "choice_prompt"),
    (re.compile(r"^(@toki-teach\s+)(.*)$"), "notification"),
    (re.compile(r"^(@placename\s+)(.*)$"), "place_name"),
    (re.compile(r"^(@subtitle\s+\S+\s+)(.*)$"), "subtitle"),
    (re.compile(r"^(@jimaku\s+)(.*)$"), "caption"),
    (re.compile(r"^(@mono_txt\s+)(.*)$"), "monologue"),
)


def controls(text: str) -> list[str]:
    return CONTROL_RE.findall(text)


def _has_source_text(text: str) -> bool:
    return bool(SOURCE_RE.search(text))


def _make_id(kind: str, speaker: str, source: str, ref: dict[str, Any]) -> str:
    location = json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = f"{kind}\0{speaker}\0{source}\0{location}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _visible_payload(source: str) -> tuple[str, str, str, str] | None:
    if not source or source.startswith(("#", "::", "\ufeff::")):
        return None
    if not source.startswith("@"):
        return "dialogue", "", source, ""
    for pattern, kind in VISIBLE_COMMANDS:
        match = pattern.match(source)
        if match and _has_source_text(match.group(2)):
            return kind, match.group(1), match.group(2), ""
    if source.startswith("@sumo_actionlist ") and ",msg," in source:
        prefix, rest = source.split(",msg,", 1)
        if ";" in rest:
            payload, suffix = rest.split(";", 1)
            if _has_source_text(payload):
                return "sumo_message", prefix + ",msg,", payload, ";" + suffix
    return None


def _walk_source_nodes(value: Any, path: tuple[Any, ...] = ()) -> Iterable[tuple[tuple[Any, ...], dict[str, Any]]]:
    if isinstance(value, dict):
        if isinstance(value.get("source"), str) and isinstance(value.get("text"), str):
            yield path, value
        for key, child in value.items():
            yield from _walk_source_nodes(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_source_nodes(child, path + (index,))


def _context_neighbors(lines: list[dict[str, Any]], index: int) -> tuple[str, str]:
    previous = ""
    following = ""
    for position in range(index - 1, -1, -1):
        payload = _visible_payload(str(lines[position].get("source", "")))
        if payload:
            previous = payload[2]
            break
    for position in range(index + 1, len(lines)):
        payload = _visible_payload(str(lines[position].get("source", "")))
        if payload:
            following = payload[2]
            break
    return previous, following


def _add_item(
    items: dict[str, dict[str, Any]],
    *,
    kind: str,
    speaker: str,
    source: str,
    context: dict[str, Any],
    ref: dict[str, Any],
) -> None:
    item_id = _make_id(kind, speaker, source, ref)
    if item_id in items:
        raise ValueError(f"Duplicate WOLF source location: {item_id}")
    items[item_id] = {
        "id": item_id,
        "kind": kind,
        "speaker": speaker or "unknown",
        "source": source,
        "context": context,
        "target": "",
        "refs": [ref],
    }


def _collect_text_json(path: Path, root: Path, items: dict[str, dict[str, Any]]) -> None:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    relative = path.relative_to(root).as_posix()
    for file_index, file_entry in enumerate(data.get("files", [])):
        filename = file_entry.get("file", f"file-{file_index}")
        lines = file_entry.get("lines", [])
        speaker = "unknown"
        for line_index, line in enumerate(lines):
            source = str(line.get("source", ""))
            speaker_match = MES_RE.match(source)
            if speaker_match:
                speaker = (speaker_match.group(1) or "unknown").strip()
                continue
            payload = _visible_payload(source)
            if not payload:
                continue
            kind, prefix, text, suffix = payload
            if not _has_source_text(text):
                continue
            previous, following = _context_neighbors(lines, line_index)
            _add_item(
                items,
                kind=kind,
                speaker=speaker,
                source=text,
                context={"file": filename, "previous": previous, "next": following},
                ref={
                    "json": relative,
                    "type": "text",
                    "file_index": file_index,
                    "line_index": line_index,
                    "prefix": prefix,
                    "suffix": suffix,
                },
            )


def _collect_structured_json(path: Path, root: Path, items: dict[str, dict[str, Any]]) -> None:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    relative = path.relative_to(root).as_posix()
    for node_path, node in _walk_source_nodes(data):
        source = node["source"]
        if not _has_source_text(source):
            continue
        speaker = str(node.get("speaker") or node.get("speaker_src") or "UI/system")
        _add_item(
            items,
            kind="structured",
            speaker=speaker,
            source=source,
            context={"file": relative},
            ref={"json": relative, "type": "structured", "path": list(node_path)},
        )


def _collect_names(path: Path, root: Path, items: dict[str, dict[str, Any]]) -> None:
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    relative = path.relative_to(root).as_posix()
    for index, node in enumerate(data.get("names", [])):
        source = str(node.get("source", ""))
        if node.get("safety") != "safe" or not _has_source_text(source):
            continue
        _add_item(
            items,
            kind="name",
            speaker="name/database",
            source=source,
            context={"note": node.get("note", "")},
            ref={"json": relative, "type": "name", "index": index},
        )


def prepare_batches(
    source_root: Path,
    output_dir: Path,
    *,
    max_chars: int = 9000,
    translated_copy: Path | None = None,
    excluded_json_names: Iterable[str] = (),
) -> dict[str, Any]:
    """Collect translatable WOLF JSON occurrences into stable source-order agent batches."""
    source_root = source_root.resolve()
    output_dir = output_dir.resolve()
    if max_chars < 500:
        raise ValueError("max_chars must be at least 500")
    if translated_copy is not None:
        translated_copy = translated_copy.resolve()
        if translated_copy.exists():
            raise FileExistsError(f"Translated copy destination already exists: {translated_copy}")
        shutil.copytree(source_root, translated_copy)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise FileExistsError(f"Batch output directory must be empty: {output_dir}")

    items: dict[str, dict[str, Any]] = {}
    _collect_names(source_root / "names.json", source_root, items)
    excluded = {"names.json", "Text_Script.json", *excluded_json_names}
    for path in sorted(source_root.glob("*.json")):
        if path.name not in excluded:
            _collect_structured_json(path, source_root, items)
    text_root = source_root / "text"
    if text_root.exists():
        for path in sorted(text_root.rglob("__strings.json")):
            relative_parts = {part.casefold() for part in path.relative_to(text_root).parts}
            if "debug" not in relative_parts:
                _collect_text_json(path, source_root, items)

    ordered = list(items.values())
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for item in ordered:
        item_chars = len(item["source"]) + len(json.dumps(item["context"], ensure_ascii=False)) + 100
        if current and current_chars + item_chars > max_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        batches.append(current)

    manifest: dict[str, Any] = {
        "format": BATCH_FORMAT,
        "item_count": len(ordered),
        "occurrence_count": sum(len(item["refs"]) for item in ordered),
        "batch_count": len(batches),
        "kind_counts": dict(Counter(item["kind"] for item in ordered)),
        "speaker_counts": dict(Counter(item["speaker"] for item in ordered)),
        "batches": [],
    }
    for index, batch in enumerate(batches, 1):
        filename = f"batch_{index:04d}.json"
        payload = {
            "format": BATCH_FORMAT,
            "instructions": (
                "Translate source faithfully using speaker and context. Fill target only. Preserve every "
                "control code, variable, tag, number, and newline. Do not add explanations."
            ),
            "items": batch,
        }
        (output_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        manifest["batches"].append(
            {"file": filename, "items": len(batch), "chars": sum(len(item["source"]) for item in batch)}
        )
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def _get_at_path(root: Any, path: list[Any]) -> Any:
    value = root
    for part in path:
        value = value[part]
    return value


def _validate_target(source: str, target: str) -> list[str]:
    errors: list[str] = []
    if controls(source) != controls(target):
        errors.append("control-code mismatch")
    if source.count("\n") != target.count("\n"):
        errors.append("newline mismatch")
    return errors


def apply_batches(
    batch_dir: Path,
    translated_root: Path,
    *,
    allow_incomplete: bool = False,
) -> dict[str, Any]:
    documents: dict[str, Any] = {}
    changed_documents: set[str] = set()
    errors: list[dict[str, Any]] = []
    applied = 0
    manual_items = 0
    manual_occurrences = 0
    for batch_path in sorted(batch_dir.glob("batch_*.json")):
        data = json.loads(batch_path.read_text(encoding="utf-8-sig"))
        if data.get("format") not in {None, BATCH_FORMAT, LEGACY_BATCH_FORMAT}:
            errors.append({"batch": batch_path.name, "error": "unsupported batch format"})
            continue
        for item in data.get("items", []):
            target = item.get("target", "")
            if not target:
                if item.get("manual") is True:
                    manual_items += 1
                    manual_occurrences += len(item.get("refs", []))
                elif not allow_incomplete:
                    errors.append({"batch": batch_path.name, "id": item.get("id"), "error": "empty target"})
                continue
            item_errors = _validate_target(str(item.get("source", "")), str(target))
            if item_errors:
                errors.append({"batch": batch_path.name, "id": item.get("id"), "errors": item_errors})
                continue
            for ref in item.get("refs", []):
                relative = str(ref["json"])
                if relative not in documents:
                    path = translated_root / Path(relative)
                    documents[relative] = json.loads(path.read_text(encoding="utf-8-sig"))
                document = documents[relative]
                if ref["type"] == "structured":
                    _get_at_path(document, ref["path"])["text"] = target
                elif ref["type"] == "name":
                    document["names"][ref["index"]]["text"] = target
                elif ref["type"] == "text":
                    line = document["files"][ref["file_index"]]["lines"][ref["line_index"]]
                    line["text"] = ref.get("prefix", "") + target + ref.get("suffix", "")
                else:
                    errors.append({"batch": batch_path.name, "id": item.get("id"), "error": "unknown reference type"})
                    continue
                changed_documents.add(relative)
                applied += 1
    if errors and not allow_incomplete:
        report = {
            "applied_occurrences": 0,
            "changed_documents": 0,
            "manual_items": manual_items,
            "manual_occurrences": manual_occurrences,
            "errors": errors,
        }
        (batch_dir / "apply_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        raise ValueError(f"Batch validation failed with {len(errors)} issue(s)")
    for relative in sorted(changed_documents):
        path = translated_root / Path(relative)
        path.write_text(json.dumps(documents[relative], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "applied_occurrences": applied,
        "changed_documents": len(changed_documents),
        "manual_items": manual_items,
        "manual_occurrences": manual_occurrences,
        "errors": errors,
    }
    (batch_dir / "apply_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def import_reviewed_results(reviewed_path: Path, batch_dir: Path) -> dict[str, Any]:
    payload = json.loads(reviewed_path.read_text(encoding="utf-8-sig"))
    rows = payload.get("results", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise ValueError("Reviewed WOLF result artifact must contain a results list")
    reviewed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Reviewed WOLF result rows must be objects")
        target_id = str(row.get("segment_id") or row.get("id") or "")
        if not target_id or target_id in reviewed:
            raise ValueError(f"Missing or duplicate reviewed WOLF target: {target_id!r}")
        reviewed[target_id] = row

    documents: dict[Path, dict[str, Any]] = {}
    items: dict[str, dict[str, Any]] = {}
    for batch_path in sorted(batch_dir.glob("batch_*.json")):
        document = json.loads(batch_path.read_text(encoding="utf-8-sig"))
        documents[batch_path] = document
        for item in document.get("items", []):
            target_id = str(item.get("id", ""))
            if not target_id or target_id in items:
                raise ValueError(f"Missing or duplicate WOLF batch target: {target_id!r}")
            items[target_id] = item
    if set(reviewed) != set(items):
        missing = len(set(items) - set(reviewed))
        extra = len(set(reviewed) - set(items))
        raise ValueError(
            f"Reviewed WOLF result set does not match batches (missing={missing}, extra={extra})"
        )

    source_preserved = 0
    for target_id, item in items.items():
        row = reviewed[target_id]
        status = str(row.get("status", ""))
        if status not in {"reviewed", "source_preserved"}:
            raise ValueError(f"WOLF target is not patchable: {target_id} ({status or 'missing status'})")
        source = str(item.get("source", ""))
        if row.get("source") != source:
            raise ValueError(f"Reviewed WOLF source changed: {target_id}")
        target = row.get("text")
        if not isinstance(target, str) or not target:
            raise ValueError(f"Reviewed WOLF target is empty: {target_id}")
        if status == "source_preserved":
            source_preserved += 1
            if target != source:
                raise ValueError(f"Source-preserved WOLF target changed source text: {target_id}")
        errors = _validate_target(source, target)
        if errors:
            raise ValueError(f"Reviewed WOLF target failed integrity checks: {target_id}")

    for target_id, item in items.items():
        item["target"] = reviewed[target_id]["text"]
        item.pop("manual", None)
    for path, document in documents.items():
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return {
        "imported": len(items),
        "source_preserved": source_preserved,
        "batches": len(documents),
    }


def export_manual_queue(
    batch_dir: Path,
    output_path: Path,
    *,
    sample_path: Path | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for batch_path in sorted(batch_dir.glob("batch_*.json")):
        batch = json.loads(batch_path.read_text(encoding="utf-8-sig"))
        for item in batch.get("items", []):
            if item.get("manual") is True:
                items.append(
                    {
                        "batch": batch_path.name,
                        "id": item["id"],
                        "kind": item.get("kind", ""),
                        "speaker": item.get("speaker", ""),
                        "source": item.get("source", ""),
                        "context": item.get("context", {}),
                        "target": item.get("target", ""),
                    }
                )
    queue = {
        "format": MANUAL_QUEUE_FORMAT,
        "instructions": [
            "Fill target with the localized text.",
            "Preserve source control codes and newline count.",
            "Do not change batch, id, or source.",
        ],
        "item_count": len(items),
        "items": items,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if sample_path is not None:
        sample_item = {
            "batch": "batch_0000.json",
            "id": "synthetic_fixture_item",
            "kind": "dialogue",
            "speaker": "fixture-speaker",
            "source": "SOURCE_FIXTURE_TEXT",
            "context": {"note": "synthetic format fixture"},
            "target": "TARGET_FIXTURE_TEXT",
        }
        sample = {**queue, "item_count": 1, "items": [sample_item]}
        sample_path.parent.mkdir(parents=True, exist_ok=True)
        sample_path.write_text(json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return queue


def merge_manual_queue(queue_path: Path, batch_dir: Path, *, report_path: Path | None = None) -> dict[str, Any]:
    queue = json.loads(queue_path.read_text(encoding="utf-8-sig"))
    if queue.get("format") != MANUAL_QUEUE_FORMAT:
        raise ValueError("Unsupported manual translation queue format")
    loaded: dict[str, dict[str, Any]] = {}
    indexes: dict[str, dict[str, dict[str, Any]]] = {}
    candidates: list[tuple[str, dict[str, Any], str]] = []
    errors: list[dict[str, Any]] = []
    for entry in queue.get("items", []):
        batch_name = str(entry.get("batch", ""))
        item_id = str(entry.get("id", ""))
        target = entry.get("target", "")
        if not isinstance(target, str):
            errors.append({"batch": batch_name, "id": item_id, "error": "target must be a string"})
            continue
        if not target:
            continue
        if batch_name not in loaded:
            batch_path = batch_dir / batch_name
            if not batch_path.is_file():
                errors.append({"batch": batch_name, "id": item_id, "error": "batch file not found"})
                continue
            loaded[batch_name] = json.loads(batch_path.read_text(encoding="utf-8-sig"))
            indexes[batch_name] = {item["id"]: item for item in loaded[batch_name].get("items", [])}
        original = indexes[batch_name].get(item_id)
        if original is None:
            errors.append({"batch": batch_name, "id": item_id, "error": "item not found"})
        elif original.get("manual") is not True:
            errors.append({"batch": batch_name, "id": item_id, "error": "item is not marked manual"})
        elif entry.get("source") != original.get("source"):
            errors.append({"batch": batch_name, "id": item_id, "error": "source changed"})
        else:
            item_errors = _validate_target(original["source"], target)
            if item_errors:
                errors.append({"batch": batch_name, "id": item_id, "errors": item_errors})
            else:
                candidates.append((batch_name, original, target))
    report_path = report_path or queue_path.with_name("manual_merge_report.json")
    if errors:
        report = {"merged": 0, "remaining_manual": None, "errors": errors}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise ValueError(f"Manual queue validation failed with {len(errors)} issue(s)")
    changed: set[str] = set()
    for batch_name, original, target in candidates:
        original["target"] = target
        original.pop("manual", None)
        changed.add(batch_name)
    for batch_name in sorted(changed):
        (batch_dir / batch_name).write_text(
            json.dumps(loaded[batch_name], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    remaining = 0
    for batch_path in sorted(batch_dir.glob("batch_*.json")):
        data = loaded.get(batch_path.name) or json.loads(batch_path.read_text(encoding="utf-8-sig"))
        remaining += sum(item.get("manual") is True for item in data.get("items", []))
    report = {"merged": len(candidates), "changed_batches": sorted(changed), "remaining_manual": remaining, "errors": []}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
