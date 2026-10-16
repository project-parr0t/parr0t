from __future__ import annotations

import copy
from collections import defaultdict

from .contracts import PROTECTED, categorical, digest, fail, get_leaf, read_json, relative_path, set_leaf, sha, source_file, validate_text
from . import tables


CACHE_SCHEMA = "parr0t.unity6000.typetrees.v1"


class Schemas:
    """Explicit generated schemas, isolated by engine line and assembly byte binding."""

    @categorical
    def __init__(self, path=None, root=None):
        self.nodes = {}
        self.fingerprint = None
        self.root = root
        if path is None:
            return
        raw = read_json(path)
        if raw.get("schema") != CACHE_SCHEMA or raw.get("engine") != "unity" or raw.get("series") != "6000.0" or not isinstance(raw.get("generator"), str) or not raw["generator"]:
            fail("SCHEMA_CACHE_IDENTITY_INVALID")
        from .discovery import installation
        if raw.get("unity_version") not in installation(root)["observed_versions"]:
            fail("SCHEMA_CACHE_VERSION_STALE")
        if not isinstance(raw.get("assemblies"), dict) or not raw["assemblies"] or not isinstance(raw.get("nodes"), dict):
            fail("SCHEMA_CACHE_INVALID")
        for name, binding in raw["assemblies"].items():
            if not isinstance(binding, dict) or sha(source_file(root, binding["path"]).read_bytes()) != binding.get("sha256"):
                fail("SCHEMA_ASSEMBLY_STALE")
        from UnityPy.helpers.TypeTreeNode import TypeTreeNode
        for key, value in raw["nodes"].items():
            if not isinstance(key, str) or "|" not in key or key.split("|", 1)[0] not in raw["assemblies"]:
                fail("SCHEMA_CACHE_KEY_INVALID")
            try:
                node = TypeTreeNode.from_list(value)
            except Exception:
                fail("SCHEMA_NODE_INVALID")
            self.nodes[key] = node
        self.fingerprint = digest(raw)

    def for_object(self, obj):
        if obj.type.name != "MonoBehaviour":
            return None
        if getattr(obj.serialized_type, "node", None) is not None:
            return None
        try:
            head = obj.parse_monobehaviour_head()
            script = head.m_Script.deref_parse_as_object()
            full = (script.m_Namespace + "." if script.m_Namespace else "") + script.m_ClassName
            key = script.m_AssemblyName + "|" + full
        except Exception:
            fail("SCHEMA_SCRIPT_UNRESOLVED")
        if key not in self.nodes:
            fail("SCHEMA_NODE_REQUIRED")
        return self.nodes[key]


def load(data, name, root=None):
    import UnityPy
    try:
        env = UnityPy.Environment()
        env.load_file(data, name=name)
        def resolve_dependency(value, is_dependency=True):
            relative_path(value)
            existing = env.get_cab(value.casefold())
            if existing:
                return existing
            if root is None:
                fail("DEPENDENCY_ROOT_REQUIRED")
            from .discovery import asset_paths, installation
            matches = [p for p in asset_paths(root, installation(root)) if p.name.casefold() == value.casefold()]
            if len(matches) != 1:
                fail("DEPENDENCY_FILE_AMBIGUOUS")
            relative = matches[0].relative_to(root).as_posix()
            return env.load_file(source_file(root, relative).read_bytes(), name=value, is_dependency=True)
        # Disable UnityPy's cwd/filesystem fallback. Only bound source asset candidates resolve.
        env.find_file = resolve_dependency
        if not env.objects:
            fail("ASSET_OBJECTS_REQUIRED")
        return env
    except Exception as exc:
        from .contracts import AdapterError
        if isinstance(exc, AdapterError):
            raise
        fail("ASSET_PARSE_INVALID")


def tree_of(obj, schemas):
    node = schemas.for_object(obj)
    try:
        return obj.read_typetree(nodes=node), node
    except Exception:
        fail("TYPETREE_UNAVAILABLE")


def key_of(obj):
    return obj.assets_file.name, obj.path_id


@categorical
def apply_tree(tree, changes, kind):
    tree = copy.deepcopy(tree)
    cells = []
    seen = set()
    for change in changes:
        field = change["field"]
        key = tuple(field)
        if key in seen:
            fail("DUPLICATE_LOCATION")
        seen.add(key)
        if len(field) == 4 and field[:2] == ["m_Script", "cell"]:
            if kind != "TextAsset":
                fail("TABLE_TYPE_INVALID")
            cells.append(change)
            continue
        if "m_Script" in field and not (kind == "TextAsset" and field == ["m_Script"]):
            fail("PROTECTED_FIELD")
        if any(isinstance(part, str) and (part in PROTECTED or "guid" in part.casefold()) for part in field):
            fail("PROTECTED_FIELD")
        if get_leaf(tree, field) != change["source"]:
            fail("SOURCE_FIELD_MISMATCH")
        validate_text(change["source"], change["target"])
        set_leaf(tree, field, change["target"])
    if cells:
        if ("m_Script",) in seen:
            fail("OVERLAPPING_LOCATION")
        tree["m_Script"] = tables.rewrite(tree["m_Script"], cells)
    return tree


@categorical
def patch(data, name, changes, schemas, overlay=None):
    env = load(data, name, schemas.root)
    grouped = defaultdict(list)
    for change in changes:
        grouped[(change["serialized_file"], change["path_id"])].append(change)
    original_raw = {key_of(o): o.get_raw_data() for o in env.objects}
    original_trees = {}
    nodes = {}
    for obj in env.objects:
        key = key_of(obj)
        if key not in grouped:
            continue
        if obj.type.name not in {"MonoBehaviour", "TextAsset", "TextMesh"}:
            fail("OBJECT_TYPE_PROTECTED")
        tree, node = tree_of(obj, schemas)
        expected = apply_tree(tree, grouped.pop(key), obj.type.name)
        obj.save_typetree(expected, nodes=node)
        original_trees[key] = expected
        nodes[key] = node
    if grouped:
        fail("ASSET_OBJECT_MISSING")
    if overlay:
        from .fonts import add_overlay
        added_expected, overlay_nodes = add_overlay(env, overlay, schemas)
        original_trees.update(added_expected)
        nodes.update(overlay_nodes)
    if not original_trees:
        fail("EMPTY_PATCH")
    if name not in env.files:
        fail("ASSET_CONTAINER_UNSUPPORTED")
    result = env.files[name].save()
    rebuilt = load(result, name, schemas.root)
    actual = {key_of(o): o for o in rebuilt.objects}
    if set(actual) != set(original_raw) | set(original_trees):
        fail("ASSET_OBJECT_SET_CHANGED")
    for key, raw in original_raw.items():
        if key not in original_trees and actual[key].get_raw_data() != raw:
            fail("UNOWNED_OBJECT_CHANGED")
    for key, expected in original_trees.items():
        if actual[key].read_typetree(nodes=nodes[key]) != expected:
            fail("ASSET_ROUNDTRIP_INVALID")
    return result
