"""Unity 2022.3 inventory orchestration: one source snapshot, method-local scans."""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from unity_inventory_cache import ScanCache, encoded, file_digest, fingerprint, relative_file


METHODS = {"primary", "independent"}


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _workspace() -> Path:
    value = os.environ.get("GAME_L10N_WORKDIR", "")
    if not value:
        raise ValueError("SDK_WORKSPACE_REQUIRED")
    return Path(value).resolve()


def require_prepared() -> tuple[Path, dict[str, Any]]:
    """Standalone helpers are reusable; actual inventory admission is core-bound."""
    from game_l10n.workspace import load_config, require_sdk_activation
    from game_l10n.extractor_preparation import require_extractor_preparation
    work = _workspace()
    config = load_config(work)
    activation = require_sdk_activation(work, config)
    require_extractor_preparation(work, config, activation)
    return work, activation


def scanner_fingerprint(method: str, extra: Any = None) -> str:
    root = Path(__file__).resolve().parent
    files = [[path.name, file_digest(path)] for path in sorted(root.glob("*.py"))]
    return fingerprint({"method": method, "files": files, "inputs": extra})


def _candidate(path: Path, data_root: Path, game_root: Path) -> bool:
    if path.name == "app.info":
        return path.parent == data_root
    if path.suffix.lower() in {".dll", ".exe"}:
        return True
    if not _inside(path, data_root):
        return False
    return path.suffix.lower() in {".assets", ".bundle", ".unity3d"} or path.name in {"globalgamemanagers", "data.unity3d", "unity default resources", "unity_builtin_extra"} or re.fullmatch(r"level\d+", path.name) is not None


def capture_source(game_root: Path, destination: Path) -> dict[str, Any]:
    from unity_textasset import _require_supported_player
    if _inside(destination, game_root):
        raise ValueError("OUTPUT_INSIDE_READ_ONLY_GAME_ROOT")
    data_root, version = _require_supported_player(game_root)
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(game_root.rglob("*")):
        if path.is_symlink():
            raise ValueError("SOURCE_SYMLINK_UNSUPPORTED")
        if not path.is_file() or not _candidate(path, data_root, game_root):
            continue
        relative = path.relative_to(game_root).as_posix()
        before = path.stat()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with path.open("rb") as source, temporary.open("wb") as output:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
                    output.write(block)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("SOURCE_CHANGED_DURING_SNAPSHOT")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        records.append({"file": relative, "sha256": digest.hexdigest(), "size": before.st_size})
    return {"schema": "parr0t.unity-source-snapshot.v1", "unity_version": version, "files": records}


