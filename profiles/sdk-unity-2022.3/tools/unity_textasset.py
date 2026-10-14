from __future__ import annotations

import collections
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


UNITY_VERSION_RE = re.compile(rb"20\d{2}\.\d+\.\d+[abfp]\d+")
LOCALE_RE = re.compile(r"^(?:[a-z]{2,3})(?:-[A-Z0-9]{2,3})?$")
FORMAT_RE = re.compile(r"\{[^{}]+\}")
TAG_RE = re.compile(
    r"</?(?:b|i|u|s|color|size|sprite|link|font|mark|align|alpha|br|indent|line-height|"
    r"line-indent|margin|mspace|nobr|noparse|page|pos|rotate|space|style|sub|sup|voffset|width)"
    r"(?:\s+[^<>]*)?/?>",
    re.I,
)
PRINTF_RE = re.compile(r"%(?:\d+\$)?[-+#0 ]*(?:\d+|\*)?(?:\.\d+)?[diuoxXfFeEgGaAcspn%]")
ESCAPE_RE = re.compile(r"\\[nrt0\\]")


@dataclass(frozen=True)
class IniEntry:
    section: str
    key: str
    value: str
    order: int
    line: int

    @property
    def pair(self) -> tuple[str, str]:
        return self.section, self.key

    @property
    def segment_id(self) -> str:
        return f"loc:{self.section}:{self.key}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_tree(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        stat = path.stat()
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": _sha256(path),
            }
        )
    return rows


