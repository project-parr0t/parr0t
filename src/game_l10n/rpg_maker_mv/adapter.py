from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..coverage import verify_inventory_pair
from ..errors import GameL10nError
from ..models import Segment
from ..validators import DEFAULT_PROTECTED_PATTERNS, validate_integrity


EXPECTED_ENGINE_VERSION = "1.6.1"
MANIFEST_FORMAT = "rpg-maker-mv-1.6.1-manifest-v1"
ENGINE_NAME = "RPG Maker MV"
STATIC_SCOPE_SCHEMA = "parr0t.static-scope.v1"
STATIC_SCOPE_CAPABILITY = "rpg-maker-mv-fixed-runtime-graph-v1"
PRIMARY_SCOPE_METHOD = {
    "name": "rpg-maker-mv-runtime-graph-bfs",
    "implementation_fingerprint": "rpg-maker-mv-runtime-graph-bfs-v1",
}
INDEPENDENT_SCOPE_METHOD = {
    "name": "rpg-maker-mv-runtime-graph-fixed-point",
    "implementation_fingerprint": "rpg-maker-mv-runtime-graph-fixed-point-v1",
}

PROTECTED_TOKEN_RE = re.compile(
    r"\\(?:[A-Za-z]+(?:\[[^\]\r\n]*\])?|[.!|^><{}$\\])"
    r"|%(?:\d+\$)?[-+#0 ']*\d*(?:\.\d+)?[diouxXeEfFgGcrsa%]"
    r"|%\d+|\{\{[^{}]*\}\}|\{[A-Za-z_][A-Za-z0-9_.:-]*\}|</?[A-Za-z][^>\r\n]*>"
)
SCRIPT_DISPLAY_RE = re.compile(
    r"(?:\$gameMessage\.add|\.setName|\.setNickname|\.setProfile)\s*\(\s*"
    r"((?:\"(?:\\.|[^\"\\])*\")|(?:'(?:\\.|[^'\\])*'))\s*\)"
)
SCRIPT_DISPLAY_ONLY_RE = re.compile(
    r"^\s*(?:(?:\$gameMessage\.add|[A-Za-z_$][\w$\.\[\]]*\.(?:setName|setNickname|setProfile))"
    r"\s*\(\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')\s*\)\s*;?\s*)+$",
    re.S,
)
PLUGIN_DISPLAY_RE = re.compile(r"(?i)^\s*D_TEXT\s+(.*?)(?:\s+\d+)?\s*$")
VERSION_RE = re.compile(r"RPGMAKER_VERSION\s*=\s*['\"]([^'\"]+)")

DATABASE_FIELDS: dict[str, tuple[str, ...]] = {
    "Actors.json": ("name", "nickname", "profile"),
    "Classes.json": ("name",),
    "Skills.json": ("name", "description", "message1", "message2"),
    "Items.json": ("name", "description"),
    "Weapons.json": ("name", "description"),
    "Armors.json": ("name", "description"),
    "Enemies.json": ("name",),
    "States.json": ("name", "message1", "message2", "message3", "message4"),
}

ACCEPTED_RESULT_STATUSES = {
    "translated",
    "reviewed",
    "accepted_unreviewed",
    "approved",
    "revised",
    "success",
}


class RpgMakerMvError(GameL10nError):
    """The MV adapter rejected an unsafe or structurally invalid operation."""


@dataclass(slots=True, frozen=True)
class GameLayout:
    game_root: Path
    data_root: Path
    core_script: Path
    version: str


