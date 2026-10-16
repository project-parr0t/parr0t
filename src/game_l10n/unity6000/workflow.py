from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from . import assets, managed
from .contracts import categorical, digest, fail, output_path, read_json, sha, source_file, validate_text, write_json
from .discovery import validate_inventory


@categorical
def selected_records(inventory, selection, *, require_resolved=True):
    if not isinstance(selection, dict) or selection.get("schema") != "parr0t.unity6000.selection.v1" or selection.get("inventory_fingerprint") != inventory.get("fingerprint"):
        fail("SELECTION_STALE")
    records = {r["id"]: r for r in inventory["records"]}
    entries = selection.get("entries")
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        fail("SELECTION_INVALID")
    ids = [e.get("id") for e in entries]
    if len(set(ids)) != len(ids) or set(ids) != set(records):
        fail("SELECTION_CENSUS_INCOMPLETE")
    allowed = {"confirmed_text", "nontext", "unresolved"}
    if any(e.get("classification") not in allowed for e in entries):
        fail("SELECTION_CLASSIFICATION_INVALID")
    if require_resolved and any(e["classification"] == "unresolved" for e in entries):
        fail("CANDIDATES_UNRESOLVED")
    selected = {e["id"] for e in entries if e["classification"] == "confirmed_text"}
    return [r for r in inventory["records"] if r["id"] in selected]


def read_results(path):
    try:
        if path.suffix.casefold() == ".jsonl":
            import json
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        else:
            rows = read_json(path).get("results")
    except (OSError, ValueError, AttributeError):
        fail("RESULTS_INVALID")
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        fail("RESULTS_INVALID")
    return rows


@categorical
def payload(root, inventory, selection, result_path, schemas, overlays=()):
    validate_inventory(inventory, root, schemas)
    selected = selected_records(inventory, selection)
    records = {r["id"]: r for r in selected}
    results = read_results(result_path)
    ids = [r.get("segment_id", r.get("id")) for r in results]
    if any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or set(ids) != set(records):
        fail("RESULT_TARGET_SET_MISMATCH")
    grouped = defaultdict(list)
    for result, identifier in zip(results, ids):
        record = records[identifier]
        if result.get("source") != record["source"]:
            fail("RESULT_SOURCE_MISMATCH")
        if result.get("status") not in {"reviewed", "translated", "source_preserved", "accepted_unreviewed"}:
            fail("RESULT_STATUS_INVALID")
        target = result.get("text")
        validate_text(record["source"], target)
        if result["status"] == "source_preserved" and target != record["source"]:
            fail("PRESERVED_RESULT_CHANGED")
        if target != record["source"]:
            grouped[record["file"]].append({**record, "target": target})
    overlay_map = {}
    for overlay in overlays:
        if not isinstance(overlay, dict) or overlay.get("file") not in inventory["files"] or overlay["file"] in overlay_map:
            fail("FONT_FILE_INVALID")
        overlay_map[overlay["file"]] = overlay
    outputs = {}
    for relative in sorted(set(grouped) | set(overlay_map)):
        before = source_file(root, relative).read_bytes()
        changes = grouped[relative]
        kinds = {c["kind"] for c in changes}
        if "managed_literal" in kinds:
            if kinds != {"managed_literal"} or relative in overlay_map:
                fail("MIXED_FILE_KIND")
            after = managed.patch(before, changes)
        else:
            after = assets.patch(before, relative, changes, schemas, overlay_map.get(relative))
        if before == after:
            fail("NO_EFFECT_PATCH")
        outputs[relative] = (before, after)
    if not outputs:
        fail("EMPTY_PATCH")
    # Recheck bound source bytes after all operations; the adapter never opens them writable.
    validate_inventory(inventory, root, schemas)
    return outputs


def prepare(root, inventory, selection, results, schemas, overlays, output, *, workspace):
    output = output_path(workspace, output, [root], Path(__file__).resolve().parents[3])
    outputs = payload(root, inventory, selection, results, schemas, overlays)
    if output.exists():
        fail("OUTPUT_COLLISION")
    output.mkdir(parents=True)
    files = []
    for index, (relative, (before, after)) in enumerate(outputs.items()):
        member = f"payload/{index}.bin"
        path = output / member
        path.parent.mkdir(exist_ok=True)
        with path.open("xb") as handle:
            handle.write(after)
        files.append({"path": relative, "payload": member, "before_sha256": sha(before), "after_sha256": sha(after)})
    receipt = {"schema": "parr0t.unity6000.prepared.v1", "inventory": inventory, "selection": selection, "overlays": overlays, "result_sha256": sha(results.read_bytes()), "schema_cache": schemas.fingerprint, "files": files}
    write_json(output / "prepared.json", receipt)
    return receipt


@categorical
def verify_prepared(root, prepared, results, schemas):
    receipt = read_json(prepared / "prepared.json")
    if not isinstance(receipt, dict) or receipt.get("schema") != "parr0t.unity6000.prepared.v1" or receipt.get("result_sha256") != sha(results.read_bytes()) or receipt.get("schema_cache") != schemas.fingerprint:
        fail("PREPARED_BINDING_STALE")
    outputs = payload(root, receipt["inventory"], receipt["selection"], results, schemas, receipt["overlays"])
    expected = []
    for index, (relative, (before, after)) in enumerate(outputs.items()):
        member = f"payload/{index}.bin"
        expected.append({"path": relative, "payload": member, "before_sha256": sha(before), "after_sha256": sha(after)})
        if source_file(prepared, member).read_bytes() != after:
            fail("PREPARED_PAYLOAD_CHANGED")
    if receipt.get("files") != expected:
        fail("PREPARED_FILES_CHANGED")
    return receipt, outputs