def _snapshot_digest(rows: list[dict[str, Any]]) -> str:
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _workspace_path(workspace: Path, value: str, *, must_exist: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = workspace / path
    path = path.resolve()
    if not _is_within(path, workspace):
        raise ValueError("WORKSPACE_PATH_OUTSIDE_ROOT")
    if must_exist and not path.exists():
        raise FileNotFoundError(path)
    return path


def _source_path(game_root: Path, relative: str) -> Path:
    path = (game_root / relative).resolve()
    if not _is_within(path, game_root):
        raise ValueError("SOURCE_PATH_OUTSIDE_ROOT")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _load_environment(path: Path) -> Any:
    try:
        import UnityPy  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("UNITYPY_1_25_4_REQUIRED") from exc
    return UnityPy.load(str(path))


def _find_data_root(game_root: Path) -> Path:
    candidates = sorted(
        path for path in game_root.iterdir() if path.is_dir() and path.name.casefold().endswith("_data")
    )
    if len(candidates) != 1:
        raise ValueError("UNITY_DATA_ROOT_NOT_UNIQUE")
    return candidates[0]


def _read_unity_version(data_root: Path) -> str:
    for name in ("globalgamemanagers", "data.unity3d"):
        path = data_root / name
        if path.is_file():
            with path.open("rb") as handle:
                match = UNITY_VERSION_RE.search(handle.read(8 * 1024 * 1024))
            if match:
                return match.group(0).decode("ascii")
    raise ValueError("UNITY_VERSION_NOT_FOUND")


def _require_supported_player(game_root: Path) -> tuple[Path, str]:
    data_root = _find_data_root(game_root)
    version = _read_unity_version(data_root)
    if not version.startswith("2022.3."):
        raise ValueError("UNITY_VERSION_NOT_2022_3")
    if (game_root / "GameAssembly.dll").is_file() and (data_root / "il2cpp_data").is_dir():
        raise ValueError("UNITY_IL2CPP_NOT_SUPPORTED_BY_PROFILE")
    if not (data_root / "Managed").is_dir():
        raise ValueError("UNITY_MONO_LAYOUT_NOT_FOUND")
    return data_root, version


def _text_asset_bytes(data: Any) -> bytes:
    payload = data.m_Script
    return payload.encode("utf-8", "surrogateescape") if isinstance(payload, str) else bytes(payload)


def _text_assets(environment: Any) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for obj in environment.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        name = str(data.m_Name)
        if name in result:
            raise ValueError("DUPLICATE_TEXTASSET_NAME")
        result[name] = {"object": obj, "data": data, "script": _text_asset_bytes(data)}
    return result


def parse_ini(text: str, *, strip_values: bool = True) -> list[IniEntry]:
    section = ""
    entries: list[IniEntry] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith((";", "#")):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if "=" not in raw:
            raise ValueError("INI_MALFORMED_LINE")
        if not section:
            raise ValueError("INI_ENTRY_WITHOUT_SECTION")
        key, value = raw.split("=", 1)
        normalized_value = value.strip() if strip_values else value
        entries.append(IniEntry(section, key.strip(), normalized_value, len(entries), line_no))
    counts = collections.Counter(entry.pair for entry in entries)
    if any(count > 1 for count in counts.values()):
        raise ValueError("INI_DUPLICATE_KEY")
    return entries


def rewrite_ini(
    original: str, replacements: dict[tuple[str, str], str]
) -> tuple[str, list[tuple[str, str]]]:
    section = ""
    rewritten: list[str] = []
    replaced: list[tuple[str, str]] = []
    for item in original.splitlines(keepends=True):
        if item.endswith("\r\n"):
            raw, ending = item[:-2], "\r\n"
        elif item.endswith(("\n", "\r")):
            raw, ending = item[:-1], item[-1]
        else:
            raw, ending = item, ""
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif stripped and not stripped.startswith((";", "#")):
            if "=" not in raw:
                raise ValueError("INI_MALFORMED_LINE")
            left, _old = raw.split("=", 1)
            pair = (section, left.strip())
            if pair in replacements:
                raw = left + "=" + replacements[pair]
                replaced.append(pair)
        rewritten.append(raw + ending)
    return "".join(rewritten), replaced


def _tokens(value: str) -> dict[str, list[str]]:
    return {
        "format": FORMAT_RE.findall(value),
        "tags": TAG_RE.findall(value),
        "printf": PRINTF_RE.findall(value),
        "escapes": ESCAPE_RE.findall(value),
    }


def _token_mismatch(source: str, target: str) -> bool:
    source_tokens = _tokens(source)
    target_tokens = _tokens(target)
    return any(
        collections.Counter(source_tokens[key]) != collections.Counter(target_tokens[key])
        for key in source_tokens
    )


def _decode_tables(assets: dict[str, dict[str, Any]]) -> dict[str, list[IniEntry]]:
    tables: dict[str, list[IniEntry]] = {}
    for name, item in assets.items():
        try:
            tables[name] = parse_ini(item["script"].decode("utf-8-sig", "strict"))
        except (UnicodeError, ValueError):
            continue
    return tables


def _select_source_locale(tables: dict[str, list[IniEntry]], target_locale: str) -> str:
    explicit = os.environ.get("GAME_L10N_SOURCE_LOCALE", "").strip()
    locales = {name: entries for name, entries in tables.items() if LOCALE_RE.fullmatch(name)}
    if explicit:
        if explicit == target_locale or explicit not in locales:
            raise ValueError("SOURCE_LOCALE_INVALID")
        return explicit
    reference_values = [
        {entry.value for entry in entries if entry.value}
        for name, entries in tables.items()
        if name not in locales
    ]
    scores: dict[str, int] = {}
    for locale, entries in locales.items():
        if locale != target_locale:
            values = {entry.value for entry in entries if entry.value}
            scores[locale] = max(
                (len(values & reference) for reference in reference_values), default=0
            )
    if not scores:
        raise ValueError("SOURCE_LOCALE_NOT_FOUND")
    highest = max(scores.values())
    winners = sorted(locale for locale, score in scores.items() if score == highest and score > 0)
    if len(winners) != 1:
        raise ValueError("SOURCE_LOCALE_AMBIGUOUS_SET_GAME_L10N_SOURCE_LOCALE")
    return winners[0]


def _discover_language_bundle(
    data_root: Path, target_locale: str
) -> tuple[Path, Any, dict[str, dict[str, Any]], dict[str, list[IniEntry]], str]:
    bundle_root = data_root / "StreamingAssets" / "Bundles"
    if not bundle_root.is_dir():
        raise FileNotFoundError("UNITY_BUNDLE_ROOT_NOT_FOUND")
    matches: list[
        tuple[Path, Any, dict[str, dict[str, Any]], dict[str, list[IniEntry]], str]
    ] = []
    for path in sorted(
        item
        for item in bundle_root.iterdir()
        if item.is_file() and item.suffix.casefold() != ".manifest"
    ):
        try:
            environment = _load_environment(path)
            assets = _text_assets(environment)
            tables = _decode_tables(assets)
        except Exception:
            continue
        if target_locale not in tables or len(
            [name for name in tables if LOCALE_RE.fullmatch(name)]
        ) < 2:
            continue
        try:
            source_locale = _select_source_locale(tables, target_locale)
        except ValueError:
            continue
        matches.append((path, environment, assets, tables, source_locale))
    if len(matches) != 1:
        raise ValueError("UNITY_LANGUAGE_BUNDLE_NOT_UNIQUE")
    return matches[0]


def extract_inventory(game_root: Path, output_root: Path) -> dict[str, Any]:
    if not game_root.is_dir():
        raise FileNotFoundError(game_root)
    if _is_within(output_root, game_root):
        raise ValueError("OUTPUT_INSIDE_READ_ONLY_GAME_ROOT")
    if output_root.exists():
        raise ValueError("OUTPUT_ALREADY_EXISTS")
    target_locale = os.environ.get("GAME_L10N_TARGET_LOCALE", "ko").strip()
    if not LOCALE_RE.fullmatch(target_locale):
        raise ValueError("TARGET_LOCALE_INVALID")
    before = _snapshot_tree(game_root)
    data_root, version = _require_supported_player(game_root)
    bundle, _environment, _assets, tables, source_locale = _discover_language_bundle(
        data_root, target_locale
    )
    source_entries = tables[source_locale]
    target_entries = tables[target_locale]
    source_pairs = [entry.pair for entry in source_entries]
    if source_pairs != [entry.pair for entry in target_entries]:
        raise ValueError("LOCALE_KEY_OR_ORDER_MISMATCH")
    target_by_pair = {entry.pair: entry for entry in target_entries}
    bundle_relative = bundle.relative_to(game_root).as_posix()
    segments = [
        {
            "id": entry.segment_id,
            "order": entry.order,
            "source": entry.value,
            "existing_target": target_by_pair[entry.pair].value,
            "metadata": {
                "unity_kind": "text_asset_ini",
                "bundle": bundle_relative,
                "source_asset": source_locale,
                "target_asset": target_locale,
                "section": entry.section,
                "key": entry.key,
                "source_line": entry.line,
                "target_line": target_by_pair[entry.pair].line,
            },
        }
        for entry in source_entries
    ]
    after = _snapshot_tree(game_root)
    if before != after:
        raise RuntimeError("SOURCE_TREE_MUTATED_DURING_EXTRACTION")
    output_root.mkdir(parents=True)
    _write_json(output_root / "segments.json", segments)
    _write_json(
        output_root / "extraction-summary.json",
        {
            "schema_version": 1,
            "status": "complete",
            "engine": "unity",
            "unity_series": "2022.3",
            "unity_version": version,
            "backend": "mono",
            "method": "unity_2022_3_textasset_ini_primary_v1",
            "segments": len(segments),
            "source_locale": source_locale,
            "target_locale": target_locale,
            "source_bundle_relative": bundle_relative,
            "source_bundle_sha256": _sha256(bundle),
            "source_immutable": True,
            "source_tree_digest": _snapshot_digest(before),
            "coverage": {
                "static_complete": False,
                "exclusions": [
                    "managed_literals",
                    "non_ini_textassets",
                    "runtime_generated_text",
                    "ui_layout",
                ],
            },
        },
    )
    return {"status": "complete", "segments": len(segments), "source_immutable": True}


def _load_segments(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("segments")
    if not isinstance(value, list):
        raise ValueError("SEGMENTS_ROOT_NOT_LIST")
    return value


def _load_reviewed(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = value.get("results")
    if not isinstance(value, list):
        raise ValueError("REVIEWED_ROOT_NOT_LIST")
    return value


def _validated_translations(
    segments: list[dict[str, Any]], reviewed: list[dict[str, Any]]
) -> tuple[dict[str, str], dict[str, int]]:
    segment_ids = [str(row.get("id")) for row in segments]
    reviewed_ids = [str(row.get("segment_id") or row.get("id")) for row in reviewed]
    if len(segment_ids) != len(set(segment_ids)):
        raise ValueError("SEGMENT_ID_DUPLICATE")
    if len(reviewed_ids) != len(set(reviewed_ids)):
        raise ValueError("REVIEWED_ID_DUPLICATE")
    if set(segment_ids) != set(reviewed_ids):
        raise ValueError("MAPPING_NOT_EXACT")
    if [int(row.get("order", -1)) for row in segments] != list(range(len(segments))):
        raise ValueError("SEGMENT_ORDER_NOT_CONTIGUOUS")
    by_id = {str(row["id"]): row for row in segments}
    translations: dict[str, str] = {}
    statuses: collections.Counter[str] = collections.Counter()
    for record in reviewed:
        segment_id = str(record.get("segment_id") or record.get("id"))
        segment = by_id[segment_id]
        if str(record.get("source")) != str(segment.get("source")):
            raise ValueError("REVIEWED_SOURCE_MISMATCH")
        status = str(record.get("status"))
        if status not in {"reviewed", "source_preserved"}:
            raise ValueError("REVIEWED_STATUS_INVALID")
        text = record.get("text")
        if not isinstance(text, str):
            raise ValueError("REVIEWED_TEXT_NOT_STRING")
        if status == "source_preserved" and text != str(segment["source"]):
            raise ValueError("SOURCE_PRESERVED_NOT_EXACT")
        if _token_mismatch(str(segment["source"]), text):
            raise ValueError("PLACEHOLDER_CONTROL_MISMATCH")
        translations[segment_id] = text
        statuses[status] += 1
    return translations, dict(statuses)


def _object_signature(environment: Any) -> list[tuple[int, str]]:
    return [(int(obj.path_id), obj.type.name) for obj in environment.objects]


def _common_metadata(
    segments: Iterable[dict[str, Any]],
) -> tuple[str, str, list[tuple[str, str]]]:
    bundle_values: set[str] = set()
    target_values: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for row in segments:
        metadata = row.get("metadata") or {}
        if metadata.get("unity_kind") != "text_asset_ini":
            raise ValueError("SEGMENT_KIND_NOT_SUPPORTED")
        bundle_values.add(str(metadata.get("bundle")))
        target_values.add(str(metadata.get("target_asset")))
        pairs.append((str(metadata.get("section")), str(metadata.get("key"))))
    if len(bundle_values) != 1 or len(target_values) != 1:
        raise ValueError("SEGMENT_LOCATION_NOT_UNIFORM")
    return next(iter(bundle_values)), next(iter(target_values)), pairs


def build_payload(request_path: Path, output_root: Path) -> dict[str, Any]:
    if os.environ.get("GAME_L10N_STAGE") != "pack":
        raise ValueError("PACK_STAGE_BOUNDARY_REQUIRED")
    workspace_value = os.environ.get("GAME_L10N_WORKDIR", "")
    if not workspace_value:
        raise ValueError("WORKSPACE_BOUNDARY_REQUIRED")
    workspace = Path(workspace_value).resolve()
    if (
        not workspace.is_dir()
        or not _is_within(request_path, workspace)
        or not _is_within(output_root, workspace)
    ):
        raise ValueError("WORKSPACE_BOUNDARY_INVALID")
    if output_root.exists():
        raise ValueError("OUTPUT_ALREADY_EXISTS")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("schema_version") != 1:
        raise ValueError("PACK_REQUEST_SCHEMA_INVALID")
    game_root = Path(str(request.get("game_root", ""))).resolve()
    if not game_root.is_dir():
        raise FileNotFoundError("GAME_ROOT_MISSING")
    _data_root, version = _require_supported_player(game_root)
    segments = _load_segments(
        _workspace_path(workspace, str(request.get("segments_path", "")))
    )
    reviewed = _load_reviewed(
        _workspace_path(workspace, str(request.get("reviewed_path", "")))
    )
    translations, statuses = _validated_translations(segments, reviewed)
    bundle_relative, target_asset, expected_pairs = _common_metadata(segments)
    requested_relative = str(
        request.get("source_bundle_relative") or bundle_relative
    ).replace("\\", "/")
    if requested_relative != bundle_relative:
        raise ValueError("SOURCE_BUNDLE_MAPPING_MISMATCH")
    source_bundle = _source_path(game_root, bundle_relative)
    expected_hash = str(request.get("source_bundle_sha256", ""))
    if not expected_hash or _sha256(source_bundle) != expected_hash:
        raise ValueError("SOURCE_BUNDLE_HASH_MISMATCH")
    before = _snapshot_tree(game_root)
    source_environment = _load_environment(source_bundle)
    source_assets = _text_assets(source_environment)
    if target_asset not in source_assets:
        raise ValueError("TARGET_TEXTASSET_MISSING")
    original = source_assets[target_asset]["script"].decode("utf-8", "surrogateescape")
    if [entry.pair for entry in parse_ini(original)] != expected_pairs:
        raise ValueError("TARGET_RESOURCE_ORDER_MISMATCH")
    replacements = {
        pair: translations[str(row["id"])] for row, pair in zip(segments, expected_pairs)
    }
    rebuilt, replaced_pairs = rewrite_ini(original, replacements)
    if replaced_pairs != expected_pairs:
        raise ValueError("TARGET_RESOURCE_REPLACEMENT_MISMATCH")
    source_signature = _object_signature(source_environment)
    source_raw = {int(obj.path_id): obj.get_raw_data() for obj in source_environment.objects}
    target_path_id = int(source_assets[target_asset]["object"].path_id)
    source_assets[target_asset]["data"].m_Script = rebuilt
    source_assets[target_asset]["data"].save()
    payload_bundle = output_root / bundle_relative
    payload_bundle.parent.mkdir(parents=True)
    source_environment.save(pack="original", out_path=str(payload_bundle.parent))
    if not payload_bundle.is_file():
        raise ValueError("BUNDLE_WRITE_MISSING")
    output_environment = _load_environment(payload_bundle)
    output_assets = _text_assets(output_environment)
    if _object_signature(output_environment) != source_signature:
        raise ValueError("OBJECT_STRUCTURE_MISMATCH")
    if set(output_assets) != set(source_assets):
        raise ValueError("TEXTASSET_SET_MISMATCH")
    output_by_id = {int(obj.path_id): obj for obj in output_environment.objects}
    for path_id, raw in source_raw.items():
        if path_id != target_path_id and output_by_id[path_id].get_raw_data() != raw:
            raise ValueError("NON_TARGET_OBJECT_BYTES_CHANGED")
    for name in source_assets:
        if name != target_asset and output_assets[name]["script"] != source_assets[name]["script"]:
            raise ValueError("NON_TARGET_TEXTASSET_CHANGED")
    output_entries = parse_ini(
        output_assets[target_asset]["script"].decode("utf-8", "surrogateescape"),
        strip_values=False,
    )
    if [entry.pair for entry in output_entries] != expected_pairs:
        raise ValueError("OUTPUT_TARGET_ORDER_MISMATCH")
    output_values = {entry.pair: entry.value for entry in output_entries}
    if any(output_values.get(pair) != replacements[pair] for pair in expected_pairs):
        raise ValueError("OUTPUT_TARGET_VALUE_MISMATCH")
    after = _snapshot_tree(game_root)
    if before != after:
        raise RuntimeError("SOURCE_TREE_MUTATED_DURING_PACK")
    report = {
        "schema_version": 1,
        "status": "valid",
        "engine": {
            "name": "Unity",
            "series": "2022.3",
            "version": version,
            "backend": "Mono",
        },
        "strategy": "unityfs_copy_single_ini_textasset",
        "mapping": {
            "segments": len(segments),
            "mapped": len(translations),
            "missing": 0,
            "extra": 0,
            "duplicate": 0,
        },
        "translation_status": statuses,
        "integrity": {
            "resource_order_preserved": True,
            "object_structure_equal": True,
            "non_target_object_byte_mismatches": 0,
            "non_target_textasset_mismatches": 0,
            "placeholder_control_mismatches": 0,
        },
        "payload": {
            "files": 1,
            "relative_path": bundle_relative,
            "source_sha256": expected_hash,
            "payload_sha256": _sha256(payload_bundle),
            "payload_size": payload_bundle.stat().st_size,
        },
        "source_immutability": {
            "files_before": len(before),
            "files_after": len(after),
            "digest_before": _snapshot_digest(before),
            "digest_after": _snapshot_digest(after),
            "identical": True,
        },
    }
    report_value = request.get("report_path")
    report_path = (
        _workspace_path(workspace, str(report_value), must_exist=False)
        if report_value
        else output_root.parent / f"{output_root.name}-validation.json"
    )
    _write_json(report_path, report)
    return {
        "status": "valid",
        "segments": len(segments),
        "payload_files": 1,
        "source_immutable": True,
        "report": str(report_path),
    }