def _merge(results: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {"occurrences": [], "types": {}, "dependencies": [], "pending": [], "proofs": {}}
    for result in results:
        for key in ("occurrences", "dependencies", "pending"):
            output[key].extend(result.get(key, []))
        for key in ("types", "proofs"):
            value = result.get(key, {})
            if isinstance(value, dict):
                for name, item in value.items():
                    if name in output[key] and output[key][name] != item:
                        raise ValueError("SDK_SHARED_EVIDENCE_COLLISION")
                    output[key][name] = item
            elif value:
                output[key]["records:" + fingerprint(value)] = value
    return output


def _pe_is_managed(path: Path) -> bool:
    from unity_pe import directory_info
    raw = path.read_bytes()
    return bool(directory_info(raw, 14)[0])




def scan_snapshot(snapshot: Path, manifest: dict[str, Any], cache_path: Path, output: Path, *, method: str) -> dict[str, Any]:
    """Private worker API: never reads the counterpart's inventories or analyses."""
    from unity_managed import scan_assembly
    from unity_native import scan_native
    from unity_objects import bind_script_references, scan_serialized
    from unity_native_calls import scan_native_dependencies
    from unity_native_resources import reconcile_resource_dependencies
    if method not in METHODS:
        raise ValueError("INVENTORY_METHOD_INVALID")
    if manifest.get("schema") != "parr0t.unity-source-snapshot.v1" or not str(manifest.get("unity_version", "")).startswith("2022.3."):
        raise ValueError("SDK_SNAPSHOT_IDENTITY_INVALID")
    files = manifest.get("files", [])
    names = [relative_file(record["file"]) for record in files]
    if len(names) != len(set(names)):
        raise ValueError("SDK_SNAPSHOT_FILE_DUPLICATE")
    for record in files:
        path = (snapshot / record["file"]).resolve()
        if not _inside(path, snapshot) or path.is_symlink() or not path.is_file() or path.stat().st_size != record["size"] or file_digest(path) != record["sha256"]:
            raise ValueError("SDK_SNAPSHOT_BINDING_MISMATCH")
    implementation = scanner_fingerprint(method)
    managed_types: dict[str, Any] = {}
    serialized = []
    reused = scanned = 0
    with ScanCache(cache_path) as cache:
        cache.retain_files(method, set(names))
        for record in files:
            file, digest = record["file"], record["sha256"]
            path = snapshot / file
            if path.name != "app.info" and path.suffix.lower() not in {".dll", ".exe"}:
                serialized.append(record)
                continue
            if cache.reusable(method, file, digest, implementation):
                result = cache.metadata(method, file)
                reused += 1
            else:
                if path.name == "app.info":
                    result = scan_native(path, file, method=method)
                else:
                    resources = scan_native(path, file, method=method)
                    native = scan_native_dependencies(path.read_bytes(), file, method=method)
                    results = [resources, reconcile_resource_dependencies(resources, native)]
                    if _pe_is_managed(path):
                        results.append(scan_assembly(path, file, method=method))
                    result = _merge(results)
                cache.store(method, file, digest, implementation, result)
                scanned += 1
            for name, layout in result.get("types", {}).items():
                if name in managed_types and managed_types[name] != layout:
                    raise ValueError("SDK_MANAGED_TYPE_AMBIGUOUS")
                managed_types[name] = layout
        type_digest = fingerprint(managed_types)
        object_implementation = fingerprint([implementation, type_digest])
        # The first pass records physical MonoScript/PPtr bindings. The second
        # decodes only unresolved dependent objects from the same immutable bytes;
        # it is not another source capture or another independent inventory.
        staged: dict[str, dict[str, Any]] = {}
        object_results = []
        for record in serialized:
            file, digest = record["file"], record["sha256"]
            cached = cache.metadata(method, file) if cache.source_matches(method, file, digest) else None
            if cached is not None and cached.get("base_scanner_digest") == object_implementation:
                result = cached
            else:
                result = scan_serialized(snapshot / file, file, method=method, managed_types=managed_types)
                staged[file] = result
            object_results.append(result)
        scripts = bind_script_references(object_results)
        complete_types = {**managed_types, "__script_bindings__": scripts}
        bound_implementation = fingerprint([implementation, type_digest, scripts])
        for record in serialized:
            file, digest = record["file"], record["sha256"]
            if cache.reusable(method, file, digest, bound_implementation):
                if file not in staged:
                    reused += 1
                    continue
            result = staged.get(file)
            if result is None or any(isinstance(item, dict) and item.get("reason") == "MONOSCRIPT_DEPENDENCY_MISSING" for item in result.get("pending", [])):
                result = scan_serialized(snapshot / file, file, method=method, managed_types=complete_types)
            result["base_scanner_digest"] = object_implementation
            cache.store(method, file, digest, bound_implementation, result)
            scanned += 1
        summary = cache.inventory(method, output, implementation)
    return {"status": "static_inventory_ready" if summary["static_complete"] else "inventory_pending", "method": method, "scanned_files": scanned, "reused_files": reused, **summary}


def extract_inventory(game_root: Path, output_root: Path) -> dict[str, Any]:
    work, _ = require_prepared()
    if not _inside(output_root, work) or _inside(output_root, game_root):
        raise ValueError("OUTPUT_OUTSIDE_PRIVATE_WORKSPACE")
    method = os.environ.get("GAME_L10N_INVENTORY_METHOD", "primary")
    if method not in METHODS:
        raise ValueError("INVENTORY_METHOD_INVALID")
    output_root.mkdir(parents=True, exist_ok=True)
    snapshot = output_root / "source-snapshot"
    manifest_path = output_root / "source-snapshot.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = capture_source(game_root, snapshot)
        manifest_path.write_text(encoded(manifest) + "\n", encoding="utf-8")
    return scan_snapshot(snapshot, manifest, output_root / "evidence.sqlite3", output_root / "inventory.json", method=method)


def build_payload(request_path: Path, output_root: Path) -> dict[str, Any]:
    from game_l10n.formats import read_segments
    from unity_managed import patch_assembly
    from unity_native import patch_native
    from unity_native_calls import patch_native_literals
    from unity_objects import patch_serialized
    from unity_textasset import _load_reviewed, _validated_translations
    if os.environ.get("GAME_L10N_STAGE") != "pack":
        raise ValueError("SDK_PACK_STAGE_REQUIRED")
    work, _ = require_prepared()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    game_root = Path(request["game_root"]).resolve()
    if not _inside(request_path, work) or not _inside(output_root, work) or _inside(output_root, game_root) or output_root.exists():
        raise ValueError("SDK_PACK_WORKSPACE_BINDING_INVALID")
    def input_path(name: str) -> Path:
        path = Path(request[name])
        path = path.resolve() if path.is_absolute() else (work / path).resolve()
        if not _inside(path, work) or not path.is_file():
            raise ValueError("SDK_PACK_INPUT_BINDING_INVALID")
        return path
    segments = read_segments(input_path("segments_path"))
    rows = [{"id": row.id, "source": row.source, "order": index, "metadata": row.metadata} for index, row in enumerate(segments)]
    translations, statuses = _validated_translations(rows, _load_reviewed(input_path("reviewed_path")))
    manifest = json.loads(input_path("source_manifest_path").read_text(encoding="utf-8"))
    expected = {relative_file(row["file"]): row["sha256"] for row in manifest["files"]}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for segment in rows:
        metadata = segment["metadata"]
        if metadata.get("display_projection_required"):
            raise ValueError("SDK_DISPLAY_PROJECTION_REQUIRED")
        file = relative_file(metadata["file"])
        if file not in expected:
            raise ValueError("SDK_PACK_SOURCE_NOT_BOUND")
        if translations[segment["id"]] != segment["source"]:
            groups[file].append({"kind": metadata["unity_kind"], "file": file, "location": metadata["location"], "source": segment["source"], "target": translations[segment["id"]]})
    output_root.mkdir(parents=True)
    files = []
    for file, replacements in sorted(groups.items()):
        source = (game_root / file).resolve()
        if not _inside(source, game_root) or source.is_symlink() or file_digest(source) != expected[file]:
            raise ValueError("SDK_PACK_SOURCE_CHANGED")
        original = source.read_bytes()
        rebuilt = original
        by_kind = defaultdict(list)
        for row in replacements:
            by_kind[row["kind"]].append(row)
        managed = by_kind.pop("managed_literal", []) + by_kind.pop("managed_resource", [])
        if managed:
            rebuilt = patch_assembly(rebuilt, managed)
        native = by_kind.pop("native_literal", [])
        if native:
            rebuilt = patch_native_literals(rebuilt, native)
        native_resources = by_kind.pop("native_resource", [])
        if native_resources:
            rebuilt = patch_native(rebuilt, native_resources)
        app_info = by_kind.pop("app_info", [])
        if app_info:
            rebuilt = patch_native(rebuilt, app_info, app_info=True)
        objects = by_kind.pop("serialized_string", []) + by_kind.pop("textasset", [])
        if objects:
            rebuilt = patch_serialized(rebuilt, objects, managed_types=request.get("managed_types"))
        if by_kind:
            raise ValueError("SDK_PACK_KIND_UNSUPPORTED")
        target = output_root / "payload" / file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(rebuilt)
        files.append({"file": file, "source_sha256": expected[file], "payload_sha256": hashlib.sha256(rebuilt).hexdigest(), "replacements": len(replacements)})
    report = {"schema": "parr0t.unity-payload.v1", "files": files, "statuses": statuses, "source_written": False, "runtime_verified": False}
    (output_root / "pack-report.json").write_text(encoded(report) + "\n", encoding="utf-8")
    return {"status": "payload_ready", "files": len(files), "replacements": sum(row["replacements"] for row in files), "source_written": False, "runtime_verified": False}
