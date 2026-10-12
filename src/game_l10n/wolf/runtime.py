from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..errors import GameL10nError


QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
COMPARE_RIGHT_RE = re.compile(
    r'(?P<ref>(?:S|CSelf)\[[^\]]+\])\s*(?:eq|==|ne|!=)\s*"(?P<value>(?:[^"\\]|\\.)*)"'
)
COMPARE_LEFT_RE = re.compile(
    r'"(?P<value>(?:[^"\\]|\\.)*)"\s*(?:eq|==|ne|!=)\s*(?P<ref>(?:S|CSelf)\[[^\]]+\])'
)
SET_STRING_RE = re.compile(
    r'SetString(?:\(targetRef=|\s+)(?P<ref>(?:S|CSelf)\[[^\]]+\])'
    r'(?:[^)]*\)\s*|\s*=\s*)'
    r'"(?P<value>(?:[^"\\]|\\.)*)"'
)
RESOURCE_LITERAL_RE = re.compile(
    r"(?i)^(?:.+[\\/])?.+\."
    r"(?:txt|png|jpe?g|bmp|gif|webp|ogg|wav|mp3|mps|wolf|dat|project|ttf|otf|ogv|mp4)$"
)
VISIBLE_SCRIPT_COMMANDS = {
    "@jimaku",
    "@mono_txt",
    "@placename",
    "@putselect",
    "@select",
    "@subtitle",
    "@sumo_actionlist",
    "@toki-teach",
}


class WolfIntegrityError(GameL10nError):
    """Raised when localization changes a WOLF runtime identifier or structure."""


@dataclass(slots=True, frozen=True)
class RuntimeAudit:
    files: int
    lines: int
    text_script_commands: int = 0
    translated_visible_commands: int = 0


def looks_like_resource_literal(value: str) -> bool:
    stripped = value.strip()
    return bool(RESOURCE_LITERAL_RE.fullmatch(stripped)) or stripped.startswith("Text_Script/")


def script_scope_data(path: Path, kind: str) -> tuple[dict[tuple[int, int | None], set[str]], set[str]]:
    if kind not in {"common", "map"}:
        raise ValueError("kind must be 'common' or 'map'")
    controls: dict[tuple[int, int | None], set[str]] = {}
    global_controls: set[str] = set()
    current_event: int | None = None
    current_page: int | None = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if kind == "common":
            match = re.match(r"^(?:@commonevent|commonEvent)\s+(\d+)\b", line)
            if match:
                current_event = int(match.group(1))
                current_page = None
        else:
            match = re.match(r"^event\s+(\d+)\b", line)
            if match:
                current_event = int(match.group(1))
                current_page = None
            match = re.match(r"^\s+page\s+(\d+)\s*\{", line)
            if match:
                current_page = int(match.group(1))
        if current_event is None:
            continue
        scope = (current_event, current_page)
        for pattern in (COMPARE_RIGHT_RE, COMPARE_LEFT_RE):
            for match in pattern.finditer(line):
                value = match.group("value")
                controls.setdefault(scope, set()).add(value)
                if match.group("ref").startswith("S["):
                    global_controls.add(value)
    return controls, global_controls


