"""Lossless display primitives and a source-driven inventory auditor for MV 1.6.1."""
from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any, Iterable, Mapping


LEXICAL = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`(?:\\.|[^`\\])*`', re.S)
DISPLAY_CALL = re.compile(r'(?:\.drawText(?:Ex)?|\.addCommand|\.setText|\$gameMessage\.add|\.setName|\.setNickname|\.setProfile)\s*\(\s*')
MAP_INFO = re.compile(r'\$dataMapInfos\s*\[\s*(\d+)\s*\]\s*\.\s*name')


def pointer(path: Iterable[Any]) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)


def target_id(filename: str, path: list[Any], span: tuple[int, int] | None = None) -> str:
    identity = f"{filename}#{pointer(path)}#{span or ''}"
    return "mv-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def is_display_text(value: Any) -> bool:
    from .adapter import PROTECTED_TOKEN_RE
    return isinstance(value, str) and bool(PROTECTED_TOKEN_RE.sub("", value).strip())


def binding_metadata(filename: str, path: list[Any], source: str, kind: str,
                     span: tuple[int, int] | None = None) -> dict[str, Any]:
    binding = f"mv:{filename}#{pointer(path)}#{span or ''}:{kind}"
    evidence = hashlib.sha256(binding.encode()).hexdigest()
    return {
        "display_binding_id": binding,
        "storage_role": "display_projection" if kind == "script_display_projection" else "display_text",
        "display_scope_evidence": {
            "kind": "positive_display_binding",
            "evidence_fingerprint": evidence,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "consumer_role": kind,
        },
    }


def script_displays(text: str, documents: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
    """Recognize complete first arguments, never literals in comments or code strings.

    A static MapInfos name read is projected at the display operand; its shared
    editor/control storage is never changed. Other expressions stay pending.
    """
    from .adapter import RpgMakerMvError, decode_js_literal
    masked = list(text)
    literals: dict[int, re.Match[str]] = {}
    comments: dict[int, int] = {}
    for token in LEXICAL.finditer(text):
        if token[0][0] in {'"', "'"}:
            literals[token.start()] = token
        elif token[0].startswith(("//", "/*")):
            comments[token.start()] = token.end()
        masked[token.start():token.end()] = " " * len(token[0])
    code = "".join(masked)
    displays: list[dict[str, Any]] = []
    pending: list[tuple[int, int]] = []
    for call in DISPLAY_CALL.finditer(code):
        # Whitespace in the masked string includes the first literal: find the
        # argument start in original code instead of consuming masked spaces.
        start = text.index("(", call.start(), call.end()) + 1
        while start < len(text):
            if text[start].isspace():
                start += 1
            elif start in comments:
                start = comments[start]
            else:
                break
        literal = literals.get(start)
        if literal and re.match(r"\s*[,)]", text[literal.end():]):
            try:
                value = decode_js_literal(literal[0])
            except RpgMakerMvError:
                pending.append((call.start(), literal.end()))
            else:
                displays.append({"source": value, "span": literal.span(),
                                 "quote": literal[0][0], "kind": "script_display_text"})
            continue
        reference = MAP_INFO.match(code, start)
        if reference and re.match(r"\s*[,)]", text[reference.end():]):
            index = int(reference[1])
            infos = documents.get("MapInfos.json", [])
            if isinstance(infos, list) and index < len(infos) and isinstance(infos[index], dict):
                value = infos[index].get("name")
                if isinstance(value, str):
                    displays.append({"source": value, "span": reference.span(), "quote": '"',
                                     "kind": "script_display_projection",
                                     "projection": {"file": "MapInfos.json", "path": [index, "name"],
                                                    "expression": text[reference.start():reference.end()]}})
                    continue
        pending.append((call.start(), start))
    return displays, pending


def structural_facts(value: Any) -> Any:
    """Retain flow/control facts, not arbitrary source string payloads."""
    if isinstance(value, dict):
        return {key: structural_facts(child) for key, child in value.items() if not isinstance(child, str)}
    if isinstance(value, list):
        return [structural_facts(child) for child in value if not isinstance(child, str)]
    return value


class IndependentInventory:
    """Mutable accounting state; deliberately has no extraction methods."""

    def __init__(self, segments: list[dict[str, Any]]) -> None:
        from collections import Counter
        self.segments = segments
        self.mappings: list[dict[str, Any]] = []
        self.contexts: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()


def _audit_script_operands(text: str, documents: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Source-operand audit, independent of the primary call-first selector."""
    from .adapter import RpgMakerMvError, decode_js_literal
    tokens = list(LEXICAL.finditer(text))
    masked = list(text)
    for token in tokens:
        masked[token.start():token.end()] = " " * len(token[0])
    code = "".join(masked)
    sink_suffix = re.compile(
        r"(?:\.drawText(?:Ex)?|\.addCommand|\.setText|\$gameMessage\.add|\.setName|\.setNickname|\.setProfile)\s*\(\s*$"
    )
    found: list[dict[str, Any]] = []
    for token in tokens:
        if token[0][0] not in {'"', "'"} or not sink_suffix.search(code[:token.start()]):
            continue
        if not re.match(r"\s*[,)]", text[token.end():]):
            continue
        try:
            value = decode_js_literal(token[0])
        except RpgMakerMvError:
            continue
        found.append({"source": value, "span": token.span(), "kind": "script_display_text"})
    infos = documents.get("MapInfos.json", [])
    for reference in MAP_INFO.finditer(code):
        if not sink_suffix.search(code[:reference.start()]) or not re.match(r"\s*[,)]", text[reference.end():]):
            continue
        index = int(reference[1])
        if isinstance(infos, list) and index < len(infos) and isinstance(infos[index], dict):
            value = infos[index].get("name")
            if isinstance(value, str):
                found.append({"source": value, "span": reference.span(), "kind": "script_display_projection"})
    return sorted(found, key=lambda item: item["span"])


def independent_segments(documents: Mapping[str, Any], sources: Mapping[str, str]) -> list[dict[str, Any]]:
    """Audit source locations without invoking the primary extractor or its walkers.

    This enumerator owns its field/command selection and grouping. Only literal
    parsing, stable identity, display evidence, and token rules are shared.
    """
    from .adapter import PLUGIN_DISPLAY_RE
    rows: list[dict[str, Any]] = []

    def record(filename: str, path: list[Any], value: Any, kind: str, scene: str,
               *, span: tuple[int, int] | None = None, field: str = "",
               identity_path: list[Any] | None = None) -> None:
        if not is_display_text(value):
            return
        identity_path = path if identity_path is None else identity_path
        metadata = {"kind": kind, "file": filename, "json_pointer": pointer(identity_path),
                    **binding_metadata(filename, identity_path, value, kind, span)}
        if kind == "database" and field.startswith("message"):
            metadata.update(ui_slot_role="template_fragment", ui_render_pattern="{name}{value}")
        rows.append({"id": target_id(filename, identity_path, span), "source": value,
                     "scene": scene, "metadata": metadata})

    # Explicit runtime-rendered database roles, not a recursive string harvest.
    fields_by_file = {
        "Actors.json": ("name", "nickname", "profile"), "Classes.json": ("name",),
        "Skills.json": ("name", "description", "message1", "message2"),
        "Items.json": ("name", "description"), "Weapons.json": ("name", "description"),
        "Armors.json": ("name", "description"), "Enemies.json": ("name",),
        "States.json": ("name", "message1", "message2", "message3", "message4"),
    }
    for filename, fields in fields_by_file.items():
        for index, entry in enumerate(documents.get(filename, [])):
            if isinstance(entry, dict):
                for field in fields:
                    record(filename, [index, field], entry.get(field), "database", filename, field=field)

    def string_leaves(value: Any, path: list[Any]) -> Iterable[tuple[list[Any], str]]:
        if isinstance(value, str):
            yield path, value
        elif isinstance(value, (dict, list)):
            children = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in children:
                yield from string_leaves(child, path + [key])

    system = documents.get("System.json", {})
    for field in ("gameTitle", "currencyUnit", "armorTypes", "elements", "equipTypes", "skillTypes", "weaponTypes", "terms"):
        for path, value in string_leaves(system.get(field), [field]):
            record("System.json", path, value, "system_ui", "system")

    def command_locations(filename: str, document: Any) -> Iterable[tuple[list[Any], list[Any], str]]:
        # Separate schema traversal from the primary event-page traversal.
        for path, _ in string_leaves(document, []):
            if len(path) < 4 or "list" not in path:
                continue
            list_at = len(path) - 1 - path[::-1].index("list")
            base = path[:list_at + 1]
            key = tuple(base)
            if key in seen_lists:
                continue
            seen_lists.add(key)
            commands = document
            for part in base:
                commands = commands[part]
            if filename == "CommonEvents.json" and len(base) == 2:
                scene = f"common:{base[0]}"
            elif filename == "Troops.json" and len(base) == 4 and base[1] == "pages":
                scene = f"troop:{base[0]}:page:{base[2]}"
            elif re.fullmatch(r"Map\d+\.json", filename) and len(base) == 5 and base[0] == "events" and base[2] == "pages":
                scene = f"map:{int(filename[3:-5])}:event:{base[1]}:page:{base[3]}"
            else:
                continue
            if isinstance(commands, list):
                yield base, commands, scene

    filenames = ["CommonEvents.json", "Troops.json"] + sorted(
        (name for name in documents if re.fullmatch(r"Map\d+\.json", name)), key=lambda name: int(name[3:-5]))
    for filename in filenames:
        document = documents.get(filename, [])
        if filename.startswith("Map"):
            record(filename, ["displayName"], document.get("displayName"), "map_name", f"map:{int(filename[3:-5])}")
        seen_lists: set[tuple[Any, ...]] = set()
        for base, commands, scene in command_locations(filename, document):
            consumed: set[int] = set()
            for index, command in enumerate(commands):
                if index in consumed or not isinstance(command, dict):
                    continue
                code = command.get("code")
                parameters = command.get("parameters", [])
                path = base + [index, "parameters"]
                if code in {401, 405}:
                    lines = []
                    cursor = index
                    while cursor < len(commands) and commands[cursor].get("code") == code:
                        lines.append(commands[cursor]["parameters"][0])
                        consumed.add(cursor)
                        cursor += 1
                    record(filename, path + [0], "\n".join(lines), "dialogue" if code == 401 else "scrolling_text", scene)
                elif code == 102 and parameters and isinstance(parameters[0], list):
                    for choice, value in enumerate(parameters[0]):
                        record(filename, path + [0, choice], value, "choice", scene)
                elif code in {320, 324, 325} and len(parameters) >= 2:
                    record(filename, path + [1], parameters[1], "actor_text", scene)
                elif code == 356 and parameters and isinstance(parameters[0], str):
                    match = PLUGIN_DISPLAY_RE.match(parameters[0])
                    if match:
                        record(filename, path + [0], match[1], "plugin_display_text", scene, span=match.span(1))
                elif code in {355, 655} and parameters and isinstance(parameters[0], str):
                    for item in _audit_script_operands(parameters[0], documents):
                        record(filename, path + [0], item["source"], item["kind"], scene, span=item["span"])

    for filename, text in sorted(sources.items()):
        if filename.endswith(".js"):
            for item in _audit_script_operands(text, documents):
                record(filename, ["text"], item["source"], item["kind"], filename, span=item["span"], identity_path=[])
        elif filename.endswith(".html"):
            for match in re.finditer(r"<title\b[^>]*>(.*?)</title\s*>", text, re.I | re.S):
                record(filename, ["text"], html.unescape(match[1]), "html_title", filename, identity_path=["title"])
    return rows


def source_census(documents: Mapping[str, Any], sources: Mapping[str, str]) -> dict[str, Any]:
    """Count raw source occurrences separately from supported display targets.

    Records carry identities/digests only. Unknown string values are unclassified,
    not made eligible by reachability, naming heuristics, or a plugin's presence.
    """
    records: list[tuple[str, str, str]] = []
    pending: list[dict[str, Any]] = []

    def walk(filename: str, value: Any, path: list[Any]) -> None:
        from .adapter import DATABASE_FIELDS, PLUGIN_DISPLAY_RE
        if isinstance(value, str):
            if not value:
                return
            role = "unclassified"
            if path and path[-1] == "note":
                role = "control_only"
            elif path and path[-1] == "name" and (
                filename in {"MapInfos.json", "CommonEvents.json", "Troops.json"} or "events" in path
            ):
                role = "control_only"
            elif filename in DATABASE_FIELDS and len(path) == 2 and path[1] in DATABASE_FIELDS[filename]:
                role = "display" if is_display_text(value) else "control_only"
            elif filename == "System.json" and path and path[0] in {
                "gameTitle", "currencyUnit", "armorTypes", "elements", "equipTypes", "skillTypes", "weaponTypes", "terms"
            }:
                role = "display" if is_display_text(value) else "control_only"
            elif re.fullmatch(r"Map\d+\.json", filename) and path == ["displayName"]:
                role = "display" if is_display_text(value) else "control_only"
            if "parameters" in path:
                parameter_at = len(path) - 1 - path[::-1].index("parameters")
                command: Any = documents[filename]
                for part in path[:parameter_at]:
                    command = command[part]
                if isinstance(command, dict) and "code" in command:
                    code = command["code"]
                    operand = path[parameter_at + 1:]
                    if code in {108, 408}:
                        role = "control_only"
                    elif (code in {401, 405} and operand == [0]) or (
                        code == 102 and len(operand) == 2 and operand[0] == 0
                    ) or (code in {320, 324, 325} and operand == [1]):
                        role = "display" if is_display_text(value) else "control_only"
                    elif code == 402 and operand == [1]:
                        role = "display_mirror"
                    elif code in {355, 655} and operand == [0]:
                        scan(filename, value, path)
                    elif code == 356 and operand == [0]:
                        match = PLUGIN_DISPLAY_RE.match(value)
                        if match:
                            records.append((f"{filename}#{pointer(path)}#{match.span(1)}",
                                            hashlib.sha256(match[1].encode()).hexdigest(),
                                            "display" if is_display_text(match[1]) else "control_only"))
            records.append((f"{filename}#{pointer(path)}", hashlib.sha256(value.encode()).hexdigest(), role))
        elif isinstance(value, (dict, list)):
            children = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in children:
                walk(filename, child, path + [key])

    def scan(filename: str, text: str, path: list[Any]) -> None:
        displays, unresolved = script_displays(text, documents)
        display_spans = {item["span"]: item for item in displays}
        for span in unresolved:
            pending.append({"file": filename, "path": path, "call_span": list(span),
                            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                            "reason_code": "RPG_MAKER_MV_UNRESOLVED_DISPLAY_ARGUMENT"})
        for token in LEXICAL.finditer(text):
            if token[0][0] in {'"', "'", "`"}:
                display = display_spans.get(token.span())
                role = ("display" if is_display_text(display["source"]) else "control_only") if display else "unclassified"
                records.append((f"{filename}#{pointer(path)}#{token.span()}",
                                hashlib.sha256(token[0].encode()).hexdigest(), role))
        for item in displays:
            if item["kind"] == "script_display_projection":
                records.append((f"{filename}#{pointer(path)}#{item['span']}",
                                hashlib.sha256(item["projection"]["expression"].encode()).hexdigest(),
                                "display" if is_display_text(item["source"]) else "control_only"))

    for filename, document in sorted(documents.items()):
        walk(filename, document, [])
    for filename, text in sorted(sources.items()):
        if filename.endswith(".js"):
            scan(filename, text, [])
        elif filename.endswith(".html"):
            records.append((filename + "#document", hashlib.sha256(text.encode()).hexdigest(), "unclassified"))
            for match in re.finditer(r"<title\b[^>]*>(.*?)</title\s*>", text, re.I | re.S):
                records.append((f"{filename}#title#{match.span(1)}", hashlib.sha256(match[1].encode()).hexdigest(),
                                "display" if is_display_text(html.unescape(match[1])) else "control_only"))
    return {"records": records, "pending_display_dependencies": pending,
            "evidence_fingerprint": hashlib.sha256(
                json.dumps([records, pending], separators=(",", ":")).encode()
            ).hexdigest()}


def display_accounting(census: Mapping[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any]:
    # Raw occurrences include storage leaves and embedded script/display operands.
    # Grouped dialogue and mirrored labels need not have one occurrence per target.
    return {"schema": "parr0t.display-scope.v1", "raw_occurrence_count": len(census["records"]),
            "display_candidate_count": len(segments),
            "control_only_count": sum(record[2] == "control_only" for record in census["records"]),
            "unclassified_count": sum(record[2] == "unclassified" for record in census["records"]),
            "evidence_fingerprint": census["evidence_fingerprint"]}
