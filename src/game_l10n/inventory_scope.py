from __future__ import annotations

from dataclasses import dataclass
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
DISPLAY_SCOPE_SCHEMA = "parr0t.display-scope.v1"
DISPLAY_SCOPE_EVIDENCE_FIELD = "display_scope_evidence"
TEXT_ELIGIBILITY_CONTRACT = "parr0t.text-eligibility.v1"
_TEXT_SOURCE_FIELDS = {
    "route", "locale_probe", "evidence_fingerprint", "screen_review", "pending_display_count",
}
_ROW_ELIGIBILITY_FIELDS = {"storage_role", "display_binding_id", DISPLAY_SCOPE_EVIDENCE_FIELD}


class ActiveControlStorageError(ConfigurationError):
    code = "INVENTORY_ACTIVE_CONTROL_STORAGE"

    def __init__(self) -> None:
        super().__init__(
            f"{self.code}: Control storage is not an active display target; "
            "use a separate display projection"
        )


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


@dataclass(frozen=True, slots=True)
class _BasisSnapshot:
    sha256: str
    payload: dict[str, Any]
    rows: dict[str, dict[str, Any]]
    fingerprint: str


def _inventory_source_fingerprint(payload: dict[str, Any]) -> str:
    identity = {key: payload[key] for key in (
        "schema", "method", "implementation_fingerprint", "coverage", "segments"
    )}
    coverage = identity["coverage"]
    if "extraction_evidence" in coverage:
        identity["coverage"] = {key: value for key, value in coverage.items() if key != "extraction_evidence"}
    return stable_hash(identity)


def _read_basis(path: Path, expected_sha256: str) -> _BasisSnapshot:
    try:
        raw = path.read_bytes()
        digest = sha256(raw).hexdigest()
        if digest != expected_sha256:
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
    rows = _rows_by_key(original.get("segments"))
    fingerprint = _inventory_source_fingerprint(original)
    return _BasisSnapshot(digest, original, rows, fingerprint)


