from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from .errors import ConfigurationError
from .store import stable_hash


SCOPE_CORRECTION_SCHEMA = "parr0t.inventory-scope-correction.v1"
_SOURCE_INVENTORY_SCHEMA = "parr0t.source-inventory.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CONTROL_ROLES = {"shared_control_value", "lookup_key", "index", "script_operand"}


def _invalid() -> ConfigurationError:
    return ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_INVALID: invalid bound scope evidence")


def _rows_by_key(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise _invalid()
    result: dict[str, dict[str, Any]] = {}
    ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise _invalid()
        target_id, source = row.get("id"), row.get("source")
        metadata = row.get("metadata", {})
        if (not isinstance(target_id, str) or not target_id or target_id in ids
                or not isinstance(source, str) or not isinstance(metadata, dict)):
            raise _invalid()
        key = metadata.get("inventory_comparison_key", target_id)
        if not isinstance(key, str) or not key or key in result:
            raise _invalid()
        ids.add(target_id)
        result[key] = row
    return result


def inventory_scope(
    payload: dict[str, Any], path: Path | None = None,
    *, defer_projection_constraints: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Separate active display targets from retained canonical occurrence history.

    Evidence truth remains the SDK's responsibility. Core verifies exact source,
    occurrence, independent-method, historical basis and projection bindings.
    A path is mandatory at file-consumption boundaries, but schema-only callers
    may validate an already loaded document without reopening its basis snapshot.
    """
    coverage = payload.get("coverage", {})
    correction = coverage.get("scope_correction") if isinstance(coverage, dict) else None
    rows = payload.get("segments", [])
    if correction is None:
        return rows, None
    if (payload.get("schema") != _SOURCE_INVENTORY_SCHEMA
            or not isinstance(correction, dict)
            or set(correction) != {"schema", "basis", "records"}
            or correction.get("schema") != SCOPE_CORRECTION_SCHEMA):
        raise _invalid()
    basis = correction.get("basis")
    records = correction.get("records")
    if (not isinstance(basis, dict) or set(basis) != {"file", "sha256"}
            or not isinstance(basis.get("file"), str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.json", basis["file"]) is None
            or not isinstance(basis.get("sha256"), str)
            or _SHA256.fullmatch(basis["sha256"]) is None
            or not isinstance(records, list)):
        raise _invalid()
    by_key = _rows_by_key(rows)
    current_ids = {row["id"]: row for row in rows}
    retired: dict[str, dict[str, Any]] = {}
    canonical_ids: set[str] = set()
    method_bindings: set[tuple[str, str]] | None = None
    for record in records:
        if (not isinstance(record, dict) or set(record) != {
                "canonical_id", "comparison_key", "source_sha256", "classification",
                "projection_ids", "evidence"}):
            raise _invalid()
        key, canonical_id = record["comparison_key"], record["canonical_id"]
        source_digest = record["source_sha256"]
        projections, proofs = record["projection_ids"], record["evidence"]
        if (not isinstance(key, str) or not key or key in retired
                or not isinstance(canonical_id, str) or not canonical_id or canonical_id in canonical_ids
                or not isinstance(source_digest, str) or _SHA256.fullmatch(source_digest) is None
                or not isinstance(record["classification"], str)
                or record["classification"] not in {"proven_non_display", "control_storage_with_projection"}
                or not isinstance(projections, list)
                or not all(isinstance(item, str) and item for item in projections)
                or len(set(projections)) != len(projections)
                or not isinstance(proofs, list) or len(proofs) != 2):
            raise _invalid()
        methods: set[str] = set()
        implementations: set[str] = set()
        digests: set[str] = set()
        bindings: set[tuple[str, str]] = set()
        for proof in proofs:
            if not isinstance(proof, dict) or set(proof) != {
                    "method", "implementation_fingerprint", "evidence_sha256"}:
                raise _invalid()
            method, implementation, digest = (
                proof["method"], proof["implementation_fingerprint"], proof["evidence_sha256"]
            )
            if (not isinstance(method, str) or not method.strip()
                    or not isinstance(implementation, str) or not implementation.strip()
                    or not isinstance(digest, str) or _SHA256.fullmatch(digest) is None):
                raise _invalid()
            methods.add(method.strip().casefold())
            implementations.add(implementation)
            digests.add(digest)
            bindings.add((method, implementation))
        if len(methods) != 2 or len(implementations) != 2 or len(digests) != 2:
            raise _invalid()
        if method_bindings is not None and method_bindings != bindings:
            raise _invalid()
        method_bindings = bindings
        if (payload.get("method"), payload.get("implementation_fingerprint")) not in bindings:
            raise _invalid()
        row = by_key.get(key)
        if row is not None and sha256(row["source"].encode("utf-8")).hexdigest() != source_digest:
            raise _invalid()
        if record["classification"] == "proven_non_display":
            if projections:
                raise _invalid()
        retired[key] = record
        canonical_ids.add(canonical_id)

    basis_fingerprint = None
    if path is not None:
        basis_path = path.parent / basis["file"]
        try:
            resolved_parent = path.parent.resolve()
            if (basis_path.resolve().parent != resolved_parent
                    or basis_path.resolve() == path.resolve()):
                raise _invalid()
            raw = basis_path.read_bytes()
            if sha256(raw).hexdigest() != basis["sha256"]:
                raise _invalid()
            original = json.loads(raw.decode("utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise _invalid() from exc
        if (not isinstance(original, dict) or original.get("schema") != _SOURCE_INVENTORY_SCHEMA
                or not isinstance(original.get("method"), str) or not original["method"].strip()
                or not isinstance(original.get("implementation_fingerprint"), str)
                or not original["implementation_fingerprint"].strip()
                or not isinstance(original.get("coverage"), dict)):
            raise _invalid()
        original_rows = _rows_by_key(original.get("segments"))
        basis_fingerprint = stable_hash({key: original[key] for key in (
            "schema", "method", "implementation_fingerprint", "coverage", "segments"
        )})
        if records and (original.get("method"), original.get("implementation_fingerprint")) not in method_bindings:
            raise _invalid()
        canonical_document = (
            payload.get("method") == original.get("method")
            and payload.get("implementation_fingerprint") == original.get("implementation_fingerprint")
        )
        if canonical_document:
            positions = {row["id"]: index for index, row in enumerate(rows)}
            previous_positions: list[int] = []
            for key, old in original_rows.items():
                current = current_ids.get(old["id"])
                if current is None or current["source"] != old["source"] or by_key.get(key) is not current:
                    raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_LINEAGE_CONFLICT")
                previous_positions.append(positions[old["id"]])
            if previous_positions != sorted(previous_positions):
                raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_LINEAGE_CONFLICT")
        for key, record in retired.items():
            old = original_rows.get(key)
            if (old is None or old["id"] != record["canonical_id"]
                    or sha256(old["source"].encode("utf-8")).hexdigest() != record["source_sha256"]):
                raise _invalid()

    active = [row for key, row in by_key.items() if key not in retired]
    # Projection identifiers are canonical display-binding IDs, not method-local
    # target IDs, so independently extracted rows can prove the same projection.
    active_bindings: dict[str, list[dict[str, Any]]] = {}
    for row in active:
        binding = row.get("metadata", {}).get("display_binding_id")
        if isinstance(binding, str):
            active_bindings.setdefault(binding, []).append(row)
    projection_constraints = 0
    for record in retired.values():
        if record["classification"] != "control_storage_with_projection":
            continue
        if not record["projection_ids"]:
            projection_constraints += 1
            continue
        for projection_id in record["projection_ids"]:
            candidates = active_bindings.get(projection_id, [])
            projection = candidates[0] if len(candidates) == 1 else None
            metadata = projection.get("metadata", {}) if projection else {}
            if projection is not None:
                evidence = metadata.get("display_scope_evidence")
                if (not isinstance(evidence, dict)
                        or not isinstance(evidence.get("evidence_fingerprint"), str)
                        or _SHA256.fullmatch(evidence["evidence_fingerprint"]) is None
                        or evidence.get("source_sha256") != sha256(projection["source"].encode("utf-8")).hexdigest()):
                    raise _invalid()
            if (projection is None or metadata.get("storage_role") in _CONTROL_ROLES
                    or metadata.get("display_projection_of") != record["comparison_key"]
                    or metadata.get("display_scope_evidence", {}).get("kind") != "positive_display_binding"):
                projection_constraints += 1
    if projection_constraints and not defer_projection_constraints:
        raise ConfigurationError("E_INVENTORY_SCOPE_PROJECTION_REQUIRED")
    return active, {
        "schema": SCOPE_CORRECTION_SCHEMA,
        "fingerprint": stable_hash(correction),
        "basis_sha256": basis["sha256"],
        "basis_file": basis["file"],
        "basis_fingerprint": basis_fingerprint,
        "retired_count": len(retired),
        "projection_constraint_count": projection_constraints,
        "methods": [list(binding) for binding in sorted(method_bindings or [])],
        "canonical_method": original.get("method") if path is not None else None,
        "canonical_implementation": original.get("implementation_fingerprint") if path is not None else None,
    }
