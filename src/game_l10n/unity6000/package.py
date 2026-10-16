"""Deterministic sparse replacement deltas with verified forward and restore paths."""
from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

from .contracts import AdapterError, digest, fail, output_path, relative_path, sha, source_file


PACK_SCHEMA = "parr0t.unity6000.delta-pack.v1"
MAX_MEMBER = 512 * 1024 * 1024


def delta(before, after):
    prefix = 0
    while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(before) - prefix, len(after) - prefix) and before[-1 - suffix] == after[-1 - suffix]:
        suffix += 1
    return prefix, suffix, before[prefix:len(before) - suffix if suffix else len(before)], after[prefix:len(after) - suffix if suffix else len(after)]


def _write(archive, name, data):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    archive.writestr(info, data)


def pack(outputs, output, *, result_sha256, inventory_fingerprint, workspace, source_root):
    output = output_path(workspace, output, [source_root], Path(__file__).resolve().parents[3])
    manifest = {"schema": PACK_SCHEMA, "engine": "unity", "series": "6000.0", "backend": "mono", "result_sha256": result_sha256, "inventory_fingerprint": inventory_fingerprint, "runtime_executed": False, "files": []}
    paths = list(outputs)
    if not paths or len({relative_path(p).casefold() for p in paths}) != len(paths):
        fail("PACK_PATH_COLLISION")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x") as archive:
        for index, relative in enumerate(sorted(outputs)):
            before, after = outputs[relative]
            if before == after:
                fail("NO_EFFECT_PATCH")
            prefix, suffix, old, new = delta(before, after)
            _write(archive, f"delta/{index}.before", old)
            _write(archive, f"delta/{index}.after", new)
            manifest["files"].append({"path": relative, "before_sha256": sha(before), "after_sha256": sha(after), "before_size": len(before), "after_size": len(after), "prefix": prefix, "suffix": suffix, "delta": index})
        _write(archive, "manifest.json", json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode())
    return manifest


def verify(archive_path, root, *, result_sha256=None, inventory_fingerprint=None):
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            names = [relative_path(i.filename) for i in infos]
            if len(names) != len(set(n.casefold() for n in names)) or any(i.is_dir() or stat.S_ISLNK(i.external_attr >> 16) or i.file_size > MAX_MEMBER for i in infos):
                fail("ARCHIVE_MEMBERS_INVALID")
            if sum(i.file_size for i in infos) > 4 * MAX_MEMBER:
                fail("ARCHIVE_SIZE_UNSUPPORTED")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("schema") != PACK_SCHEMA or manifest.get("engine") != "unity" or manifest.get("series") != "6000.0" or manifest.get("backend") != "mono" or manifest.get("runtime_executed") is not False:
                fail("PACK_IDENTITY_INVALID")
            if result_sha256 is not None and manifest.get("result_sha256") != result_sha256:
                fail("PACK_RESULT_STALE")
            if inventory_fingerprint is not None and manifest.get("inventory_fingerprint") != inventory_fingerprint:
                fail("PACK_INVENTORY_STALE")
            files = manifest.get("files")
            if not isinstance(files, list) or not files:
                fail("PACK_FILES_INVALID")
            paths = [relative_path(f["path"]) for f in files]
            if len(paths) != len(set(p.casefold() for p in paths)) or paths != sorted(paths):
                fail("PACK_PATH_COLLISION")
            expected = {"manifest.json"}
            for index, entry in enumerate(files):
                if entry["delta"] != index or any(type(entry.get(k)) is not int or entry[k] < 0 for k in ("prefix", "suffix", "before_size", "after_size")):
                    fail("DELTA_METADATA_INVALID")
                prefix, suffix = entry["prefix"], entry["suffix"]
                if prefix + suffix > min(entry["before_size"], entry["after_size"]):
                    fail("DELTA_BOUNDS_INVALID")
                old_name, new_name = f"delta/{index}.before", f"delta/{index}.after"
                expected.update((old_name, new_name))
                old, new = archive.read(old_name), archive.read(new_name)
                before = source_file(root, entry["path"]).read_bytes()
                if len(before) != entry["before_size"] or sha(before) != entry["before_sha256"] or before[prefix:len(before) - suffix if suffix else len(before)] != old:
                    fail("DELTA_SOURCE_STALE")
                after = before[:prefix] + new + (before[-suffix:] if suffix else b"")
                restored = after[:prefix] + old + (after[-suffix:] if suffix else b"")
                if len(after) != entry["after_size"] or sha(after) != entry["after_sha256"] or restored != before:
                    fail("DELTA_ROUNDTRIP_INVALID")
            if set(names) != expected:
                fail("ARCHIVE_UNOWNED_MEMBER")
    except AdapterError:
        raise
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        fail("ARCHIVE_INVALID")
    return {"status": "verified", "files": len(files), "restore": "verified", "fingerprint": digest({"archive_sha256": sha(archive_path.read_bytes()), "manifest": manifest})}