def inventory_scope(
    payload: dict[str, Any], path: Path | None = None,
    *, defer_projection_constraints: bool = False,
    _basis_cache: dict[Path, _BasisSnapshot] | None = None,
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
        try:
            basis_path = (path.parent / basis["file"]).resolve()
        except (OSError, ValueError) as exc:
            raise _invalid() from exc
        if basis_path.parent != path.parent.resolve() or basis_path == path.resolve():
            raise _invalid()
        snapshot = _basis_cache.get(basis_path) if _basis_cache is not None else None
        if snapshot is None:
            snapshot = _read_basis(basis_path, basis["sha256"])
            if _basis_cache is not None:
                _basis_cache[basis_path] = snapshot
        if snapshot.sha256 != basis["sha256"]:
            raise _invalid()
        original = snapshot.payload
        original_rows = snapshot.rows
        basis_fingerprint = snapshot.fingerprint
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


def _validate_text_source(
    coverage: Any, *, required: bool, screen_review_skip: bool | None,
) -> str | None:
    if not isinstance(coverage, dict) or "text_source" not in coverage:
        if required:
            raise ConfigurationError("Inventory requires a coverage.text_source declaration")
        return None
    declaration = coverage["text_source"]
    if not isinstance(declaration, dict) or declaration.keys() != _TEXT_SOURCE_FIELDS:
        raise ConfigurationError("Inventory coverage.text_source has an invalid shape")
    route = declaration["route"]
    fingerprint = declaration["evidence_fingerprint"]
    pending = declaration["pending_display_count"]
    screen_review = declaration["screen_review"]
    if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
        raise ConfigurationError("Inventory text source requires a lowercase SHA-256 evidence fingerprint")
    if type(pending) is not int or pending < 0:
        raise ConfigurationError("Inventory pending_display_count must be a non-negative integer")
    if not isinstance(coverage.get("declared_complete"), bool):
        raise ConfigurationError("Inventory coverage.declared_complete must be boolean")
    if route == "locale_data":
        if (declaration["locale_probe"] != "applicable"
                or screen_review != "not_applicable" or pending != 0):
            raise ConfigurationError(
                "Inventory locale data requires an applicable locale probe and no screen review or pending display text"
            )
        if "scope_correction" in coverage or "static_scope" in coverage:
            raise ConfigurationError("Inventory locale data cannot use scope correction or static scope")
    elif route == "display_extraction":
        if (declaration["locale_probe"] != "unavailable"
                or screen_review not in ("static", "user_skipped")):
            raise ConfigurationError(
                "Inventory display extraction requires an unavailable locale probe and a screen review disposition"
            )
        if (screen_review == "user_skipped" and (required or screen_review_skip is not None)
                and screen_review_skip is not True):
            raise ConfigurationError("Inventory screen review skip requires task approval")
        if coverage["declared_complete"] and pending:
            raise ConfigurationError("Complete inventory cannot have pending display text")
    else:
        raise ConfigurationError("Inventory text source route is invalid")
    return route


def _validate_row_eligibility(
    row: dict[str, Any], metadata: dict[str, Any], evidence: dict[str, Any], *, locale: bool,
) -> None:
    # Segment readers merge top-level extras over metadata; eligibility cannot be overridden.
    if not _ROW_ELIGIBILITY_FIELDS.isdisjoint(row):
        raise ConfigurationError("Inventory target eligibility fields must appear only in metadata")
    if locale:
        if (metadata.get("storage_role") != "locale_value" or evidence.get("kind") != "locale_value"
                or evidence.get("binding_basis") != "locale_catalog"):
            raise ConfigurationError("Inventory locale targets require locale value storage and catalog evidence")
    elif (metadata.get("storage_role") != "display_value"
            or evidence.get("kind") != "positive_display_binding"
            or evidence.get("binding_basis") not in ("format_field", "render_argument")):
        raise ConfigurationError(
            "Inventory display targets require display value storage and a positive format or render binding"
        )


def _validate_active_display_scope(
    payload: dict[str, Any], active_rows: list[dict[str, Any]], *, required: bool = False,
    required_text_source: bool = False, screen_review_skip: bool | None = None,
) -> None:
    """Check declared eligibility before attestation or any segment consumer."""
    coverage = payload.get("coverage")
    route = _validate_text_source(
        coverage, required=required_text_source, screen_review_skip=screen_review_skip,
    )
    required = required or route is not None or (
        isinstance(coverage, dict) and coverage.get("scope_correction") is not None
    )
    tracking = coverage.get("display_scope") if isinstance(coverage, dict) else None
    if tracking is None and not required:
        return
    if not isinstance(tracking, dict) or tracking.get("schema") != DISPLAY_SCOPE_SCHEMA:
        raise ConfigurationError("Inventory requires a valid coverage.display_scope declaration")
    if route is not None and not isinstance(active_rows, list):
        raise ConfigurationError("Inventory segments must be a list")
    for key in ("raw_occurrence_count", "display_candidate_count", "control_only_count", "unclassified_count"):
        value = tracking.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigurationError(f"Inventory display scope {key} must be a non-negative integer")
    if tracking["control_only_count"] + tracking["unclassified_count"] > tracking["raw_occurrence_count"]:
        raise ConfigurationError("Inventory raw occurrence accounting is inconsistent")
    if active_rows and tracking["raw_occurrence_count"] == 0:
        raise ConfigurationError("Inventory display candidates require source occurrences")
    fingerprint = tracking.get("evidence_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ConfigurationError("Inventory display scope requires an evidence_fingerprint")
    if tracking["display_candidate_count"] != len(active_rows):
        raise ConfigurationError("Inventory display candidate count must match segments")
    locale = route == "locale_data"
    if locale and (tracking["control_only_count"] or tracking["unclassified_count"]
                   or tracking["raw_occurrence_count"] != len(active_rows)):
        raise ConfigurationError("Inventory locale accounting must count only active locale values")
    evidence_error = (
        "Every inventory target requires display binding or dependency evidence" if route is None
        else "Every inventory target requires source-bound eligibility evidence"
    )
    for row in active_rows:
        metadata = row.get("metadata", {}) if isinstance(row, dict) else {}
        evidence = metadata.get(DISPLAY_SCOPE_EVIDENCE_FIELD) if isinstance(metadata, dict) else None
        if (
            not isinstance(evidence, dict)
            or (route is None and evidence.get("kind") not in (
                "positive_display_binding", "possible_display_dependency",
            ))
            or not isinstance(evidence.get("evidence_fingerprint"), str)
            or _SHA256.fullmatch(evidence["evidence_fingerprint"]) is None
            or not isinstance(row.get("source"), str)
            or not isinstance(metadata.get("display_binding_id"), str)
            or not metadata["display_binding_id"].strip()
        ):
            raise ConfigurationError(evidence_error)
        try:
            source_digest = sha256(row["source"].encode("utf-8")).hexdigest()
        except UnicodeEncodeError as exc:
            raise ConfigurationError(
                "E_INVENTORY_INVALID_UNICODE: inventory strings must contain Unicode scalar values"
            ) from exc
        if evidence.get("source_sha256") != source_digest:
            raise ConfigurationError(evidence_error)
        storage_role = metadata.get("storage_role")
        if isinstance(storage_role, str) and storage_role in _CONTROL_ROLES:
            raise ActiveControlStorageError()
        if route is not None:
            _validate_row_eligibility(row, metadata, evidence, locale=locale)
