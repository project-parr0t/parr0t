from __future__ import annotations

import re
from pathlib import Path

from . import assets, managed, tables
from .contracts import PROTECTED, categorical, digest, fail, leaves, relative_path, sha, source_file, target_id


INVENTORY_SCHEMA = "parr0t.unity6000.inventory.v1"
VERSION = re.compile(rb"(?:6000|20\d\d)\.\d+\.\d+[abfp]\d+")


def installation(root):
    root = root.resolve()
    data = [p for p in root.iterdir() if p.is_dir() and p.name.casefold().endswith("_data")]
    if len(data) != 1:
        fail("DATA_ROOT_AMBIGUOUS")
    data = data[0]
    if (root / "GameAssembly.dll").exists() or (data / "il2cpp_data").exists():
        fail("BACKEND_UNSUPPORTED")
    if not (data / "Managed").is_dir() or not (data / "Managed" / "UnityEngine.CoreModule.dll").is_file():
        fail("MONO_LAYOUT_REQUIRED")
    versions = set()
    for name in ("globalgamemanagers", "data.unity3d"):
        path = data / name
        if path.is_file():
            with path.open("rb") as handle:
                versions.update(m.decode("ascii") for m in VERSION.findall(handle.read(8 * 1024 * 1024)))
    if not versions or any(not v.startswith("6000.0.") for v in versions):
        fail("ENGINE_VERSION_UNSUPPORTED")
    return {"engine": "unity", "series": "6000.0", "backend": "mono", "observed_versions": sorted(versions), "data_root": data.relative_to(root).as_posix()}


def asset_paths(root, identity):
    data = root / identity["data_root"]
    return sorted(p for p in data.rglob("*") if p.is_file() and "Managed" not in p.relative_to(data).parts and (p.suffix.casefold() in {".assets", ".bundle", ".unity3d"} or p.name.startswith("level") or p.name in {"globalgamemanagers", "unity_builtin_extra", "unity default resources"}))


def script_class(obj):
    if obj.type.name != "MonoBehaviour":
        return obj.type.name
    try:
        return obj.parse_monobehaviour_head().m_Script.deref_parse_as_object().m_ClassName
    except Exception:
        fail("SCRIPT_IDENTITY_UNAVAILABLE")


def census(objects):
    """Structural label/image census; never inspects texture or sprite content."""
    groups = {}
    for item in objects:
        groups.setdefault(item.get("serialized_file", ""), []).append(item)
    if len(groups) > 1:
        reports = [census(group) for group in groups.values()]
        return {"controls": [c for r in reports for c in r["controls"]], "unresolved_relationships": sum(r["unresolved_relationships"] for r in reports), "image_content_inspected": False, "image_only_controls_are_text_covered": False, "runtime_coverage": "unverified"}
    by_go = {}
    parent = {}
    transforms = {}
    controls = []
    unresolved = 0
    for item in objects:
        tree = item["tree"]
        go_ref = tree.get("m_GameObject", {})
        go = go_ref.get("m_PathID", 0) if not go_ref.get("m_FileID", 0) else 0
        if go_ref.get("m_FileID", 0):
            unresolved += 1
        if go:
            by_go.setdefault(go, []).append(item)
        if item["class"] in {"Transform", "RectTransform"}:
            transforms[item["path_id"]] = go
            father = tree.get("m_Father", {})
            if father.get("m_FileID", 0):
                unresolved += 1
            else:
                parent[go] = father.get("m_PathID", 0)
    unresolved += sum(1 for p in parent.values() if p and p not in transforms)
    for item in objects:
        if item["class"] not in {"Button", "Toggle", "Dropdown", "TMP_Dropdown", "InputField", "TMP_InputField"}:
            continue
        go_ref = item["tree"].get("m_GameObject", {})
        go = go_ref.get("m_PathID", 0) if not go_ref.get("m_FileID", 0) else 0
        descendants = {go}
        while True:
            added = {child for child, transform in parent.items() if transforms.get(transform) in descendants}
            if added <= descendants:
                break
            descendants |= added
        components = [component for child in descendants for component in by_go.get(child, [])]
        text = [c["path_id"] for c in components if c["class"] in {"Text", "TextMesh", "TextMeshPro", "TextMeshProUGUI"}]
        images = [c["path_id"] for c in components if c["class"] in {"Image", "RawImage", "SpriteRenderer"}]
        controls.append({"serialized_file": item.get("serialized_file", ""), "path_id": item["path_id"], "text_components": sorted(text), "image_components": sorted(images), "label_state": "serialized_text_candidate" if text else "no_serialized_text_component", "image_content": "unverified" if images else "no_image_component_observed", "runtime_coverage": "unverified"})
    return {"controls": controls, "unresolved_relationships": unresolved, "image_content_inspected": False, "image_only_controls_are_text_covered": False, "runtime_coverage": "unverified"}


