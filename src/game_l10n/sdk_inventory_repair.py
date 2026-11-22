"""Deterministic SDK pass before preparing an inventory exception relay."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from .coverage import _atomic_json, _inventory_document, save_inventory_attestation
from .errors import ConfigurationError
from .inventory_constraints import artifact_digest
from .inventory_scope import _BasisSnapshot
from .store import stable_hash

_TOOL = "reconcile_inventory.py"
_ATTESTATION = ".game-l10n/inventory-attestation.json"
_EMPTY_CACHE = ".game-l10n/sdk-inventory-repair-empty.json"
_REPAIR_PROTOCOL = "parr0t.sdk-inventory-projection-repair.v1"


def inventory_repair_tool(work_dir: Path, activation: dict) -> tuple[Path, str] | None:
    """Select a hook only from the current activated SDK implementation."""
    workspace_tools = activation.get("workspace_tools")
    if workspace_tools is not None:
        for entry in workspace_tools["manifest"]:
            if entry["path"] == _TOOL:
                return work_dir / "tools" / _TOOL, entry["sha256"]
        return None
    runtime_root = (activation.get("runtime") or {}).get("path")
    if isinstance(runtime_root, str):
        root = Path(runtime_root)
        tool = root / "tools" / _TOOL
        if root.is_absolute() and tool.is_file() and tool.resolve() == tool:
            return tool, artifact_digest(tool)
    return None


def _positive_links(rows: list[dict], pending: set[str]) -> dict[str, dict[str, tuple[str, str]]]:
    links: dict[str, dict[str, tuple[str, str]]] = {}
    seen: set[str] = set()
    ambiguous: set[str] = set()
    for row in rows:
        metadata = row.get("metadata", {})
        binding = metadata.get("display_binding_id")
        if not isinstance(binding, str):
            continue
        if binding in seen:
            ambiguous.add(binding)
        seen.add(binding)
        original = metadata.get("display_projection_of")
        evidence = metadata.get("display_scope_evidence", {})
        key = metadata.get("inventory_comparison_key")
        if (isinstance(original, str) and original in pending and isinstance(key, str)
                and evidence.get("kind") == "positive_display_binding"):
            links.setdefault(original, {})[binding] = key, evidence["source_sha256"]
    # Any duplicate active binding invalidates that occurrence as a whole.
    return {key: bindings for key, bindings in links.items() if ambiguous.isdisjoint(bindings)}


def reconcile_cached_sdk_inventory(work_dir: Path, tool: tuple[Path, str]) -> dict | None:
    """Publish a new pair only for projections independently evidenced on both sides."""
    path, digest = tool
    try:
        state_path = work_dir / _ATTESTATION
        if state_path.parent.resolve() != state_path.parent:
            raise ValueError("redirected reconciliation state")
        if not state_path.is_file():
            return None
        state = json.loads(state_path.read_bytes())
        if ("INVENTORY_SCOPE_PROJECTION_REQUIRED" not in state.get("issue_codes", ())
                or "INVENTORY_SCOPE_CORRECTION_MISMATCH" in state["issue_codes"]):
            return None
        inputs = state.get("constraint_inputs")
        if not isinstance(inputs, list) or not 2 <= len(inputs) <= 4:
            return None
        paths = [Path(state[f"{role}_path"]).resolve(strict=True) for role in ("primary", "independent")]
        if paths[0] == paths[1] or not all(p.is_relative_to(work_dir) for p in paths):
            raise ValueError("invalid inventory pair")
        digests = {}
        for entry in inputs:
            input_path = Path(entry["path"]).resolve(strict=True)
            if not input_path.is_relative_to(work_dir):
                raise ValueError("unbound inventory evidence")
            if input_path not in digests:
                digests[input_path] = artifact_digest(input_path)
            if entry["sha256"] != digests[input_path]:
                raise ValueError("stale inventory evidence")
        if not all(p in digests for p in paths):
            raise ValueError("unbound inventory pair")
        binding = stable_hash({"schema": _REPAIR_PROTOCOL, "tool_sha256": digest, "inputs": inputs})
        empty_cache = work_dir / _EMPTY_CACHE
        if empty_cache.is_file() and json.loads(empty_cache.read_bytes()) == {"binding": binding}:
            return None

        if path.resolve(strict=True) != path or artifact_digest(path) != digest:
            raise ValueError("SDK tool changed after activation")
        basis: dict[Path, _BasisSnapshot] = {}
        documents = [_inventory_document(p, defer_projection_constraints=True, _basis_cache=basis) for p in paths]
        if any(doc.sha256 != digests[p] for p, doc in zip(paths, documents, strict=True)):
            raise ValueError("inventory input changed")
        corrections = [doc.payload["coverage"]["scope_correction"] for doc in documents]
        if corrections[0] != corrections[1]:
            raise ValueError("independent corrections disagree")
        pending = {record["comparison_key"] for record in corrections[0]["records"]
                   if record["classification"] == "control_storage_with_projection" and not record["projection_ids"]}
        primary, independent = [_positive_links(doc.active_rows, pending) for doc in documents]
        if not primary or not independent:
            _atomic_json(empty_cache, {"binding": binding})
            return None
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(path)], cwd=work_dir,
            input=json.dumps({"primary": primary, "independent": independent}).encode("utf-8"),
            capture_output=True, check=True,
        )
        repairs = json.loads(process.stdout)
        if not isinstance(repairs, dict):
            raise ValueError("invalid SDK result")
        for key, ids in repairs.items():
            expected = primary.get(key)
            if (not expected or expected != independent.get(key)
                    or not isinstance(ids, list) or ids != sorted(expected)):
                raise ValueError("SDK repair lacks exact independent display evidence")
        if not repairs:
            _atomic_json(empty_cache, {"binding": binding})
            return None

        # Publish a pair of new files, never overwrite historical inventory inputs.
        # The core attestation is the only promotion pointer, written after full pair verification.
        revision = uuid4().hex
        staged = [p.with_name(f"inventory-{role}-{revision}.json")
                  for p, role in zip(paths, ("primary", "independent"), strict=True)]
        for doc, correction, target in zip(documents, corrections, staged, strict=True):
            for record in correction["records"]:
                if record["comparison_key"] in repairs:
                    record["projection_ids"] = repairs[record["comparison_key"]]
            _atomic_json(target, doc.payload)
        result = save_inventory_attestation(work_dir, *staged)
        return {"resolved_projections": len(repairs), "gap_score": result["gap_score"],
                "static_complete": result["static_complete"]}
    except Exception:
        # SDK failures must never echo code, source text or private paths to the controller.
        raise ConfigurationError("E_SDK_INVENTORY_REPAIR: cached SDK reconciliation failed") from None