def _iter_translation_entries(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        if isinstance(node.get("source"), str) and isinstance(node.get("text"), str):
            yield node
        for value in node.values():
            yield from _iter_translation_entries(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_translation_entries(value)


def protect_runtime_identifiers(
    translated_json_root: Path,
    original_scripts: Mapping[str, Path],
    *,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Restore translated values that are runtime keys or resource paths.

    ``original_scripts`` maps names such as ``CommonEvent.dat`` and ``Map001.mps``
    to their original decompiled WScript files.
    """
    repairs: list[dict[str, Any]] = []
    for json_path in sorted(translated_json_root.glob("*.json")):
        document = json.loads(json_path.read_text(encoding="utf-8-sig"))
        scenes = document.get("scenes")
        if not isinstance(scenes, list):
            continue
        runtime_name = json_path.name.removesuffix(".json")
        script_path = original_scripts.get(runtime_name)
        if script_path is None:
            continue
        kind = "common" if runtime_name == "CommonEvent.dat" else "map"
        scoped_controls, global_controls = script_scope_data(script_path, kind)
        changed = False
        for scene in scenes:
            scope = (scene.get("event"), scene.get("page") if kind == "map" else None)
            protected = scoped_controls.get(scope, set()) | global_controls
            for entry in scene.get("lines", []):
                source = entry.get("source")
                target = entry.get("text")
                if not isinstance(source, str) or source == target or source not in protected:
                    continue
                repairs.append(
                    {
                        "file": runtime_name,
                        "event": scene.get("event"),
                        "page": scene.get("page"),
                        "reason": "string comparison key",
                        "source": source,
                    }
                )
                entry["text"] = source
                changed = True
        if changed:
            json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for json_path in sorted(translated_json_root.rglob("*.json")):
        document = json.loads(json_path.read_text(encoding="utf-8-sig"))
        changed = False
        for entry in _iter_translation_entries(document):
            source = entry["source"]
            if source != entry["text"] and looks_like_resource_literal(source):
                repairs.append(
                    {
                        "file": json_path.relative_to(translated_json_root).as_posix(),
                        "reason": "resource or script path",
                        "source": source,
                    }
                )
                entry["text"] = source
                changed = True
        if changed:
            json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {"restored_count": len(repairs), "repairs": repairs}
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _current_scope(line: str, kind: str, state: tuple[int | None, int | None]) -> tuple[int | None, int | None]:
    event, page = state
    if kind == "common":
        match = re.match(r"^(?:@commonevent|commonEvent)\s+(\d+)\b", line)
        return (int(match.group(1)), None) if match else state
    match = re.match(r"^event\s+(\d+)\b", line)
    if match:
        return int(match.group(1)), None
    match = re.match(r"^\s+page\s+(\d+)\s*\{", line)
    return (event, int(match.group(1))) if match else state


def validate_decompiled_pair(name: str, original_path: Path, localized_path: Path) -> int:
    original_lines = original_path.read_text(encoding="utf-8-sig").splitlines()
    localized_lines = localized_path.read_text(encoding="utf-8-sig").splitlines()
    if len(original_lines) != len(localized_lines):
        raise WolfIntegrityError(
            f"Runtime line count changed for {name}: {len(original_lines)} -> {len(localized_lines)}"
        )
    kind = "common" if name == "CommonEvent.dat" else "map"
    scoped_controls, global_controls = script_scope_data(original_path, kind)
    state: tuple[int | None, int | None] = (None, None)
    errors: list[str] = []
    for line_number, (original, localized) in enumerate(zip(original_lines, localized_lines), 1):
        state = _current_scope(original, kind, state)
        original_quotes = QUOTED_RE.findall(original)
        localized_quotes = QUOTED_RE.findall(localized)
        if "CommonEventByName(" in original or re.match(r'^\s*call\s+"', original):
            if not original_quotes or not localized_quotes or original_quotes[0] != localized_quotes[0]:
                errors.append(f"line {line_number}: called event name changed")
        if "Database(" in original and original_quotes[1:] != localized_quotes[1:]:
            errors.append(f"line {line_number}: database lookup identifier changed")
        original_resources = [value for value in original_quotes if looks_like_resource_literal(value)]
        localized_resources = [value for value in localized_quotes if looks_like_resource_literal(value)]
        if original_resources != localized_resources:
            errors.append(f"line {line_number}: resource/script path changed")
        setter = SET_STRING_RE.search(original)
        if setter and setter.group("value") in (scoped_controls.get(state, set()) | global_controls):
            localized_setter = SET_STRING_RE.search(localized)
            if localized_setter is None or localized_setter.group("value") != setter.group("value"):
                errors.append(f"line {line_number}: string branch key changed")
    if errors:
        preview = "\n".join(errors[:80])
        raise WolfIntegrityError(f"Runtime WScript audit failed for {name}: {len(errors)} issue(s)\n{preview}")
    return len(original_lines)


def _command_skeleton(line: str) -> tuple[str, ...]:
    stripped = line.lstrip()
    parts = stripped.split()
    if not parts:
        return ()
    verb = parts[0]
    if verb in {"@putselect", "@placename", "@toki-teach"}:
        return (verb,)
    if verb == "@select":
        return tuple(parts[:3])
    if verb in {"@jimaku", "@mono_txt", "@subtitle"}:
        return tuple(parts[:2])
    if verb == "@sumo_actionlist":
        match = re.match(r"^(.*?,msg,).*(,\d+;\s*)$", stripped)
        return (verb, match.group(1), match.group(2)) if match else (stripped,)
    return (stripped,)


def validate_text_script_commands(source_root: Path, localized_root: Path) -> tuple[int, int]:
    errors: list[str] = []
    command_count = 0
    translated_visible = 0
    for source in sorted(source_root.rglob("*.txt")):
        relative = source.relative_to(source_root)
        localized = localized_root / relative
        if not localized.is_file():
            errors.append(f"missing text script: {relative}")
            continue
        source_commands = [
            line.rstrip()
            for line in source.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if line.lstrip().startswith("@")
        ]
        localized_commands = [
            line.rstrip()
            for line in localized.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if line.lstrip().startswith("@")
        ]
        command_count += len(source_commands)
        if len(source_commands) != len(localized_commands):
            errors.append(f"command count changed: {relative}")
            continue
        for index, (original, translated) in enumerate(zip(source_commands, localized_commands), 1):
            if original == translated:
                continue
            verb = original.lstrip().split(maxsplit=1)[0]
            if verb not in VISIBLE_SCRIPT_COMMANDS:
                errors.append(f"runtime command changed: {relative} command#{index}")
            elif _command_skeleton(original) != _command_skeleton(translated):
                errors.append(f"visible command structure changed: {relative} command#{index}")
            else:
                translated_visible += 1
    if errors:
        raise WolfIntegrityError(
            f"Text-script runtime audit failed: {len(errors)} issue(s)\n" + "\n".join(errors[:50])
        )
    return command_count, translated_visible


def audit_runtime_scripts(
    original_scripts: Mapping[str, Path],
    localized_scripts: Mapping[str, Path],
    *,
    original_text_root: Path | None = None,
    localized_text_root: Path | None = None,
    report_path: Path | None = None,
) -> RuntimeAudit:
    missing = sorted(set(original_scripts) - set(localized_scripts))
    if missing:
        raise WolfIntegrityError(f"Localized runtime scripts are missing: {', '.join(missing)}")
    total_lines = sum(
        validate_decompiled_pair(name, original_scripts[name], localized_scripts[name])
        for name in sorted(original_scripts)
    )
    commands = 0
    visible = 0
    if (original_text_root is None) != (localized_text_root is None):
        raise ValueError("Both original_text_root and localized_text_root are required together")
    if original_text_root is not None and localized_text_root is not None:
        commands, visible = validate_text_script_commands(original_text_root, localized_text_root)
    result = RuntimeAudit(len(original_scripts), total_lines, commands, visible)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    return result