def discover(root, schemas, managed_files=(), table_files=()):
    identity = installation(root)
    files, records, ui = {}, [], []
    table_files = set(table_files)
    for value in table_files:
        relative_path(value)
    for path in asset_paths(root, identity):
        relative = path.relative_to(root).as_posix()
        blob = source_file(root, relative).read_bytes()
        files[relative] = sha(blob)
        env = assets.load(blob, relative, root)
        structural = []
        for obj in env.objects:
            # These asset classes hold binary image/audio data, never localization text.
            if obj.type.name in {"Texture2D", "Sprite", "AudioClip", "Mesh", "Font", "VideoClip"}:
                continue
            version = str(obj.assets_file.unity_version)
            if not version.startswith("6000.0."):
                fail("SERIALIZED_VERSION_UNSUPPORTED")
            tree, _ = assets.tree_of(obj, schemas)
            cls = script_class(obj)
            structural.append({"serialized_file": obj.assets_file.name, "path_id": obj.path_id, "class": cls, "tree": tree})
            if obj.type.name not in {"MonoBehaviour", "TextAsset", "TextMesh"}:
                continue
            for field, value in leaves(tree):
                protected = any(isinstance(part, str) and (part in PROTECTED or "guid" in part.casefold()) for part in field)
                if protected or ("m_Script" in field and obj.type.name != "TextAsset"):
                    continue
                if obj.type.name == "TextAsset" and field == ["m_Script"] and relative in table_files:
                    for row, (cells, _, _) in enumerate(tables.rows(value)):
                        for column, source in enumerate(cells):
                            location = {"file": relative, "serialized_file": obj.assets_file.name, "path_id": obj.path_id, "field": ["m_Script", "cell", row, column], "kind": "serialized_text"}
                            records.append({"id": target_id(location), **location, "source": source})
                    continue
                location = {"file": relative, "serialized_file": obj.assets_file.name, "path_id": obj.path_id, "field": field, "kind": "serialized_text"}
                records.append({"id": target_id(location), **location, "source": value})
        ui.append({"file": relative, **census(structural)})
    for relative in sorted(set(managed_files)):
        blob = source_file(root, relative).read_bytes()
        files[relative] = sha(blob)
        for record in managed.discover(blob):
            location = {"file": relative, **{k: v for k, v in record.items() if k != "source"}}
            records.append({"id": target_id(location), **location, "source": record["source"]})
    result = {"schema": INVENTORY_SCHEMA, "identity": identity, "files": files, "schema_cache": schemas.fingerprint, "records": records, "ui_census": ui, "managed_selection": "explicit_caller_list", "coverage": "discovery_candidates_require_independent_reconciliation"}
    result["fingerprint"] = digest(result)
    return result


@categorical
def validate_inventory(inventory, root, schemas):
    if inventory.get("schema") != INVENTORY_SCHEMA or inventory.get("fingerprint") != digest({k: v for k, v in inventory.items() if k != "fingerprint"}):
        fail("INVENTORY_INVALID")
    if installation(root) != inventory.get("identity") or inventory.get("schema_cache") != schemas.fingerprint:
        fail("INVENTORY_IDENTITY_STALE")
    if not inventory.get("files"):
        fail("INVENTORY_EMPTY")
    for relative, expected in inventory["files"].items():
        if sha(source_file(root, relative).read_bytes()) != expected:
            fail("SOURCE_STALE")
    seen = set()
    for record in inventory.get("records", []):
        location = {k: v for k, v in record.items() if k not in {"id", "source"}}
        if record["id"] in seen or record["id"] != target_id(location) or record["file"] not in inventory["files"]:
            fail("INVENTORY_TARGET_INVALID")
        seen.add(record["id"])