@dataclass(slots=True, frozen=True)
class StaticDisplayScope:
    reachable_nodes: frozenset[str]
    conservative_nodes: frozenset[str]
    unused_nodes: frozenset[str]
    methods_agree: bool
    dynamic_reference_count: int
    evidence_fingerprint: str
    graph_counts: dict[str, int]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _stable_digest(rows: Iterable[tuple[str, str]]) -> str:
    encoded = json.dumps(list(rows), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def _scope_node(kind: str, identifier: int) -> str:
    return f"{kind}:{identifier}"


def _scene_scope_node(scene: str) -> str | None:
    match = re.match(r"^(map|common|troop):(\d+)(?::|$)", scene)
    return _scope_node(match.group(1), int(match.group(2))) if match else None


def _event_command_lists(document: Any, kind: str) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    if kind == "map" and isinstance(document, dict):
        map_id = int(document["_scope_id"])
        for event in document.get("events", []):
            if not isinstance(event, dict):
                continue
            for page in event.get("pages", []):
                if isinstance(page, dict) and isinstance(page.get("list"), list):
                    yield _scope_node("map", map_id), page["list"]
    elif kind == "common" and isinstance(document, list):
        for index, row in enumerate(document):
            if isinstance(row, dict) and isinstance(row.get("list"), list):
                yield _scope_node("common", index), row["list"]
    elif kind == "troop" and isinstance(document, list):
        for index, row in enumerate(document):
            if not isinstance(row, dict):
                continue
            for page in row.get("pages", []):
                if isinstance(page, dict) and isinstance(page.get("list"), list):
                    yield _scope_node("troop", index), page["list"]


def _literal_script_reference(script: str, function: str) -> tuple[set[int], bool]:
    calls = list(re.finditer(rf"\b{re.escape(function)}\s*\(\s*([^,\)]+)", script))
    values: set[int] = set()
    dynamic = function in script and not calls
    for call in calls:
        expression = call.group(1).strip()
        if re.fullmatch(r"\d+", expression):
            values.add(int(expression))
        else:
            dynamic = True
    return values, dynamic


def _command_scope_references(
    commands: Iterable[Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    references: set[str] = set()
    dynamic: set[str] = set()
    for command in commands:
        if not isinstance(command, Mapping):
            continue
        code = command.get("code")
        parameters = command.get("parameters", [])
        if not isinstance(parameters, list):
            continue
        if code == 201:
            if len(parameters) >= 2 and parameters[0] == 0 and type(parameters[1]) is int:
                references.add(_scope_node("map", parameters[1]))
            else:
                dynamic.add("map")
        elif code == 117:
            if parameters and type(parameters[0]) is int:
                references.add(_scope_node("common", parameters[0]))
            else:
                dynamic.add("common")
        elif code == 301:
            if len(parameters) >= 2 and parameters[0] == 0 and type(parameters[1]) is int:
                references.add(_scope_node("troop", parameters[1]))
            elif not parameters or parameters[0] != 2:
                dynamic.add("troop")
        elif code in {355, 655}:
            script = parameters[0] if parameters and isinstance(parameters[0], str) else ""
            for function, category in (
                ("reserveTransfer", "map"),
                ("reserveCommonEvent", "common"),
                ("BattleManager.setup", "troop"),
            ):
                values, unresolved = _literal_script_reference(script, function)
                references.update(_scope_node(category, value) for value in values)
                if unresolved:
                    dynamic.add(category)
            if script.strip() and SCRIPT_DISPLAY_ONLY_RE.fullmatch(script) is None:
                dynamic.update({"map", "common", "troop"})
        elif code == 356:
            value = parameters[0] if parameters and isinstance(parameters[0], str) else ""
            if not PLUGIN_DISPLAY_RE.match(value):
                dynamic.update({"map", "common", "troop"})
    return references, dynamic


def _scope_graph(
    documents: Mapping[str, Any], external_sources: Mapping[str, str] | None = None
) -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    maps: dict[int, dict[str, Any]] = {}
    for filename, document in documents.items():
        if re.fullmatch(r"Map\d+\.json", filename) and isinstance(document, dict):
            map_id = int(re.search(r"\d+", filename).group())
            maps[map_id] = {**document, "_scope_id": map_id}
    commons = documents.get("CommonEvents.json", [])
    troops = documents.get("Troops.json", [])
    nodes = {
        *(_scope_node("map", value) for value in maps),
        *(_scope_node("common", index) for index, row in enumerate(commons) if isinstance(row, dict)),
        *(_scope_node("troop", index) for index, row in enumerate(troops) if isinstance(row, dict)),
    }
    edges: dict[str, set[str]] = {node: set() for node in nodes}
    dynamic: dict[str, set[str]] = {node: set() for node in nodes}

    for map_id, document in maps.items():
        owner = _scope_node("map", map_id)
        for encounter in document.get("encounterList", []):
            if isinstance(encounter, dict) and type(encounter.get("troopId")) is int:
                edges[owner].add(_scope_node("troop", encounter["troopId"]))
        for _, commands in _event_command_lists(document, "map"):
            references, unresolved = _command_scope_references(commands)
            edges[owner].update(references)
            dynamic[owner].update(unresolved)
    for owner, commands in _event_command_lists(commons, "common"):
        references, unresolved = _command_scope_references(commands)
        edges[owner].update(references)
        dynamic[owner].update(unresolved)
    for owner, commands in _event_command_lists(troops, "troop"):
        references, unresolved = _command_scope_references(commands)
        edges[owner].update(references)
        dynamic[owner].update(unresolved)

    roots: set[str] = set()
    system = documents.get("System.json", {})
    start_map = system.get("startMapId") if isinstance(system, dict) else None
    if type(start_map) is int and start_map > 0:
        roots.add(_scope_node("map", start_map))
    else:
        roots.update(node for node in nodes if node.startswith("map:"))
        dynamic.setdefault("root", set()).add("map")
    if isinstance(commons, list):
        for index, row in enumerate(commons):
            if isinstance(row, dict) and row.get("trigger") in {1, 2}:
                roots.add(_scope_node("common", index))
    items = documents.get("Items.json", [])
    if isinstance(items, list):
        for row in items:
            if not isinstance(row, dict):
                continue
            for effect in row.get("effects", []):
                if isinstance(effect, dict) and effect.get("code") == 44 and type(effect.get("dataId")) is int:
                    roots.add(_scope_node("common", effect["dataId"]))
    for filename, source in sorted((external_sources or {}).items()):
        if not filename.endswith(".js"):
            continue
        if filename.startswith("js/plugins/"):
            dynamic.setdefault("root", set()).update({"map", "common", "troop"})
        for function, category in (
            ("reserveTransfer", "map"),
            ("reserveCommonEvent", "common"),
            ("BattleManager.setup", "troop"),
        ):
            values, unresolved = _literal_script_reference(source, function)
            roots.update(_scope_node(category, value) for value in values)
            if unresolved:
                dynamic.setdefault("root", set()).add(category)
    return roots, edges, dynamic


def _bfs_scope(
    roots: set[str], edges: Mapping[str, set[str]], dynamic: Mapping[str, set[str]]
) -> tuple[set[str], set[str], int]:
    nodes = set(edges)
    reached: set[str] = set()
    root_categories = dynamic.get("root", set())
    conservative = {
        node for node in nodes if any(node.startswith(f"{category}:") for category in root_categories)
    }
    queue = list(sorted((roots & nodes) | conservative))
    dynamic_count = len(dynamic.get("root", set()))
    while queue:
        node = queue.pop(0)
        if node in reached:
            continue
        reached.add(node)
        categories = dynamic.get(node, set())
        dynamic_count += len(categories)
        for category in categories:
            additions = {item for item in nodes if item.startswith(f"{category}:")} - reached
            conservative.update(additions)
            queue.extend(sorted(additions))
        queue.extend(sorted((edges.get(node, set()) & nodes) - reached))
    return reached, conservative, dynamic_count


def _fixed_point_scope(
    roots: set[str], edges: Mapping[str, set[str]], dynamic: Mapping[str, set[str]]
) -> tuple[set[str], set[str], int]:
    nodes = set(edges)
    root_categories = dynamic.get("root", set())
    conservative = {
        node for node in nodes if any(node.startswith(f"{category}:") for category in root_categories)
    }
    reached = (roots & nodes) | conservative
    seen_dynamic: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for node in sorted(tuple(reached)):
            additions = (edges.get(node, set()) & nodes) - reached
            if additions:
                reached.update(additions)
                changed = True
            for category in dynamic.get(node, set()):
                seen_dynamic.add((node, category))
                additions = {item for item in nodes if item.startswith(f"{category}:")} - reached
                if additions:
                    conservative.update(additions)
                    reached.update(additions)
                    changed = True
    return set(reached), conservative, len(dynamic.get("root", set())) + len(seen_dynamic)


def build_static_display_scope(
    documents: Mapping[str, Any], external_sources: Mapping[str, str] | None = None
) -> StaticDisplayScope:
    roots, edges, dynamic = _scope_graph(documents, external_sources)
    primary = _bfs_scope(roots, edges, dynamic)
    independent = _fixed_point_scope(roots, edges, dynamic)
    agree = primary[:2] == independent[:2]
    nodes = set(edges)
    if agree:
        reached, conservative = primary[:2]
    else:
        reached, conservative = nodes, nodes - (roots & nodes)
    unused = nodes - reached
    evidence = {
        "roots": sorted(roots),
        "edges": {key: sorted(value) for key, value in sorted(edges.items())},
        "dynamic": {key: sorted(value) for key, value in sorted(dynamic.items()) if value},
        "primary_reached": sorted(primary[0]),
        "independent_reached": sorted(independent[0]),
        "methods_agree": agree,
    }
    graph_counts = Counter(node.split(":", 1)[0] for node in nodes)
    return StaticDisplayScope(
        reachable_nodes=frozenset(reached),
        conservative_nodes=frozenset(conservative),
        unused_nodes=frozenset(unused),
        methods_agree=agree,
        dynamic_reference_count=max(primary[2], independent[2]),
        evidence_fingerprint=_sha256_bytes(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
        graph_counts=dict(graph_counts),
    )


def _apply_static_display_scope(
    extractor: "_Extractor", scope: StaticDisplayScope
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    candidate_segments = list(extractor.segments)
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    excluded_counts: Counter[str] = Counter()
    conservative_count = 0

    for row in candidate_segments:
        node = _scene_scope_node(str(row.get("scene", "")))
        if node is not None and node in scope.unused_nodes:
            category = node.split(":", 1)[0]
            excluded_counts[category] += 1
            excluded.append({"id": str(row["id"]), "node": node})
            continue
        status = (
            "conservative_retained"
            if node is None or node in scope.conservative_nodes
            else "statically_reachable"
        )
        if status == "conservative_retained":
            conservative_count += 1
        metadata = row.setdefault("metadata", {})
        metadata["display_scope"] = {
            "schema": STATIC_SCOPE_SCHEMA,
            "claim": "static_reachability_only",
            "status": status,
            "node": node or "engine-global",
            "evidence_fingerprint": scope.evidence_fingerprint,
        }
        included.append(row)

    active_ids = {str(row["id"]) for row in included}
    extractor.segments = included
    extractor.mappings = [row for row in extractor.mappings if str(row["id"]) in active_ids]
    extractor.contexts = [
        row
        for row in extractor.contexts
        if (_scene_scope_node(str(row.get("scene", ""))) not in scope.unused_nodes)
    ]
    for index, row in enumerate(extractor.segments):
        row["previous"] = ""
        row["next"] = ""
        row["metadata"]["order"] = index
        if index and extractor.segments[index - 1]["scene"] == row["scene"]:
            row["previous"] = extractor.segments[index - 1]["source"]
        if index + 1 < len(extractor.segments) and extractor.segments[index + 1]["scene"] == row["scene"]:
            row["next"] = extractor.segments[index + 1]["source"]

    exclusions = [
        {
            "reason_code": f"RPG_MAKER_MV_STATIC_UNREACHABLE_{category.upper()}",
            "scope": "static_reachability",
            "target_count": count,
            "affects_user_visible_text": False,
            "evidence_fingerprint": scope.evidence_fingerprint,
        }
        for category, count in sorted(excluded_counts.items())
    ]
    tracking = {
        "schema": STATIC_SCOPE_SCHEMA,
        "claim": "static_reachability_only",
        "engine_capability": STATIC_SCOPE_CAPABILITY,
        "candidate_count": len(candidate_segments),
        "included_count": len(included),
        "proven_unused_count": len(excluded),
        "conservative_retained_count": conservative_count,
        "unresolved_dynamic_reference_count": scope.dynamic_reference_count,
        "methods": [dict(PRIMARY_SCOPE_METHOD), dict(INDEPENDENT_SCOPE_METHOD)],
        "methods_agree": scope.methods_agree,
        "evidence_fingerprint": scope.evidence_fingerprint,
    }
    ledger = {
        **tracking,
        "runtime_complete": False,
        "graph_node_counts": scope.graph_counts,
        "reachable_nodes": sorted(scope.reachable_nodes),
        "conservative_nodes": sorted(scope.conservative_nodes),
        "proven_unused_nodes": sorted(scope.unused_nodes),
        "excluded_targets": excluded,
    }
    extractor.counts["static_scope_candidates"] = len(candidate_segments)
    extractor.counts["static_scope_included"] = len(included)
    extractor.counts["static_scope_proven_unused"] = len(excluded)
    extractor.counts["static_scope_conservative_retained"] = conservative_count
    return tracking, exclusions, ledger


def _source_inventory_document(
    segments: list[dict[str, Any]],
    method: Mapping[str, str],
    tracking: Mapping[str, Any],
    exclusions: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "parr0t.source-inventory.v1",
        "method": method["name"],
        "implementation_fingerprint": method["implementation_fingerprint"],
        "coverage": {
            "declared_complete": True,
            "exclusions": exclusions,
            "static_scope": dict(tracking),
        },
        "segments": segments,
    }


def _source_snapshot(game_root: Path, files: Iterable[Mapping[str, Any]]) -> str:
    rows: list[tuple[str, str]] = []
    for item in files:
        relative = _safe_relative_path(str(item["patch_path"]), label="patch_path")
        path = game_root / relative
        if not path.is_file():
            raise RpgMakerMvError(f"SOURCE_FILE_MISSING:{relative.as_posix()}")
        rows.append((relative.as_posix(), _sha256_file(path)))
    return _stable_digest(sorted(rows))


def _ensure_separate_output(output: Path, source: Path) -> None:
    output = output.resolve()
    source = source.resolve()
    if output == source or output.is_relative_to(source):
        raise RpgMakerMvError("OUTPUT_INSIDE_READ_ONLY_SOURCE")


def _safe_relative_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise RpgMakerMvError(f"UNSAFE_{label.upper()}")
    return path


def _json_pointer(path: Iterable[Any]) -> str:
    return "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _get_path(document: Any, path: Iterable[Any]) -> Any:
    current = document
    try:
        for key in path:
            current = current[int(key)] if isinstance(current, list) else current[str(key)]
        return current
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RpgMakerMvError("MAPPING_PATH_NOT_FOUND") from exc


def _set_path(document: Any, path: list[Any], value: Any) -> None:
    if not path:
        raise RpgMakerMvError("EMPTY_ASSIGNMENT_PATH")
    current = document
    try:
        for key in path[:-1]:
            current = current[int(key)] if isinstance(current, list) else current[str(key)]
        key = path[-1]
        if isinstance(current, list):
            current[int(key)] = value
        else:
            current[str(key)] = value
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise RpgMakerMvError("MAPPING_PATH_NOT_FOUND") from exc


def decode_js_literal(literal: str) -> str:
    """Decode one ordinary quoted JavaScript string literal without evaluating code."""
    if len(literal) < 2 or literal[0] not in {'"', "'"} or literal[-1] != literal[0]:
        raise RpgMakerMvError("INVALID_SCRIPT_LITERAL")
    if literal[0] == '"':
        try:
            value = json.loads(literal)
        except json.JSONDecodeError as exc:
            raise RpgMakerMvError("UNSUPPORTED_SCRIPT_LITERAL") from exc
        if not isinstance(value, str):
            raise RpgMakerMvError("INVALID_SCRIPT_LITERAL")
        return value

    result: list[str] = []
    index = 1
    end = len(literal) - 1
    simple = {
        "'": "'",
        '"': '"',
        "\\": "\\",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "0": "\0",
    }
    while index < end:
        character = literal[index]
        if character != "\\":
            result.append(character)
            index += 1
            continue
        if index + 1 >= end:
            raise RpgMakerMvError("UNSUPPORTED_SCRIPT_LITERAL")
        escape = literal[index + 1]
        if escape in simple:
            result.append(simple[escape])
            index += 2
            continue
        if escape in {"x", "u"}:
            width = 2 if escape == "x" else 4
            digits = literal[index + 2 : index + 2 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                raise RpgMakerMvError("UNSUPPORTED_SCRIPT_LITERAL")
            result.append(chr(int(digits, 16)))
            index += 2 + width
            continue
        raise RpgMakerMvError("UNSUPPORTED_SCRIPT_LITERAL")
    return "".join(result)


def encode_js_literal(value: str, quote: str) -> str:
    """Encode text while preserving the original JavaScript quote style."""
    if quote == '"':
        return (
            json.dumps(value, ensure_ascii=False)
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
    if quote != "'":
        raise RpgMakerMvError("INVALID_SCRIPT_QUOTE")
    escapes = {
        "\\": "\\\\",
        "'": "\\'",
        "\r": "\\r",
        "\n": "\\n",
        "\t": "\\t",
        "\b": "\\b",
        "\f": "\\f",
        "\v": "\\v",
        "\u2028": "\\u2028",
        "\u2029": "\\u2029",
    }
    encoded: list[str] = []
    for character in value:
        if character in escapes:
            encoded.append(escapes[character])
        elif ord(character) < 0x20:
            encoded.append(f"\\x{ord(character):02x}")
        else:
            encoded.append(character)
    return "'" + "".join(encoded) + "'"


def discover_game(game_root: Path, *, expected_version: str = EXPECTED_ENGINE_VERSION) -> GameLayout:
    game_root = game_root.resolve()
    if not game_root.is_dir():
        raise RpgMakerMvError("GAME_ROOT_MISSING")
    # NW.js resolves main relative to the launch package, not recursively.
    # Prefer that explicit entry to incidental nested copies of a project.
    package = game_root / "package.json"
    entry_data: Path | None = None
    if package.is_file():
        try:
            config = json.loads(package.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpgMakerMvError("INVALID_RUNTIME_PACKAGE") from exc
        main = config.get("main") if isinstance(config, dict) else None
        if isinstance(main, str) and main:
            entry = (game_root / _safe_relative_path(main, label="runtime_main")).resolve()
            if not entry.is_relative_to(game_root):
                raise RpgMakerMvError("UNSAFE_RUNTIME_MAIN")
            candidate = entry.parent / "data"
            if (entry.is_file() and (candidate / "System.json").is_file()
                    and (candidate / "MapInfos.json").is_file()
                    and (entry.parent / "js" / "rpg_core.js").is_file()):
                entry_data = candidate
            else:
                raise RpgMakerMvError("RUNTIME_ENTRY_LAYOUT_UNSUPPORTED")
    candidates = [entry_data] if entry_data else sorted(
        path.parent
        for path in game_root.rglob("System.json")
        if (path.parent / "MapInfos.json").is_file()
        and (path.parent.parent / "js" / "rpg_core.js").is_file()
    )
    if len(candidates) != 1:
        raise RpgMakerMvError("ENGINE_DATA_ROOT_AMBIGUOUS")
    data_root = candidates[0]
    core_script = data_root.parent / "js" / "rpg_core.js"
    match = VERSION_RE.search(core_script.read_text(encoding="utf-8-sig"))
    if match is None:
        raise RpgMakerMvError("ENGINE_VERSION_MISSING")
    version = match.group(1)
    if expected_version and version != expected_version:
        raise RpgMakerMvError(f"ENGINE_VERSION_MISMATCH:{version}")
    return GameLayout(game_root, data_root, core_script, version)


class _Extractor:
    def __init__(self, documents: Mapping[str, Any]) -> None:
        self.documents = documents
        self.segments: list[dict[str, Any]] = []
        self.mappings: list[dict[str, Any]] = []
        self.contexts: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()
        self.issues: Counter[str] = Counter()
        self.seen: set[tuple[str, str, tuple[int, int] | None]] = set()

    def add(
        self,
        filename: str,
        paths: list[list[Any]],
        value: Any,
        kind: str,
        scene: str,
        *,
        speaker: str = "",
        context: Mapping[str, Any] | None = None,
        span: tuple[int, int] | None = None,
        mirrors: list[list[Any]] | None = None,
        quote: str | None = None,
    ) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        if not PROTECTED_TOKEN_RE.sub("", value).strip():
            self.counts["control_only_preserved"] += 1
            return
        if not paths:
            raise RpgMakerMvError("EMPTY_EXTRACTION_PATH")
        key = (filename, _json_pointer(paths[0]), span)
        if key in self.seen:
            raise RpgMakerMvError("DUPLICATE_EXTRACTION_PATH")
        self.seen.add(key)
        identity = f"{filename}#{key[1]}#{span or ''}"
        identifier = "mv-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        metadata: dict[str, Any] = {
            "kind": kind,
            "file": filename,
            "json_pointer": key[1],
            "order": len(self.segments),
            "protected_tokens": PROTECTED_TOKEN_RE.findall(value),
            "preserve_control_code_order": True,
            "context": dict(context or {}),
        }
        if kind == "battle_message" or (kind == "database" and str((context or {}).get('field', '')).startswith('message')):
            metadata.update(ui_slot_role="template_fragment", ui_render_pattern="{name}{value}")
        if span is not None:
            metadata['source_span'] = list(span)
        if kind in {"dialogue", "scrolling_text"}:
            metadata["required_line_count"] = len(paths)
        nameboxes = re.findall(r"\\[nN]<([^>\r\n]*)>", value)
        if nameboxes:
            metadata["namebox_payloads"] = nameboxes
            metadata["namebox_rule"] = (
                "Translate literal speaker-name payloads consistently while preserving control syntax."
            )
        self.segments.append(
            {
                "id": identifier,
                "source": value,
                "speaker": speaker,
                "scene": scene,
                "previous": "",
                "next": "",
                "metadata": metadata,
            }
        )
        mapping: dict[str, Any] = {
            "id": identifier,
            "file": filename,
            "paths": paths,
            "source": value,
            "kind": kind,
            "mirrors": mirrors or [],
        }
        if span is not None:
            mapping["span"] = list(span)
        if quote is not None:
            mapping["quote"] = quote
        self.mappings.append(mapping)
        self.counts[kind] += 1

    def tree(self, filename: str, value: Any, path: list[Any], scene: str) -> None:
        if isinstance(value, str):
            self.add(filename, [path], value, "system_ui", scene)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self.tree(filename, child, path + [index], scene)
        elif isinstance(value, dict):
            for key, child in value.items():
                self.tree(filename, child, path + [key], scene)

    def events(
        self,
        filename: str,
        commands: list[dict[str, Any]],
        base: list[Any],
        scene: str,
        details: Mapping[str, Any],
    ) -> None:
        self.contexts.append(
            {"scene": scene, "file": filename, "path": base, "details": dict(details), "commands": commands}
        )
        comments: list[str] = []
        message: dict[str, Any] = {}
        branches: dict[int, str] = {}
        index = 0
        while index < len(commands):
            command = commands[index]
            if not isinstance(command, dict):
                raise RpgMakerMvError("INVALID_EVENT_COMMAND")
            code = command.get("code", 0)
            indent = int(command.get("indent", 0))
            parameters = command.get("parameters", [])
            if not isinstance(parameters, list):
                raise RpgMakerMvError("INVALID_EVENT_PARAMETERS")
            self.counts["event_commands"] += 1
            if code in {108, 408} and parameters and isinstance(parameters[0], str):
                comments = (comments + [parameters[0]])[-8:]
            elif code == 101:
                message = {
                    "face_resource": parameters[0] if parameters else "",
                    "face_index": parameters[1] if len(parameters) > 1 else 0,
                    "background": parameters[2] if len(parameters) > 2 else 0,
                    "position": parameters[3] if len(parameters) > 3 else 2,
                }
            elif code == 402 and len(parameters) >= 2:
                branches[indent] = str(parameters[1])
            elif code in {403, 404, 412, 413}:
                branches.pop(indent, None)
            context = {
                **details,
                "command_index": index,
                "indent": indent,
                "comments": list(comments),
                "message": dict(message),
                "branches": dict(branches),
            }

            if code in {401, 405}:
                cursor = index
                lines: list[str] = []
                paths: list[list[Any]] = []
                while cursor < len(commands) and commands[cursor].get("code") == code:
                    values = commands[cursor].get("parameters", [])
                    if not values or not isinstance(values[0], str):
                        raise RpgMakerMvError("INVALID_TEXT_COMMAND")
                    lines.append(values[0])
                    paths.append(base + [cursor, "parameters", 0])
                    cursor += 1
                text = "\n".join(lines)
                name = re.search(r"\\[nN](?:<([^>]+)>|\[([^\]]+)\])", text)
                speaker = (name.group(1) or name.group(2) or "") if name else ""
                self.add(
                    filename,
                    paths,
                    text,
                    "dialogue" if code == 401 else "scrolling_text",
                    scene,
                    speaker=speaker,
                    context=context,
                )
                self.counts["dialogue_lines"] += len(lines)
                self.counts["event_commands"] += cursor - index - 1
                index = cursor
                continue

            if code == 102 and parameters and isinstance(parameters[0], list):
                for choice_index, choice_text in enumerate(parameters[0]):
                    if not isinstance(choice_text, str):
                        continue
                    mirrors: list[list[Any]] = []
                    for cursor in range(index + 1, len(commands)):
                        other = commands[cursor]
                        other_parameters = other.get("parameters", [])
                        other_indent = int(other.get("indent", 0))
                        if other_indent < indent or (other_indent == indent and other.get("code") == 404):
                            break
                        if (
                            other_indent == indent
                            and other.get("code") == 402
                            and len(other_parameters) >= 2
                            and other_parameters[0] == choice_index
                        ):
                            if other_parameters[1] != choice_text:
                                raise RpgMakerMvError("CHOICE_BRANCH_SOURCE_MISMATCH")
                            mirrors.append(base + [cursor, "parameters", 1])
                    self.add(
                        filename,
                        [base + [index, "parameters", 0, choice_index]],
                        choice_text,
                        "choice",
                        scene,
                        context=context,
                        mirrors=mirrors,
                    )
            elif code in {320, 324, 325} and len(parameters) >= 2:
                self.add(
                    filename,
                    [base + [index, "parameters", 1]],
                    parameters[1],
                    "actor_text",
                    scene,
                    context={**context, "actor_id": parameters[0]},
                )
            elif code == 356 and parameters and isinstance(parameters[0], str):
                match = PLUGIN_DISPLAY_RE.match(parameters[0])
                if match and match.group(1).strip():
                    self.add(
                        filename,
                        [base + [index, "parameters", 0]],
                        match.group(1),
                        "plugin_display_text",
                        scene,
                        context=context,
                        span=match.span(1),
                    )
                self.counts["plugin_commands_preserved"] += 1
            elif code in {355, 655}:
                self.counts["script_commands_preserved"] += 1
                script = parameters[0] if parameters and isinstance(parameters[0], str) else ""
                for match in SCRIPT_DISPLAY_RE.finditer(script):
                    literal = match.group(1)
                    try:
                        decoded = decode_js_literal(literal)
                    except RpgMakerMvError:
                        self.issues["SCRIPT_LITERAL_DECODING_UNSUPPORTED"] += 1
                        continue
                    self.add(
                        filename,
                        [base + [index, "parameters", 0]],
                        decoded,
                        "script_display_text",
                        scene,
                        context=context,
                        span=match.span(1),
                        quote=literal[0],
                    )
            index += 1

    def run(self) -> None:
        for filename, fields in DATABASE_FIELDS.items():
            rows = self.documents.get(filename, [])
            if not isinstance(rows, list):
                raise RpgMakerMvError(f"INVALID_DATABASE_DOCUMENT:{filename}")
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    continue
                for field in fields:
                    self.add(
                        filename,
                        [[index, field]],
                        row.get(field),
                        "database",
                        filename,
                        context={
                            "record_id": row.get("id", index),
                            "record_name": row.get("name", ""),
                            "note": row.get("note", ""),
                            "field": field,
                        },
                    )

        system = self.documents.get("System.json", {})
        if not isinstance(system, dict):
            raise RpgMakerMvError("INVALID_SYSTEM_DOCUMENT")
        for key in (
            "gameTitle",
            "currencyUnit",
            "armorTypes",
            "elements",
            "equipTypes",
            "skillTypes",
            "weaponTypes",
            "terms",
        ):
            if key in system:
                self.tree("System.json", system[key], [key], "system")

        for index, row in enumerate(self.documents.get("CommonEvents.json", [])):
            if isinstance(row, dict) and isinstance(row.get("list"), list):
                self.events(
                    "CommonEvents.json",
                    row["list"],
                    [index, "list"],
                    f"common:{index}",
                    {
                        "name": row.get("name", ""),
                        "trigger": row.get("trigger"),
                        "switch_id": row.get("switchId"),
                    },
                )

        for index, row in enumerate(self.documents.get("Troops.json", [])):
            if not isinstance(row, dict):
                continue
            for page_index, page in enumerate(row.get("pages", [])):
                if isinstance(page, dict) and isinstance(page.get("list"), list):
                    self.events(
                        "Troops.json",
                        page["list"],
                        [index, "pages", page_index, "list"],
                        f"troop:{index}:page:{page_index}",
                        {
                            "name": row.get("name", ""),
                            "conditions": page.get("conditions", {}),
                        },
                    )

        infos = self.documents.get("MapInfos.json", [])
        if not isinstance(infos, list):
            raise RpgMakerMvError("INVALID_MAP_INFO_DOCUMENT")
        map_files = sorted(
            (name for name in self.documents if re.fullmatch(r"Map\d+\.json", name)),
            key=lambda name: int(re.search(r"\d+", name).group()),
        )
        for filename in map_files:
            data = self.documents[filename]
            if not isinstance(data, dict):
                raise RpgMakerMvError(f"INVALID_MAP_DOCUMENT:{filename}")
            map_id = int(re.search(r"\d+", filename).group())
            self.add(filename, [["displayName"]], data.get("displayName"), "map_name", f"map:{map_id}")
            if map_id < len(infos) and isinstance(infos[map_id], dict):
                self.add(
                    "MapInfos.json",
                    [[map_id, "name"]],
                    infos[map_id].get("name"),
                    "map_name",
                    f"map:{map_id}",
                )
            for event_index, event in enumerate(data.get("events", [])):
                if not isinstance(event, dict):
                    continue
                for page_index, page in enumerate(event.get("pages", [])):
                    if not isinstance(page, dict) or not isinstance(page.get("list"), list):
                        continue
                    self.events(
                        filename,
                        page["list"],
                        ["events", event_index, "pages", page_index, "list"],
                        f"map:{map_id}:event:{event_index}:page:{page_index}",
                        {
                            "map_name": (
                                infos[map_id].get("name", "")
                                if map_id < len(infos) and isinstance(infos[map_id], dict)
                                else ""
                            ),
                            "event_name": event.get("name", ""),
                            "conditions": page.get("conditions", {}),
                            "trigger": page.get("trigger"),
                            "note": event.get("note", ""),
                        },
                    )

        for index, row in enumerate(self.segments):
            if index and self.segments[index - 1]["scene"] == row["scene"]:
                row["previous"] = self.segments[index - 1]["source"]
            if index + 1 < len(self.segments) and self.segments[index + 1]["scene"] == row["scene"]:
                row["next"] = self.segments[index + 1]["source"]


def extract_external_text(extractor: _Extractor, sources: Mapping[str, str]) -> None:
    """Select statically bound first-argument literals without evaluating JavaScript.

    Indirect values and concatenations require a separate dataflow audit. Callers
    supply only scripts reachable from the selected runtime entry point.
    """
    lexical = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`(?:\\.|[^`\\])*`', re.S)
    display = re.compile(r'(?:\.drawText(?:Ex)?|\.addCommand|\.setText|\$gameMessage\.add|\.setName|\.setNickname|\.setProfile)\s*\(\s*$')
    for filename, text in sorted(sources.items()):
        candidates = []
        if filename.endswith('.js'):
            masked = list(text)
            for match in lexical.finditer(text):
                literal = match[0]
                if literal.startswith(('//', '/*')):
                    masked[match.start():match.end()] = ' ' * len(literal)
                    continue
                if literal[0] not in {'"', "'"}:
                    continue
                if display.search(''.join(masked[:match.start()])) and re.match(r'\s*[,)]', text[match.end():]):
                    candidates.append((decode_js_literal(literal), match.span(), 'script_display_text', literal[0], []))
                masked[match.start():match.end()] = ' ' * len(literal)
        elif filename.endswith('.html'):
            for match in re.finditer(r'<title\b[^>]*>(.*?)</title\s*>', text, re.I | re.S):
                candidates.append((html.unescape(match[1]), match.span(1), 'html_title', None, ['title']))
        for value, span, kind, quote, identity_path in candidates:
            before = len(extractor.segments)
            extractor.add(filename, [['text']], value, kind, filename, span=span, quote=quote)
            if len(extractor.segments) == before:
                continue
            identity_span = span if kind == 'script_display_text' else None
            identifier = 'mv-' + hashlib.sha256(f'{filename}#{_json_pointer(identity_path)}#{identity_span or ""}'.encode()).hexdigest()[:24]
            extractor.segments[-1]['id'] = extractor.mappings[-1]['id'] = identifier
            extractor.segments[-1]['metadata'].update(json_pointer=_json_pointer(identity_path), source_span=list(span))


def extract_bound_external_text(
    extractor: _Extractor,
    sources: Mapping[str, str],
    bindings: Iterable[Mapping[str, Any]],
) -> None:
    """Extract independently audited indirect display literals from retained sources.

    Bindings are caller-owned evidence, never executable expressions. Each exact
    quoted source span is sealed to its source-file digest. No title rules live
    in the adapter, and existing apply/verify paths enforce immutable surroundings.
    """
    for binding in bindings:
        filename = str(binding["file"])
        _safe_relative_path(filename, label="bound_external_file")
        text = sources[filename]
        if _sha256_bytes(text.encode("utf-8")) != binding["text_sha256"]:
            raise RpgMakerMvError("BOUND_EXTERNAL_SOURCE_CHANGED")
        span = tuple(binding["span"])
        if len(span) != 2 or any(type(v) is not int for v in span) or not 0 <= span[0] < span[1] <= len(text):
            raise RpgMakerMvError("INVALID_SPAN_MAPPING")
        literal = text[span[0]:span[1]]
        value = decode_js_literal(literal)
        if not value.strip():
            raise RpgMakerMvError("EMPTY_BOUND_DISPLAY_LITERAL")
        before = len(extractor.segments)
        extractor.add(filename, [["text"]], value, "script_display_text", filename,
                      span=span, quote=literal[0])
        if len(extractor.segments) == before:
            continue
        identifier = "mv-" + hashlib.sha256(f"{filename}#/#{span}".encode()).hexdigest()[:24]
        extractor.segments[-1]["id"] = extractor.mappings[-1]["id"] = identifier
        extractor.segments[-1]["metadata"].update(json_pointer="/", source_span=list(span))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract_game(
    game_root: Path,
    output_root: Path,
    *,
    expected_version: str = EXPECTED_ENGINE_VERSION,
) -> dict[str, Any]:
    """Extract ordered targets and immutable baselines into a new external workspace directory."""
    layout = discover_game(game_root, expected_version=expected_version)
    output_root = output_root.resolve()
    _ensure_separate_output(output_root, layout.game_root)
    if output_root.exists():
        raise RpgMakerMvError("OUTPUT_ALREADY_EXISTS")

    paths = sorted(layout.data_root.glob("*.json"), key=lambda path: path.name)
    if not paths:
        raise RpgMakerMvError("NO_JSON_DATA")
    documents: dict[str, Any] = {}
    for path in paths:
        try:
            documents[path.name] = json.loads(path.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpgMakerMvError(f"INVALID_SOURCE_JSON:{path.name}") from exc

    extractor = _Extractor(documents)
    extractor.run()
    runtime_root = layout.data_root.parent
    external_paths = []
    index = runtime_root / 'index.html'
    if index.is_file():
        external_paths.append(index)
    registry = runtime_root / 'js' / 'plugins.js'
    if registry.is_file():
        match = re.search(r'var\s+\$plugins\s*=\s*(\[.*\])\s*;', registry.read_text(encoding='utf-8-sig'), re.S)
        if not match:
            raise RpgMakerMvError('INVALID_PLUGIN_REGISTRY')
        for plugin in json.loads(match[1]):
            if plugin.get('status'):
                name = _safe_relative_path(str(plugin['name']), label='plugin')
                path = runtime_root / 'js' / 'plugins' / Path(str(name) + '.js')
                if not path.is_file():
                    raise RpgMakerMvError('ACTIVE_PLUGIN_MISSING')
                external_paths.append(path)
    external_sources = {p.relative_to(runtime_root).as_posix(): p.read_text(encoding='utf-8-sig') for p in external_paths}
    extract_external_text(extractor, external_sources)
    scope = build_static_display_scope(documents, external_sources)
    static_tracking, static_exclusions, static_ledger = _apply_static_display_scope(
        extractor, scope
    )
    if not extractor.segments:
        raise RpgMakerMvError("NO_TRANSLATABLE_SEGMENTS")

    output_root.mkdir(parents=True)
    relative_data_root = layout.data_root.relative_to(layout.game_root)
    files: list[dict[str, str]] = []
    for path in paths:
        baseline = output_root / "baseline" / path.name
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(path.read_bytes())
        source_hash = _sha256_file(path)
        if _sha256_file(baseline) != source_hash:
            raise RpgMakerMvError("BASELINE_COPY_MISMATCH")
        files.append(
            {
                "file": path.name,
                "baseline": f"baseline/{path.name}",
                "patch_path": (relative_data_root / path.name).as_posix(),
                "sha256": source_hash,
            }
        )

    segment_path = output_root / "segments.jsonl"
    for path in external_paths:
        filename = path.relative_to(runtime_root).as_posix()
        if not any(entry['file'] == filename for entry in extractor.mappings):
            continue
        baseline = output_root / 'baseline' / filename
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(path.read_bytes())
        files.append({'file': filename, 'baseline': 'baseline/' + filename,
                      'patch_path': path.relative_to(layout.game_root).as_posix(),
                      'sha256': _sha256_file(path), 'storage': 'text'})
    segment_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in extractor.segments),
        encoding="utf-8",
    )
    _write_json(
        output_root / "context-script.json",
        {
            "engine": ENGINE_NAME,
            "version": layout.version,
            "ordered_scenes": extractor.contexts,
        },
    )
    _write_json(output_root / "static-scope.json", static_ledger)
    primary_inventory = output_root / "primary-inventory.json"
    independent_inventory = output_root / "independent-inventory.json"
    _write_json(
        primary_inventory,
        _source_inventory_document(
            extractor.segments,
            PRIMARY_SCOPE_METHOD,
            static_tracking,
            static_exclusions,
        ),
    )
    _write_json(
        independent_inventory,
        _source_inventory_document(
            extractor.segments,
            INDEPENDENT_SCOPE_METHOD,
            static_tracking,
            static_exclusions,
        ),
    )
    inventory_attestation = verify_inventory_pair(primary_inventory, independent_inventory)
    if inventory_attestation["status"] != "static_verified":
        raise RpgMakerMvError("STATIC_SCOPE_INVENTORY_MISMATCH")
    _write_json(output_root / "inventory-attestation.json", inventory_attestation)
    manifest = {
        "format": MANIFEST_FORMAT,
        "engine": ENGINE_NAME,
        "version": layout.version,
        "data_root_relative": relative_data_root.as_posix(),
        "files": files,
        "segments": extractor.mappings,
        "segment_count": len(extractor.segments),
        "context_script": "context-script.json",
        "static_scope": "static-scope.json",
        "primary_inventory": "primary-inventory.json",
        "independent_inventory": "independent-inventory.json",
        "inventory_attestation": "inventory-attestation.json",
    }
    _write_json(output_root / "apply-manifest.json", manifest)
    source_before = _source_snapshot(layout.game_root, files)
    source_after = _source_snapshot(layout.game_root, files)
    if source_before != source_after:
        raise RpgMakerMvError("SOURCE_CHANGED_DURING_EXTRACTION")
    report = {
        "status": "completed",
        "engine": ENGINE_NAME,
        "version": layout.version,
        "json_files": len(files),
        "segments": len(extractor.segments),
        "scenes": len(extractor.contexts),
        "counts": dict(extractor.counts),
        "static_scope": {
            **static_tracking,
            "runtime_complete": False,
        },
        "inventory_status": inventory_attestation["status"],
        "issue_codes": dict(extractor.issues),
        "source_unchanged": True,
    }
    _write_json(output_root / "extraction-report.json", report)
    return report


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RpgMakerMvError("INVALID_MANIFEST") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise RpgMakerMvError("MANIFEST_FORMAT_MISMATCH")
    if manifest.get("version") != EXPECTED_ENGINE_VERSION:
        raise RpgMakerMvError("MANIFEST_VERSION_MISMATCH")
    files = manifest.get("files")
    segments = manifest.get("segments")
    if not isinstance(files, list) or not isinstance(segments, list):
        raise RpgMakerMvError("INVALID_MANIFEST_STRUCTURE")
    if manifest.get("segment_count") != len(segments):
        raise RpgMakerMvError("MANIFEST_SEGMENT_COUNT_MISMATCH")
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in segments):
        raise RpgMakerMvError("MANIFEST_SEGMENT_IDS_INVALID")
    identifiers = [row["id"] for row in segments]
    if len(set(identifiers)) != len(identifiers):
        raise RpgMakerMvError("MANIFEST_SEGMENT_IDS_INVALID")
    return manifest


def _load_results(path: Path) -> dict[str, dict[str, Any]]:
    try:
        if path.suffix.casefold() == ".jsonl":
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        else:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            rows = payload.get("results", payload) if isinstance(payload, dict) else payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RpgMakerMvError("INVALID_RESULT_FILE") from exc
    if not isinstance(rows, list):
        raise RpgMakerMvError("INVALID_RESULT_FILE")
    results: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RpgMakerMvError("INVALID_RESULT_ROW")
        identifier = row.get("segment_id", row.get("id"))
        if not isinstance(identifier, str) or not identifier:
            raise RpgMakerMvError("RESULT_ID_MISSING")
        if identifier in results:
            raise RpgMakerMvError("DUPLICATE_RESULT_ID")
        results[identifier] = row
    return results


def _candidate_issues(source: str, candidate: str, *, line_count: int | None) -> list[str]:
    segment = Segment(id="validation", source=source)
    issues = {issue.code for issue in validate_integrity(segment, candidate, [])}
    for pattern in DEFAULT_PROTECTED_PATTERNS:
        if re.findall(pattern, source) != re.findall(pattern, candidate):
            issues.add("protected-order-mismatch")
            break
    if source.count("\r") != candidate.count("\r"):
        issues.add("carriage-return-mismatch")
    if line_count is not None and len(candidate.split("\n")) != line_count:
        issues.add("line-count-mismatch")
    return sorted(issues)


def _load_baselines(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    game_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = manifest_path.parent.resolve()
    baselines: dict[str, Any] = {}
    documents: dict[str, Any] = {}
    filenames: set[str] = set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise RpgMakerMvError("INVALID_FILE_MANIFEST")
        filename = item.get("file")
        if not isinstance(filename, str) or filename in filenames:
            raise RpgMakerMvError("INVALID_FILE_MANIFEST")
        _safe_relative_path(filename, label="file")
        filenames.add(filename)
        baseline_relative = _safe_relative_path(str(item.get("baseline", "")), label="baseline")
        baseline = (root / baseline_relative).resolve()
        if not baseline.is_relative_to(root) or not baseline.is_file():
            raise RpgMakerMvError("BASELINE_MISSING")
        patch_relative = _safe_relative_path(str(item.get("patch_path", "")), label="patch_path")
        source = game_root / patch_relative
        expected = item.get("sha256")
        if not isinstance(expected, str) or _sha256_file(baseline) != expected:
            raise RpgMakerMvError("BASELINE_CHANGED")
        if not source.is_file() or _sha256_file(source) != expected:
            raise RpgMakerMvError("READ_ONLY_SOURCE_CHANGED")
        try:
            raw = baseline.read_bytes().decode("utf-8-sig")
            if item.get('storage') == 'text':
                bindings = [e for e in manifest["segments"] if e["file"] == filename]
                def matches_bindings(value: str) -> bool:
                    for entry in bindings:
                        start, end = entry["span"]
                        literal = value[start:end]
                        try:
                            decoded = (decode_js_literal(literal) if entry.get("kind") == "script_display_text"
                                       else html.unescape(literal) if entry.get("kind") == "html_title"
                                       else literal)
                        except RpgMakerMvError:
                            return False
                        if decoded != entry["source"]:
                            return False
                    return True
                normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
                if not matches_bindings(raw):
                    if not matches_bindings(normalized):
                        raise RpgMakerMvError("TEXT_SPAN_SOURCE_MISMATCH")
                    raw = normalized
            baseline_document = {'text': raw} if item.get('storage') == 'text' else json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpgMakerMvError("INVALID_BASELINE_JSON") from exc
        baselines[filename] = baseline_document
        documents[filename] = copy.deepcopy(baseline_document)
    return baselines, documents


def _record_direct_assignment(
    assignments: dict[tuple[str, tuple[Any, ...]], str],
    filename: str,
    path: list[Any],
    value: str,
) -> None:
    key = (filename, tuple(path))
    existing = assignments.get(key)
    if existing is not None and existing != value:
        raise RpgMakerMvError("CONFLICTING_PATH_ASSIGNMENT")
    assignments[key] = value


def inspect_script_span_bindings(manifest_path: Path, game_root: Path) -> dict[str, Any]:
    """Return only structural span-coordinate counts, never source or candidates."""
    manifest = _load_manifest(manifest_path)
    baselines, _ = _load_baselines(manifest_path, manifest, game_root)
    counts: Counter[str] = Counter()
    files = {item["file"]: item for item in manifest["files"]}
    for entry in manifest["segments"]:
        if entry.get("kind") != "script_display_text" or "span" not in entry:
            continue
        parent = _get_path(baselines[entry["file"]], entry["paths"][0])
        start, end = entry["span"]
        def matches(value: str) -> bool:
            try:
                return decode_js_literal(value) == entry["source"]
            except RpgMakerMvError:
                return False
        if matches(parent[start:end]):
            counts["canonical"] += 1
            continue
        counts["noncanonical"] += 1
        for name, encoding in (("utf16_offsets", "utf-16-le"), ("utf8_offsets", "utf-8")):
            try:
                data = parent.encode(encoding)
                unit = 2 if name == "utf16_offsets" else 1
                if matches(data[start*unit:end*unit].decode(encoding)):
                    counts[name] += 1
            except UnicodeError:
                pass
        if start > 0 and matches(parent[start-1:end+1]):
            counts["body_only_offsets"] += 1
        item = files[entry["file"]]
        if item.get("storage") == "text":
            data = (manifest_path.parent / item["baseline"]).read_bytes()
            for name, encoding in (("raw_utf8", "utf-8"), ("raw_utf8_sig", "utf-8-sig")):
                if matches(data.decode(encoding)[start:end]):
                    counts[name] += 1
    return {"status": "inspected", "span_counts": dict(counts)}


def apply_translations(
    manifest_path: Path,
    results_path: Path,
    game_root: Path,
    output_root: Path,
    *,
    preserve_missing: bool = True,
    preserve_invalid: bool = True,
) -> dict[str, Any]:
    """Build a new patch tree from validated results without mutating the game or baselines."""
    manifest_path = manifest_path.resolve()
    game_root = game_root.resolve()
    output_root = output_root.resolve()
    _ensure_separate_output(output_root, game_root)
    if output_root.exists():
        raise RpgMakerMvError("OUTPUT_ALREADY_EXISTS")
    manifest = _load_manifest(manifest_path)
    results = _load_results(results_path.resolve())
    manifest_ids = {row["id"] for row in manifest["segments"]}
    unknown = sorted(set(results) - manifest_ids)
    if unknown:
        raise RpgMakerMvError("UNKNOWN_RESULT_ID")
    baselines, documents = _load_baselines(manifest_path, manifest, game_root)
    source_before = _source_snapshot(game_root, manifest["files"])

    assignments: dict[tuple[str, tuple[Any, ...]], str] = {}
    span_edits: dict[tuple[str, tuple[Any, ...]], list[tuple[int, int, str]]] = {}
    issue_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()

    for entry in manifest["segments"]:
        identifier = entry["id"]
        source = entry.get("source")
        filename = entry.get("file")
        paths = entry.get("paths")
        mirrors = entry.get("mirrors", [])
        if (
            not isinstance(source, str)
            or filename not in baselines
            or not isinstance(paths, list)
            or not paths
            or not all(isinstance(path, list) and path for path in paths)
            or not isinstance(mirrors, list)
        ):
            raise RpgMakerMvError("INVALID_SEGMENT_MAPPING")

        row = results.get(identifier)
        if row is None:
            if not preserve_missing:
                raise RpgMakerMvError("MISSING_RESULT")
            target = source
            status_counts["source_preserved_missing"] += 1
        else:
            status = str(row.get("status", ""))
            if status == "source_preserved":
                target = source
                status_counts["source_preserved"] += 1
            elif status not in ACCEPTED_RESULT_STATUSES or not isinstance(row.get("text"), str):
                if not preserve_missing:
                    raise RpgMakerMvError("RESULT_NOT_ACCEPTED")
                target = source
                status_counts["source_preserved_unaccepted"] += 1
            else:
                target = row["text"]
                line_count = len(paths) if entry.get("kind") in {"dialogue", "scrolling_text"} else None
                issues = _candidate_issues(source, target, line_count=line_count)
                if issues:
                    issue_counts.update(issues)
                    if not preserve_invalid:
                        raise RpgMakerMvError("INVALID_TRANSLATION:" + ",".join(issues))
                    else:
                        target = source
                        status_counts["source_preserved_invalid"] += 1
                else:
                    status_counts["applied"] += 1

        baseline_document = baselines[filename]
        if "span" in entry:
            if len(paths) != 1 or mirrors:
                raise RpgMakerMvError("INVALID_SPAN_MAPPING")
            span = entry["span"]
            if not (
                isinstance(span, list)
                and len(span) == 2
                and all(isinstance(value, int) for value in span)
                and 0 <= span[0] <= span[1]
            ):
                raise RpgMakerMvError("INVALID_SPAN_MAPPING")
            path = paths[0]
            parent = _get_path(baseline_document, path)
            if not isinstance(parent, str) or span[1] > len(parent):
                raise RpgMakerMvError("SPAN_PARENT_INVALID")
            original = parent[span[0] : span[1]]
            if entry.get("kind") == "script_display_text":
                if decode_js_literal(original) != source:
                    raise RpgMakerMvError("SCRIPT_SPAN_SOURCE_MISMATCH")
                replacement = encode_js_literal(target, str(entry.get("quote", "")))
            elif entry.get("kind") == "html_title":
                if html.unescape(original) != source:
                    raise RpgMakerMvError("HTML_SPAN_SOURCE_MISMATCH")
                replacement = html.escape(target, quote=False)
            else:
                if original != source:
                    raise RpgMakerMvError("PLUGIN_SPAN_SOURCE_MISMATCH")
                replacement = target
            key = (filename, tuple(path))
            if key in assignments:
                raise RpgMakerMvError("CONFLICTING_PATH_ASSIGNMENT")
            edits = span_edits.setdefault(key, [])
            for start, end, _ in edits:
                if not (span[1] <= start or span[0] >= end):
                    raise RpgMakerMvError("OVERLAPPING_SPAN_ASSIGNMENT")
            edits.append((span[0], span[1], replacement))
            continue

        # Only event dialogue groups distribute text over multiple commands.
        # Database/UI values may contain literal newlines in one JSON field.
        grouped = entry.get("kind") in {"dialogue", "scrolling_text"}
        source_lines = source.split("\n") if grouped else [source]
        target_lines = target.split("\n") if grouped else [target]
        if len(source_lines) != len(paths):
            raise RpgMakerMvError("LINE_PATH_MISMATCH")
        if len(target_lines) != len(paths):
            raise RpgMakerMvError("LINE_PATH_MISMATCH")
        for path, source_line, target_line in zip(paths, source_lines, target_lines):
            if _get_path(baseline_document, path) != source_line:
                raise RpgMakerMvError("BASELINE_VALUE_MISMATCH")
            key = (filename, tuple(path))
            if key in span_edits:
                raise RpgMakerMvError("CONFLICTING_PATH_ASSIGNMENT")
            _record_direct_assignment(assignments, filename, path, target_line)
        for path in mirrors:
            if not isinstance(path, list) or not path:
                raise RpgMakerMvError("INVALID_MIRROR_PATH")
            if _get_path(baseline_document, path) != source:
                raise RpgMakerMvError("MIRROR_VALUE_MISMATCH")
            key = (filename, tuple(path))
            if key in span_edits:
                raise RpgMakerMvError("CONFLICTING_PATH_ASSIGNMENT")
            _record_direct_assignment(assignments, filename, path, target)

    for (filename, path), value in assignments.items():
        _set_path(documents[filename], list(path), value)
    for (filename, path), edits in span_edits.items():
        parent = _get_path(baselines[filename], path)
        for start, end, replacement in sorted(edits, reverse=True):
            parent = parent[:start] + replacement + parent[end:]
        _set_path(documents[filename], list(path), parent)

    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mv-patch-", dir=output_root.parent) as temporary:
        temporary_root = Path(temporary)
        for item in manifest["files"]:
            relative = _safe_relative_path(str(item["patch_path"]), label="patch_path")
            target = temporary_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            output_text = (documents[item['file']]['text'] if item.get('storage') == 'text'
                           else json.dumps(documents[item["file"]], ensure_ascii=False, separators=(",", ":")))
            target.write_bytes(output_text.encode("utf-8"))
            if item.get('storage') != 'text':
                json.loads(target.read_text(encoding="utf-8"))
        verification = _verify_patch_loaded(manifest_path, manifest, game_root, temporary_root)
        temporary_root.replace(output_root)

    source_after = _source_snapshot(game_root, manifest["files"])
    if source_before != source_after:
        raise RpgMakerMvError("READ_ONLY_SOURCE_CHANGED")
    return {
        "status": "success",
        "targets": len(manifest["segments"]),
        "status_counts": dict(status_counts),
        "validation_issue_codes": dict(issue_counts),
        "output_files": verification["output_files"],
        "applied_paths": len(assignments) + len(span_edits),
        "source_unchanged": True,
    }


def _leaf_differences(before: Any, after: Any, path: tuple[Any, ...] = ()) -> set[tuple[Any, ...]]:
    if type(before) is not type(after):
        return {path}
    if isinstance(before, dict):
        if before.keys() != after.keys():
            return {path}
        differences: set[tuple[Any, ...]] = set()
        for key in before:
            differences.update(_leaf_differences(before[key], after[key], path + (key,)))
        return differences
    if isinstance(before, list):
        if len(before) != len(after):
            return {path}
        differences = set()
        for index, (left, right) in enumerate(zip(before, after)):
            differences.update(_leaf_differences(left, right, path + (index,)))
        return differences
    return set() if before == after else {path}


def _verify_patch_loaded(
    manifest_path: Path,
    manifest: Mapping[str, Any],
    game_root: Path,
    patch_root: Path,
) -> dict[str, Any]:
    baselines, _ = _load_baselines(manifest_path, manifest, game_root)
    expected_paths = {
        _safe_relative_path(str(item["patch_path"]), label="patch_path").as_posix()
        for item in manifest["files"]
    }
    actual_paths = {
        path.relative_to(patch_root).as_posix() for path in patch_root.rglob("*") if path.is_file()
    }
    if actual_paths != expected_paths:
        raise RpgMakerMvError("PATCH_FILE_SET_MISMATCH")

    allowed: dict[str, set[tuple[Any, ...]]] = {name: set() for name in baselines}
    for entry in manifest["segments"]:
        filename = entry["file"]
        for path in entry.get("paths", []):
            allowed[filename].add(tuple(path))
        for path in entry.get("mirrors", []):
            allowed[filename].add(tuple(path))

    for item in manifest["files"]:
        relative = _safe_relative_path(str(item["patch_path"]), label="patch_path")
        if item.get('storage') == 'text':
            original = baselines[item['file']]['text']
            current = (patch_root / relative).read_bytes().decode('utf-8-sig')
            entries = sorted((entry for entry in manifest['segments'] if entry['file'] == item['file']), key=lambda entry: entry['span'])
            cursor = 0
            chunks = []
            for entry in entries:
                start, end = entry['span']
                if start < cursor or end > len(original):
                    raise RpgMakerMvError('INVALID_SPAN_MAPPING')
                chunks.append(re.escape(original[cursor:start]))
                if entry.get('kind') == 'script_display_text':
                    quote = str(entry.get('quote', ''))
                    if quote not in {'"', "'"} or original[start:end][:1] != quote:
                        raise RpgMakerMvError('INVALID_SCRIPT_QUOTE')
                    chunks.append(re.escape(quote) + r'(?:\\[^\r\n]|[^' + re.escape(quote) + r'\\\r\n])*' + re.escape(quote))
                elif entry.get('kind') == 'html_title':
                    chunks.append(r'(?:[^<&]|&(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#x[0-9A-Fa-f]+);)*')
                else:
                    raise RpgMakerMvError('INVALID_EXTERNAL_SPAN_KIND')
                cursor = end
            chunks.append(re.escape(original[cursor:]))
            if not re.fullmatch(''.join(chunks), current):
                raise RpgMakerMvError('PATCH_CHANGED_UNMAPPED_DATA')
            continue
        try:
            output_document = json.loads((patch_root / relative).read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpgMakerMvError("INVALID_PATCH_JSON") from exc
        differences = _leaf_differences(baselines[item["file"]], output_document)
        if not differences.issubset(allowed[item["file"]]):
            raise RpgMakerMvError("PATCH_CHANGED_UNMAPPED_DATA")
    _source_snapshot(game_root, manifest["files"])
    return {"status": "verified", "output_files": len(actual_paths), "source_unchanged": True}


def verify_patch(manifest_path: Path, game_root: Path, patch_root: Path) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    game_root = game_root.resolve()
    patch_root = patch_root.resolve()
    _ensure_separate_output(patch_root, game_root)
    if not patch_root.is_dir():
        raise RpgMakerMvError("PATCH_ROOT_MISSING")
    manifest = _load_manifest(manifest_path)
    return _verify_patch_loaded(manifest_path, manifest, game_root, patch_root)


def pack_patch(patch_root: Path, archive_path: Path) -> dict[str, Any]:
    """Create a deterministic ZIP containing only the verified JSON patch tree."""
    patch_root = patch_root.resolve()
    archive_path = archive_path.resolve()
    if not patch_root.is_dir():
        raise RpgMakerMvError("PATCH_ROOT_MISSING")
    if archive_path.exists():
        raise RpgMakerMvError("ARCHIVE_ALREADY_EXISTS")
    if archive_path == patch_root or archive_path.is_relative_to(patch_root):
        raise RpgMakerMvError("ARCHIVE_INSIDE_PATCH_ROOT")
    files = sorted(path for path in patch_root.rglob("*") if path.is_file())
    if not files or any(path.suffix.casefold() not in {".json", ".js", ".html"} for path in files):
        raise RpgMakerMvError("PATCH_CONTAINS_UNEXPECTED_FILES")
    for path in files:
        try:
            text = path.read_bytes().decode("utf-8-sig")
            if path.suffix.casefold() == ".json":
                json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RpgMakerMvError("INVALID_PATCH_JSON") from exc
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(patch_root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    return {
        "status": "packed",
        "files": len(files),
        "archive_sha256": _sha256_file(archive_path),
    }
