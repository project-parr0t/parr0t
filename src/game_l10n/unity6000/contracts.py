from __future__ import annotations

import hashlib
import json
import re
from functools import wraps
from collections import Counter
from pathlib import Path, PurePosixPath


class AdapterError(ValueError):
    """Categorical failures are safe for control-plane output."""


def fail(code):
    raise AdapterError("U6000_" + code)


def categorical(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except AdapterError:
            raise
        except (KeyError, TypeError, AttributeError, IndexError, OverflowError):
            fail("SPEC_INVALID")
    return checked


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(value):
    return sha(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode())


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        fail("JSON_INVALID")


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=True, sort_keys=True, indent=2)
        handle.write("\n")


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        fail("PATH_INVALID")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in {"", ".", ".."} for x in value.split("/")):
        fail("PATH_INVALID")
    # Reject Windows aliases even when verification runs on another platform.
    for part in p.parts:
        if any(c in part for c in '*?"<>|') or part.endswith((".", " ")) or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            fail("PATH_INVALID")
    return value


def source_file(root, relative):
    relative_path(relative)
    p = (root / relative).resolve()
    if not p.is_relative_to(root.resolve()) or not p.is_file():
        fail("SOURCE_PATH_INVALID")
    return p


def output_path(workspace, value, sources=(), repository=None):
    p = Path(value)
    p = (workspace / p).resolve() if not p.is_absolute() else p.resolve()
    if not p.is_relative_to(workspace.resolve()):
        fail("OUTPUT_OUTSIDE_WORKSPACE")
    if any(p.is_relative_to(s.resolve()) or s.resolve().is_relative_to(p) for s in sources):
        fail("OUTPUT_SOURCE_OVERLAP")
    if repository and p.is_relative_to(repository.resolve()):
        fail("OUTPUT_IN_REPOSITORY")
    if p.exists():
        fail("OUTPUT_COLLISION")
    return p


PROTECTED = {"m_Name", "m_AssemblyName", "m_ClassName", "m_Namespace", "m_SourceFontFileGUID", "m_EditorClassIdentifier", "m_MethodName", "m_TargetAssemblyTypeName", "m_TypeName", "m_ObjectArgumentAssemblyTypeName", "m_ShaderKeywords", "m_ValidKeywords", "m_InvalidKeywords"}
ORDERED_TOKENS = re.compile(r"<[^>\n]*>|\\[A-Za-z]+(?:\[[^\]]*\])?|\r\n|\r|\n|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def validate_text(source, target):
    if not isinstance(source, str) or not isinstance(target, str):
        fail("TEXT_TYPE_INVALID")
    from ..validators import protected_tokens
    if source.strip() and not target.strip():
        fail("EMPTY_REPLACEMENT")
    extra = [r"\{[^{}\r\n]*\}", r"\{\{|\}\}"]
    if protected_tokens(source, extra) != protected_tokens(target, extra) or ORDERED_TOKENS.findall(source) != ORDERED_TOKENS.findall(target):
        fail("PROTECTED_TOKEN_CHANGED")


def leaves(value, path=()):
    if isinstance(value, str):
        yield list(path), value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from leaves(child, (*path, key))
    elif isinstance(value, (list, tuple)):
        for key, child in enumerate(value):
            yield from leaves(child, (*path, key))


def get_leaf(tree, path):
    if not isinstance(path, list) or not path or any(type(k) not in (str, int) or (type(k) is int and k < 0) for k in path):
        fail("FIELD_PATH_INVALID")
    try:
        for key in path:
            tree = tree[key]
        return tree
    except (KeyError, IndexError, TypeError):
        fail("FIELD_PATH_INVALID")


def set_leaf(tree, path, value):
    get_leaf(tree, path)
    owner = tree
    for key in path[:-1]:
        owner = owner[key]
    owner[path[-1]] = value


def target_id(location):
    return "u6000:" + digest(location)
