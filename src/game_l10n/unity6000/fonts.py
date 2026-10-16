"""Add caller-prepared dynamic TMP fallback objects; no glyph/atlas generation."""
from __future__ import annotations

import copy
import io
import struct

from .contracts import categorical, fail, sha


ROLES = {"native": "Font", "tmp": "MonoBehaviour", "material": "Material", "atlas": "Texture2D"}


def ptr(path_id):
    return {"m_FileID": 0, "m_PathID": path_id}


def f32(value):
    if isinstance(value, float):
        return struct.unpack("<f", struct.pack("<f", value))[0]
    if isinstance(value, dict):
        return {k: f32(v) for k, v in value.items()}
    if isinstance(value, list):
        return [f32(v) for v in value]
    if isinstance(value, tuple):
        return tuple(f32(v) for v in value)
    return value


def pointers(value):
    if isinstance(value, dict):
        if set(value) == {"m_FileID", "m_PathID"}:
            yield value
        else:
            for child in value.values():
                yield from pointers(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from pointers(child)


def conform(template, value):
    """Restore tuple map-pairs and float width after JSON transport."""
    if isinstance(template, dict):
        if not isinstance(value, dict) or set(template) != set(value):
            fail("FONT_TEMPLATE_SCHEMA_CHANGED")
        return {key: conform(child, value[key]) for key, child in template.items()}
    if isinstance(template, (list, tuple)):
        if not isinstance(value, (list, tuple)):
            fail("FONT_TEMPLATE_SCHEMA_CHANGED")
        if isinstance(template, tuple):
            if len(template) != len(value):
                fail("FONT_TEMPLATE_SCHEMA_CHANGED")
            return tuple(conform(t, v) for t, v in zip(template, value))
        return [conform(template[0], v) for v in value] if template else f32(list(value))
    if isinstance(template, str) and not isinstance(value, str):
        fail("FONT_TEMPLATE_SCHEMA_CHANGED")
    if type(template) is int and type(value) is not int:
        fail("FONT_TEMPLATE_SCHEMA_CHANGED")
    return f32(value)


@categorical
def validate_overlay(spec, original, classes):
    if spec.get("schema") != "parr0t.unity6000.tmp-overlay.v1":
        fail("FONT_SPEC_INVALID")
    ids, templates, trees = spec.get("ids", {}), spec.get("templates", {}), spec.get("trees", {})
    if set(ids) != set(ROLES) or set(templates) != set(ROLES) or set(trees) != set(ROLES):
        fail("FONT_ROLES_INVALID")
    if any(type(i) is not int or i <= 0 for i in ids.values()) or len(set(ids.values())) != 4 or set(ids.values()) & set(original):
        fail("FONT_OBJECT_COLLISION")
    if len(set(templates.values())) != 4 or any(i not in original or classes[i] != ROLES[role] for role, i in templates.items()):
        fail("FONT_TEMPLATE_INVALID")
    settings_id = spec.get("settings_id")
    if settings_id not in original or classes[settings_id] != "MonoBehaviour" or not isinstance(original[settings_id].get("m_fallbackFontAssets"), list):
        fail("FONT_SETTINGS_INVALID")
    for role in ROLES:
        # Caller supplies a complete same-schema clone; unknown fields are not serialized silently.
        if set(trees[role]) != set(original[templates[role]]):
            fail("FONT_TEMPLATE_SCHEMA_CHANGED")
    tmp = trees["tmp"]
    if tmp.get("m_Script") != original[templates["tmp"]].get("m_Script") or trees["material"].get("m_Shader") != original[templates["material"]].get("m_Shader"):
        fail("FONT_TEMPLATE_IDENTITY_CHANGED")
    for tree in trees.values():
        for key in ("m_StreamData", "m_StreamingInfo"):
            streaming = tree.get(key, {})
            if streaming and (streaming.get("path") or streaming.get("size") or streaming.get("offset")):
                fail("FONT_STREAMED_DATA_UNSUPPORTED")
    if tmp.get("m_SourceFontFile") != ptr(ids["native"]) or tmp.get("m_Material") != ptr(ids["material"]) or tmp.get("m_AtlasTextures") != [ptr(ids["atlas"])]:
        fail("FONT_REFERENCE_INVALID")
    if tmp.get("m_AtlasPopulationMode") != 1 or tmp.get("m_GlyphTable") != [] or tmp.get("m_CharacterTable") != []:
        fail("FONT_DYNAMIC_TEMPLATE_REQUIRED")
    texture_refs = [value.get("m_Texture") for name, value in trees["material"].get("m_SavedProperties", {}).get("m_TexEnvs", []) if name == "_MainTex"]
    if texture_refs != [ptr(ids["atlas"])]:
        fail("FONT_MATERIAL_REFERENCE_INVALID")
    # Preserve all external references from each template; new external dependencies are forbidden.
    for role, tree in trees.items():
        external = {(p["m_FileID"], p["m_PathID"]) for p in pointers(original[templates[role]]) if p["m_FileID"]}
        for p in pointers(tree):
            if type(p["m_FileID"]) is not int or type(p["m_PathID"]) is not int:
                fail("FONT_REFERENCE_INVALID")
            if p["m_FileID"]:
                if (p["m_FileID"], p["m_PathID"]) not in external:
                    fail("FONT_EXTERNAL_REFERENCE_CHANGED")
            elif p["m_PathID"] and p["m_PathID"] not in set(original) | set(ids.values()):
                fail("FONT_DANGLING_REFERENCE")
    try:
        font_bytes = bytes(trees["native"]["m_FontData"])
    except (KeyError, TypeError, ValueError):
        fail("FONT_DATA_INVALID")
    if sha(font_bytes) != spec.get("font_sha256"):
        fail("FONT_DATA_STALE")
    from fontTools.ttLib import TTFont
    try:
        font = TTFont(io.BytesIO(font_bytes))
        cmap = font.getBestCmap()
        required = spec["required_codepoints"]
        if not required or any(type(cp) is not int or not 0 <= cp <= 0x10FFFF or cp not in cmap for cp in required):
            fail("FONT_GLYPH_COVERAGE_INVALID")
    except (KeyError, TypeError):
        fail("FONT_GLYPH_COVERAGE_INVALID")
    except Exception as exc:
        from .contracts import AdapterError
        if isinstance(exc, AdapterError):
            raise
        fail("FONT_DATA_INVALID")
    return {role: conform(original[templates[role]], tree) for role, tree in trees.items()}


@categorical
def add_overlay(env, spec, schemas):
    from .assets import tree_of, key_of
    serialized = spec.get("serialized_file")
    objects = {o.path_id: o for o in env.objects if o.assets_file.name == serialized}
    needed = set(spec.get("templates", {}).values()) | {spec.get("settings_id")}
    if not objects or not needed <= set(objects):
        fail("FONT_TEMPLATE_MISSING")
    original = {}
    nodes = {}
    for pid in needed:
        original[pid], nodes[pid] = tree_of(objects[pid], schemas)
    # Include all IDs for collision and local reference closure; only template trees are read.
    original.update({pid: {} for pid in objects if pid not in original})
    trees = validate_overlay(spec, original, {pid: o.type.name for pid, o in objects.items()})
    expected, result_nodes = {}, {}
    sf = next(iter(objects.values())).assets_file
    for role, pid in spec["ids"].items():
        template = spec["templates"][role]
        obj = copy.copy(objects[template])
        obj.path_id = pid
        obj.save_typetree(trees[role], nodes=nodes[template])
        sf.objects[pid] = obj
        expected[key_of(obj)] = trees[role]
        result_nodes[key_of(obj)] = nodes[template]
    settings_id = spec["settings_id"]
    settings = copy.deepcopy(original[settings_id])
    settings["m_fallbackFontAssets"].append(ptr(spec["ids"]["tmp"]))
    obj = objects[settings_id]
    obj.save_typetree(settings, nodes=nodes[settings_id])
    expected[key_of(obj)] = settings
    result_nodes[key_of(obj)] = nodes[settings_id]
    return expected, result_nodes
