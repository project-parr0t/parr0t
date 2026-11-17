from __future__ import annotations

from dataclasses import dataclass
from hashlib import file_digest, sha256
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .errors import ConfigurationError
from .inventory_scope import _BasisSnapshot, inventory_scope
from .localization_authority import localization_authority_contract
from .models import FailureCode, Segment, TranslationResult
from .review_selection import target_language_review_reasons
from .store import stable_hash


COMPLETION_SCHEMA = "parr0t.completion-attestation.v3"


INVENTORY_SCHEMA = "parr0t.source-inventory.v1"
RUNTIME_SCHEMA = "parr0t.runtime-coverage.v1"
STATIC_SCOPE_SCHEMA = "parr0t.static-scope.v1"
UI_COMPOSITION_FIELDS = (
    "ui_slot_role",
    "ui_state_semantics",
    "ui_composed_with_ids",
    "ui_render_pattern",
    "ui_fixed_target_prefix",
    "ui_fixed_target_suffix",
    "ui_max_chars",
)
UI_SLOT_ROLES = {"standalone", "label", "value", "state_value", "template_fragment"}
INVENTORY_COMPARISON_KEY = "inventory_comparison_key"
PRIMARY_IDENTITY_SCHEMA = "parr0t.primary-inventory-identity.v1"
DISPLAY_SCOPE_SCHEMA = "parr0t.display-scope.v1"
DISPLAY_SCOPE_EVIDENCE_FIELD = "display_scope_evidence"


@dataclass(frozen=True, slots=True)
class _InventoryDocument:
    payload: dict[str, Any]
    active_rows: list[dict[str, Any]]
    scope_correction: dict[str, Any] | None


def validate_inventory_display_scope(
    payload: dict[str, Any], *, required: bool = False
) -> None:
    """Validate evidence-backed translation scope for new inventory tasks."""
    active_rows, _ = inventory_scope(payload)
    _validate_active_display_scope(payload, active_rows, required=required)


def _validate_active_display_scope(
    payload: dict[str, Any], active_rows: list[dict[str, Any]], *, required: bool = False,
) -> None:
    coverage = payload.get("coverage")
    required = required or (isinstance(coverage, dict) and coverage.get("scope_correction") is not None)
    tracking = coverage.get("display_scope") if isinstance(coverage, dict) else None
    if tracking is None and not required:
        return
    if not isinstance(tracking, dict) or tracking.get("schema") != DISPLAY_SCOPE_SCHEMA:
        raise ConfigurationError("Inventory requires a valid coverage.display_scope declaration")
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
    for row in active_rows:
        metadata = row.get("metadata", {}) if isinstance(row, dict) else {}
        evidence = metadata.get(DISPLAY_SCOPE_EVIDENCE_FIELD) if isinstance(metadata, dict) else None
        if (
            not isinstance(evidence, dict)
            or evidence.get("kind") not in {"positive_display_binding", "possible_display_dependency"}
            or not isinstance(evidence.get("evidence_fingerprint"), str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence["evidence_fingerprint"]) is None
            or not isinstance(row.get("source"), str)
            or evidence.get("source_sha256") != sha256(row["source"].encode("utf-8")).hexdigest()
            or not isinstance(metadata.get("display_binding_id"), str)
            or not metadata["display_binding_id"].strip()
        ):
            raise ConfigurationError("Every inventory target requires display binding or dependency evidence")
        if metadata.get("storage_role") in {"shared_control_value", "lookup_key", "index", "script_operand"} and metadata.get("display_projection_required") is not True:
            raise ConfigurationError("Control storage requires a separate display projection")

PRIMARY_IDENTITY_PATH = Path(".game-l10n/inventory-primary-identity.json")
_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _static_scope_tracking(coverage: dict[str, Any]) -> dict[str, Any] | None:
    tracking = coverage.get("static_scope")
    if tracking is None:
        if any(
            item.get("scope") == "static_reachability"
            for item in coverage.get("exclusions", [])
        ):
            raise ConfigurationError("Inventory static scope exclusions require scope tracking")
        return None
    required = {
        "schema",
        "claim",
        "engine_capability",
        "candidate_count",
        "included_count",
        "proven_unused_count",
        "conservative_retained_count",
        "unresolved_dynamic_reference_count",
        "methods",
        "methods_agree",
        "evidence_fingerprint",
    }
    if not isinstance(tracking, dict) or set(tracking) != required:
        raise ConfigurationError("Inventory coverage.static_scope has an invalid shape")
    if (
        tracking.get("schema") != STATIC_SCOPE_SCHEMA
        or tracking.get("claim") != "static_reachability_only"
        or not isinstance(tracking.get("engine_capability"), str)
        or not tracking["engine_capability"].strip()
        or not isinstance(tracking.get("evidence_fingerprint"), str)
        or not tracking["evidence_fingerprint"].strip()
        or not isinstance(tracking.get("methods_agree"), bool)
    ):
        raise ConfigurationError("Inventory static scope identity is invalid")
    count_names = (
        "candidate_count",
        "included_count",
        "proven_unused_count",
        "conservative_retained_count",
        "unresolved_dynamic_reference_count",
    )
    if any(
        not isinstance(tracking.get(name), int)
        or isinstance(tracking.get(name), bool)
        or tracking[name] < 0
        for name in count_names
    ):
        raise ConfigurationError("Inventory static scope counts must be non-negative integers")
    if tracking["candidate_count"] != tracking["included_count"] + tracking["proven_unused_count"]:
        raise ConfigurationError("Inventory static scope candidate accounting is incomplete")
    if tracking["conservative_retained_count"] > tracking["included_count"]:
        raise ConfigurationError("Inventory static scope retained count exceeds included targets")
    methods = tracking.get("methods")
    if not isinstance(methods, list) or len(methods) != 2:
        raise ConfigurationError("Inventory static scope requires two analysis methods")
    identities: set[tuple[str, str]] = set()
    for method in methods:
        if not isinstance(method, dict) or set(method) != {"name", "implementation_fingerprint"}:
            raise ConfigurationError("Inventory static scope method binding is invalid")
        name = method.get("name")
        fingerprint = method.get("implementation_fingerprint")
        if not isinstance(name, str) or not name.strip() or not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ConfigurationError("Inventory static scope method identity is empty")
        identities.add((name.strip().casefold(), fingerprint))
    if len(identities) != 2 or len({item[0] for item in identities}) != 2 or len({item[1] for item in identities}) != 2:
        raise ConfigurationError("Inventory static scope methods must be independent")
    if tracking["proven_unused_count"] and tracking["methods_agree"] is not True:
        raise ConfigurationError("Inventory cannot exclude targets without independent scope agreement")

    exclusions = coverage.get("exclusions", [])
    tracked_exclusions = [item for item in exclusions if item.get("scope") == "static_reachability"]
    for item in tracked_exclusions:
        if (
            item.get("affects_user_visible_text") is not False
            or not isinstance(item.get("target_count"), int)
            or isinstance(item.get("target_count"), bool)
            or item["target_count"] < 1
            or not isinstance(item.get("evidence_fingerprint"), str)
            or not item["evidence_fingerprint"].strip()
        ):
            raise ConfigurationError("Inventory static scope exclusion evidence is invalid")
    if sum(item["target_count"] for item in tracked_exclusions) != tracking["proven_unused_count"]:
        raise ConfigurationError("Inventory static scope exclusion accounting is incomplete")
    return tracking


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _file_binding(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _validate_inventory_unicode(value: Any) -> None:
    """Reject non-scalar JSON strings without exposing or rewriting their contents."""

    if isinstance(value, str):
        if _SURROGATE.search(value):
            raise ConfigurationError(
                "E_INVENTORY_INVALID_UNICODE: inventory strings must contain Unicode scalar values"
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            _validate_inventory_unicode(key)
            _validate_inventory_unicode(item)
    elif isinstance(value, list):
        for item in value:
            _validate_inventory_unicode(item)


def _inventory_document(
    path: Path, *, defer_projection_constraints: bool = False,
    required_display_scope: bool = False,
    _basis_cache: dict[Path, _BasisSnapshot] | None = None,
) -> _InventoryDocument:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            "E_INVENTORY_INVALID_UNICODE: inventory must be valid UTF-8"
        ) from exc
    _validate_inventory_unicode(payload)
    if not isinstance(payload, dict) or payload.get("schema") != INVENTORY_SCHEMA:
        raise ConfigurationError(
            f"Inventory must be a {INVENTORY_SCHEMA} JSON document: {path}"
        )
    method = payload.get("method")
    implementation_fingerprint = payload.get("implementation_fingerprint")
    coverage = payload.get("coverage")
    segments = payload.get("segments")
    if not isinstance(method, str) or not method.strip():
        raise ConfigurationError("Inventory method must be a non-empty string")
    if not isinstance(implementation_fingerprint, str) or not implementation_fingerprint.strip():
        raise ConfigurationError("Inventory implementation_fingerprint must be non-empty")
    if not isinstance(coverage, dict):
        raise ConfigurationError("Inventory coverage declaration is missing")
    if not isinstance(coverage.get("declared_complete"), bool):
        raise ConfigurationError("Inventory coverage.declared_complete must be boolean")
    exclusions = coverage.get("exclusions", [])
    if not isinstance(exclusions, list):
        raise ConfigurationError("Inventory coverage.exclusions must be a list")
    for item in exclusions:
        if not isinstance(item, dict) or not isinstance(item.get("reason_code"), str):
            raise ConfigurationError("Every inventory exclusion requires a reason_code")
        if not isinstance(item.get("affects_user_visible_text"), bool):
            raise ConfigurationError(
                "Every inventory exclusion must declare affects_user_visible_text"
            )
    tracking = _static_scope_tracking(coverage)
    if not isinstance(segments, list) or not segments:
        raise ConfigurationError("Inventory must contain at least one segment")
    active_rows, correction = inventory_scope(
        payload, path, defer_projection_constraints=defer_projection_constraints,
        _basis_cache=_basis_cache,
    )
    if tracking is not None and tracking["included_count"] != len(active_rows):
        raise ConfigurationError("Inventory static scope included count does not match eligible segments")
    _validate_active_display_scope(payload, active_rows, required=required_display_scope)
    return _InventoryDocument(payload, active_rows, correction)


def _segment_map(
    payload: dict[str, Any], *, use_comparison_key: bool = False
) -> dict[str, str]:
    result: dict[str, str] = {}
    seen_target_ids: set[str] = set()
    for index, row in enumerate(payload["segments"], start=1):
        if not isinstance(row, dict):
            raise ConfigurationError(f"Inventory segment {index} must be an object")
        target_id = row.get("id")
        source = row.get("source")
        if not isinstance(target_id, str) or not target_id:
            raise ConfigurationError(f"Inventory segment {index} requires a stable id")
        if target_id in seen_target_ids:
            raise ConfigurationError(f"Inventory contains duplicate target id: {target_id}")
        seen_target_ids.add(target_id)
        if not isinstance(source, str):
            raise ConfigurationError(f"Inventory segment {target_id!r} requires source text")
        comparison_key = target_id
        if use_comparison_key:
            metadata = row.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ConfigurationError(
                    f"Inventory segment {target_id!r} metadata must be an object"
                )
            comparison_key = metadata.get(INVENTORY_COMPARISON_KEY, target_id)
            if not isinstance(comparison_key, str) or not comparison_key.strip():
                raise ConfigurationError(
                    f"Inventory segment {target_id!r} has an invalid {INVENTORY_COMPARISON_KEY}"
                )
        if comparison_key in result:
            kind = "comparison key" if use_comparison_key else "target id"
            raise ConfigurationError(
                f"Inventory contains duplicate {kind}: {comparison_key}"
            )
        result[comparison_key] = source
    return result


def _ui_contract_map(
    payload: dict[str, Any], *, use_comparison_key: bool = False
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    comparison_keys: dict[str, str] = {}
    if use_comparison_key:
        for row in payload["segments"]:
            metadata = row.get("metadata", {})
            if not isinstance(metadata, dict):
                continue
            comparison_keys[str(row.get("id", ""))] = str(
                metadata.get(INVENTORY_COMPARISON_KEY, row.get("id", ""))
            )
    for row in payload["segments"]:
        target_id = str(row["id"])
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ConfigurationError(f"Inventory segment {target_id!r} metadata must be an object")
        comparison_key = (
            metadata.get(INVENTORY_COMPARISON_KEY, target_id)
            if use_comparison_key
            else target_id
        )
        if not isinstance(comparison_key, str) or not comparison_key.strip():
            raise ConfigurationError(
                f"Inventory segment {target_id!r} has an invalid {INVENTORY_COMPARISON_KEY}"
            )
        contract = {key: metadata[key] for key in UI_COMPOSITION_FIELDS if key in metadata}
        display_binding = {
            key: metadata[key]
            for key in ("storage_role", "display_projection_required", "display_binding_id", "display_projection_of")
            if key in metadata
        }
        if not contract:
            result[comparison_key] = display_binding
            continue
        role = contract.get("ui_slot_role")
        if role not in UI_SLOT_ROLES:
            raise ConfigurationError(
                f"Inventory segment {target_id!r} requires a supported ui_slot_role"
            )
        for key in (
            "ui_state_semantics",
            "ui_render_pattern",
            "ui_fixed_target_prefix",
            "ui_fixed_target_suffix",
        ):
            if key in contract and not isinstance(contract[key], str):
                raise ConfigurationError(
                    f"Inventory segment {target_id!r} field {key} must be a string"
                )
        related = contract.get("ui_composed_with_ids")
        if related is not None and (
            not isinstance(related, list)
            or not all(isinstance(item, str) and item for item in related)
        ):
            raise ConfigurationError(
                f"Inventory segment {target_id!r} ui_composed_with_ids must be string IDs"
            )
        if related is not None and use_comparison_key:
            contract["ui_composed_with_ids"] = [
                comparison_keys.get(item, item) for item in related
            ]
        max_chars = contract.get("ui_max_chars")
        if max_chars is not None and (
            not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 0
        ):
            raise ConfigurationError(
                f"Inventory segment {target_id!r} ui_max_chars must be a non-negative integer"
            )
        contract.update(display_binding)
        result[comparison_key] = contract
    return result

def _localization_authority_map(
    payload: dict[str, Any], *, use_comparison_key: bool = False
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in payload["segments"]:
        target_id = str(row["id"])
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ConfigurationError(f"Inventory segment {target_id!r} metadata must be an object")
        comparison_key = (
            metadata.get(INVENTORY_COMPARISON_KEY, target_id)
            if use_comparison_key
            else target_id
        )
        if not isinstance(comparison_key, str) or not comparison_key.strip():
            raise ConfigurationError(
                f"Inventory segment {target_id!r} has an invalid {INVENTORY_COMPARISON_KEY}"
            )
        try:
            result[comparison_key] = localization_authority_contract(metadata)
        except ValueError as exc:
            raise ConfigurationError(
                f"Inventory segment {target_id!r} localization authority is invalid: {exc}"
            ) from exc
    return result


def inventory_fingerprint(payload: dict[str, Any]) -> str:
    try:
        return stable_hash(
            {
                "schema": payload["schema"],
                "method": payload["method"],
                "implementation_fingerprint": payload["implementation_fingerprint"],
                "coverage": payload["coverage"],
                "segments": payload["segments"],
            }
        )
    except UnicodeEncodeError as exc:
        raise ConfigurationError(
            "E_INVENTORY_INVALID_UNICODE: inventory strings must contain Unicode scalar values"
        ) from exc


def _inventory_target_ids(payload: dict[str, Any]) -> list[str]:
    return [str(row["id"]) for row in payload["segments"]]


def _inventory_id_fingerprint(payload: dict[str, Any]) -> str:
    """Bind downstream identity without copying game text into control metadata."""

    return stable_hash(_inventory_target_ids(payload))


def _comparison_key_coverage(payload: dict[str, Any]) -> int:
    return sum(
        1
        for row in payload["segments"]
        if isinstance(row.get("metadata"), dict)
        and isinstance(row["metadata"].get(INVENTORY_COMPARISON_KEY), str)
        and row["metadata"][INVENTORY_COMPARISON_KEY].strip()
    )


def _source_multiset_overlap(
    primary_rows: dict[str, str], independent_rows: dict[str, str]
) -> tuple[int, float]:
    overlap = sum(
        (Counter(primary_rows.values()) & Counter(independent_rows.values())).values()
    )
    denominator = min(len(primary_rows), len(independent_rows))
    return overlap, (overlap / denominator if denominator else 0.0)


def _validate_scope_correction_history(
    work_dir: Path, attestation: dict[str, Any], previous: dict[str, Any] | None,
) -> None:
    correction = attestation.get("scope_correction")
    previous_correction = previous.get("scope_correction") if previous else None
    if previous_correction is not None and correction is None:
        raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_HISTORY_REQUIRED")
    for proposal in attestation.get("scope_correction_proposals", []):
        if previous is not None:
            accepted_bases = {previous.get("primary_fingerprint")}
            if isinstance(previous_correction, dict):
                accepted_bases.add(previous_correction.get("basis_fingerprint"))
            if proposal["basis_fingerprint"] not in accepted_bases:
                raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_BASIS_STALE")
        elif (work_dir / PRIMARY_IDENTITY_PATH).exists():
            raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_BASIS_UNBOUND")


def _write_primary_identity_binding(
    work_dir: Path,
    primary_path: Path,
    primary_payload: dict[str, Any],
    previous_attestation: dict[str, Any] | None,
    *, primary_fingerprint: str, scope_correction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preserve canonical IDs across handoffs while allowing additive extraction.

    Existing IDs must remain in their original relative order.  Reconciliation may add
    newly proven targets, but it cannot rename, remove, or reorder IDs that downstream
    checkpoints already reference.
    """

    binding_path = work_dir / PRIMARY_IDENTITY_PATH
    current_ids = _inventory_target_ids(primary_payload)
    existing_binding: dict[str, Any] | None = None
    if binding_path.is_file():
        try:
            existing_binding = json.loads(binding_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                "E_INVENTORY_PRIMARY_IDENTITY_INVALID: canonical ID binding is unreadable"
            ) from exc
        previous_ids = (
            existing_binding.get("ids")
            if isinstance(existing_binding, dict)
            else None
        )
        if (
            not isinstance(existing_binding, dict)
            or existing_binding.get("schema") != PRIMARY_IDENTITY_SCHEMA
            or not isinstance(previous_ids, list)
            or not all(isinstance(item, str) and item for item in previous_ids)
        ):
            raise ConfigurationError(
                "E_INVENTORY_PRIMARY_IDENTITY_INVALID: canonical ID binding is invalid"
            )
        current_positions = {target_id: index for index, target_id in enumerate(current_ids)}
        missing = [target_id for target_id in previous_ids if target_id not in current_positions]
        retained_positions = [
            current_positions[target_id]
            for target_id in previous_ids
            if target_id in current_positions
        ]
        if missing or retained_positions != sorted(retained_positions):
            raise ConfigurationError(
                "E_INVENTORY_PRIMARY_IDENTITY_DRIFT: reconciliation cannot rename, "
                "remove, or reorder checkpoint-bound primary IDs"
            )
    elif previous_attestation is not None:
        previous_fingerprint = previous_attestation.get("primary_fingerprint")
        if previous_fingerprint != primary_fingerprint:
            raise ConfigurationError(
                "E_INVENTORY_IDENTITY_LINEAGE_UNAVAILABLE: an inherited primary inventory "
                "changed before canonical ID lineage was bound"
            )

    binding = {
        "schema": PRIMARY_IDENTITY_SCHEMA,
        "primary_path": str(primary_path.resolve()),
        "records": len(current_ids),
        "id_fingerprint": stable_hash(current_ids),
        "ids": current_ids,
    }
    previous_occurrences = existing_binding.get("occurrences") if existing_binding else None
    if scope_correction is not None or (existing_binding and "occurrences" in existing_binding):
        current_occurrences = {
            row["id"]: {
                "source_sha256": sha256(row["source"].encode("utf-8")).hexdigest(),
                "comparison_key": row.get("metadata", {}).get(INVENTORY_COMPARISON_KEY, row["id"]),
            }
            for row in primary_payload["segments"]
        }
        if existing_binding and "occurrences" in existing_binding:
            if (not isinstance(previous_occurrences, dict)
                    or set(previous_occurrences) != set(existing_binding["ids"])
                    or any(not isinstance(value, dict)
                           or set(value) != {"source_sha256", "comparison_key"}
                           or not isinstance(value["source_sha256"], str)
                           or re.fullmatch(r"[0-9a-f]{64}", value["source_sha256"]) is None
                           or not isinstance(value["comparison_key"], str)
                           or not value["comparison_key"]
                           for value in previous_occurrences.values())):
                raise ConfigurationError("E_INVENTORY_PRIMARY_IDENTITY_INVALID")
            if any(current_occurrences[target_id] != value
                   for target_id, value in previous_occurrences.items()):
                raise ConfigurationError("E_INVENTORY_PRIMARY_OCCURRENCE_DRIFT")
        elif existing_binding:
            previous_fingerprint = previous_attestation.get("primary_fingerprint") if previous_attestation else None
            if (previous_fingerprint is None
                    or (primary_fingerprint != previous_fingerprint
                        and (scope_correction is None
                             or scope_correction["basis_fingerprint"] != previous_fingerprint))):
                raise ConfigurationError("E_INVENTORY_IDENTITY_LINEAGE_UNAVAILABLE")
        binding["occurrences"] = current_occurrences
    if existing_binding != binding:
        _atomic_json(binding_path, binding)
    return {
        "schema": PRIMARY_IDENTITY_SCHEMA,
        "path": PRIMARY_IDENTITY_PATH.as_posix(),
        "records": binding["records"],
        "id_fingerprint": binding["id_fingerprint"],
        "policy": "append_only_preserve_existing_id_order",
        **({"occurrence_fingerprint": stable_hash(binding["occurrences"])}
           if "occurrences" in binding else {}),
    }


def verify_inventory_pair(
    primary_path: Path, independent_path: Path, *, required_display_scope: bool = False,
) -> dict[str, Any]:
    basis_cache: dict[Path, _BasisSnapshot] = {}
    primary = _inventory_document(
        primary_path, defer_projection_constraints=True,
        required_display_scope=required_display_scope, _basis_cache=basis_cache,
    )
    independent = _inventory_document(
        independent_path, defer_projection_constraints=True,
        required_display_scope=required_display_scope, _basis_cache=basis_cache,
    )
    return _verify_inventory_payloads(primary_path, independent_path, primary, independent)

def _verify_inventory_payloads(
    primary_path: Path,
    independent_path: Path,
    primary_document: _InventoryDocument,
    independent_document: _InventoryDocument,
) -> dict[str, Any]:
    primary = primary_document.payload
    independent = independent_document.payload
    if primary["method"].strip().casefold() == independent["method"].strip().casefold():
        raise ConfigurationError("Inventory methods must be independently named")
    if primary["implementation_fingerprint"] == independent["implementation_fingerprint"]:
        raise ConfigurationError("Inventory methods must use different implementation fingerprints")
    primary_active, primary_correction = primary_document.active_rows, primary_document.scope_correction
    independent_active, independent_correction = independent_document.active_rows, independent_document.scope_correction
    primary_targets = {**primary, "segments": primary_active}
    independent_targets = {**independent, "segments": independent_active}
    actual_methods = sorted([
        [primary["method"], primary["implementation_fingerprint"]],
        [independent["method"], independent["implementation_fingerprint"]],
    ])
    correction_mismatch = bool(primary_correction) != bool(independent_correction)
    if primary_correction is not None and independent_correction is not None:
        correction_mismatch = (
            primary_correction["fingerprint"] != independent_correction["fingerprint"]
            or primary_correction["basis_fingerprint"] != independent_correction["basis_fingerprint"]
            or any(item["methods"] and item["methods"] != actual_methods
                   for item in (primary_correction, independent_correction))
            or primary_correction["canonical_method"] != primary["method"]
            or primary_correction["canonical_implementation"] != primary["implementation_fingerprint"]
        )
    projection_constraints = sum(
        correction["projection_constraint_count"]
        for correction in (primary_correction, independent_correction) if correction is not None
    )
    scope_correction = None
    if primary_correction is not None:
        scope_correction = {**primary_correction, "basis_files": [
            {"path": str((path.parent / correction["basis_file"]).resolve()),
             "sha256": correction["basis_sha256"]}
            for path, correction in ((primary_path, primary_correction),
                                     (independent_path, independent_correction))
            if correction is not None
        ]}

    primary_rows = _segment_map(primary_targets, use_comparison_key=True)
    independent_rows = _segment_map(independent_targets, use_comparison_key=True)
    primary_raw_rows = _segment_map(primary_targets)
    independent_raw_rows = _segment_map(independent_targets)
    primary_ui_contracts = _ui_contract_map(primary_targets, use_comparison_key=True)
    independent_ui_contracts = _ui_contract_map(
        independent_targets, use_comparison_key=True
    )
    primary_authority_contracts = _localization_authority_map(
        primary_targets, use_comparison_key=True
    )
    independent_authority_contracts = _localization_authority_map(
        independent_targets, use_comparison_key=True
    )
    primary_order = list(primary_rows)
    independent_order = list(independent_rows)
    primary_ids = set(primary_rows)
    independent_ids = set(independent_rows)
    raw_id_overlap_count = len(set(primary_raw_rows) & set(independent_raw_rows))
    raw_id_overlap_ratio = raw_id_overlap_count / max(
        1, min(len(primary_raw_rows), len(independent_raw_rows))
    )
    source_overlap_measured = raw_id_overlap_ratio < 0.05
    if source_overlap_measured:
        source_overlap_count, source_overlap_ratio = _source_multiset_overlap(
            primary_raw_rows, independent_raw_rows
        )
    else:
        source_overlap_count, source_overlap_ratio = 0, 0.0
    primary_comparison_key_count = _comparison_key_coverage(primary_targets)
    independent_comparison_key_count = _comparison_key_coverage(independent_targets)
    comparison_keys_complete = (
        primary_comparison_key_count == len(primary_raw_rows)
        and independent_comparison_key_count == len(independent_raw_rows)
    )
    missing_from_primary = independent_ids - primary_ids
    missing_from_independent = primary_ids - independent_ids
    source_mismatches = {
        target_id
        for target_id in primary_ids & independent_ids
        if primary_rows[target_id] != independent_rows[target_id]
    }
    ui_contract_mismatches = {
        target_id
        for target_id in primary_ids & independent_ids
        if primary_ui_contracts[target_id] != independent_ui_contracts[target_id]
    }
    authority_contract_mismatches = {
        target_id
        for target_id in primary_ids & independent_ids
        if primary_authority_contracts[target_id]
        != independent_authority_contracts[target_id]
    }
    agreed_target_count = len(
        (primary_ids & independent_ids)
        - source_mismatches - ui_contract_mismatches - authority_contract_mismatches
    )
    order_mismatch = primary_order != independent_order
    visible_exclusions = [
        item
        for payload in (primary, independent)
        for item in payload["coverage"].get("exclusions", [])
        if item.get("affects_user_visible_text") is True
    ]
    primary_scope = _static_scope_tracking(primary["coverage"])
    independent_scope = _static_scope_tracking(independent["coverage"])
    scope_tracking_mismatch = primary_scope != independent_scope
    comparison_key_overlap_ratio = len(primary_ids & independent_ids) / max(
        1, min(len(primary_ids), len(independent_ids))
    )
    namespace_mismatch = bool(
        primary_ids
        and independent_ids
        and raw_id_overlap_ratio < 0.05
        and source_overlap_ratio >= 0.90
        and not (comparison_keys_complete and comparison_key_overlap_ratio >= 0.90)
        and (primary_ids != independent_ids)
    )
    declared_complete = all(
        payload["coverage"]["declared_complete"] for payload in (primary, independent)
    )
    complete = not (
        missing_from_primary
        or missing_from_independent
        or source_mismatches
        or ui_contract_mismatches
        or order_mismatch
        or authority_contract_mismatches
        or visible_exclusions
        or scope_tracking_mismatch
        or not declared_complete
        or correction_mismatch
        or projection_constraints
    )
    issue_codes: list[str] = []
    if correction_mismatch:
        issue_codes.append("INVENTORY_SCOPE_CORRECTION_MISMATCH")
    if projection_constraints:
        issue_codes.append("INVENTORY_SCOPE_PROJECTION_REQUIRED")
    if namespace_mismatch:
        issue_codes.append("INVENTORY_IDENTITY_NAMESPACE_MISMATCH")
    if missing_from_primary:
        issue_codes.append("INDEPENDENT_INVENTORY_FOUND_UNEXTRACTED_TARGETS")
    if missing_from_independent:
        issue_codes.append("INDEPENDENT_INVENTORY_MISSED_PRIMARY_TARGETS")
    if source_mismatches:
        issue_codes.append("INVENTORY_SOURCE_MISMATCH")
    if ui_contract_mismatches:
        issue_codes.append("INVENTORY_UI_COMPOSITION_MISMATCH")
    if authority_contract_mismatches:
        issue_codes.append("INVENTORY_LOCALIZATION_AUTHORITY_MISMATCH")
    if order_mismatch:
        issue_codes.append("INVENTORY_ORDER_MISMATCH")
    if visible_exclusions:
        issue_codes.append("USER_VISIBLE_SCOPE_EXCLUDED")
    if scope_tracking_mismatch:
        issue_codes.append("STATIC_SCOPE_TRACKING_MISMATCH")
    if not declared_complete:
        issue_codes.append("INVENTORY_NOT_DECLARED_COMPLETE")
    missing_gap_score = (
        1
        if namespace_mismatch
        else len(missing_from_primary) + len(missing_from_independent)
    )
    gap_score = (
        missing_gap_score
        + len(source_mismatches)
        + len(ui_contract_mismatches)
        + len(authority_contract_mismatches)
        + len(visible_exclusions)
        + int(order_mismatch)
        + int(scope_tracking_mismatch)
        + int(not declared_complete)
        + int(correction_mismatch)
        + projection_constraints
    )
    return {
        "schema": "parr0t.inventory-attestation.v1",
        "status": "static_verified" if complete else "incomplete",
        "primary_path": str(primary_path.resolve()),
        "independent_path": str(independent_path.resolve()),
        "primary_method": primary["method"],
        "independent_method": independent["method"],
        "primary_implementation_fingerprint": primary["implementation_fingerprint"],
        "independent_implementation_fingerprint": independent[
            "implementation_fingerprint"
        ],
        "primary_fingerprint": inventory_fingerprint(primary),
        "independent_fingerprint": inventory_fingerprint(independent),
        "primary_id_fingerprint": _inventory_id_fingerprint(primary),
        "independent_id_fingerprint": _inventory_id_fingerprint(independent),
        "primary_segments": len(primary_rows),
        "independent_segments": len(independent_rows),
        "agreed_target_count": agreed_target_count,
        "primary_occurrences": len(primary["segments"]),
        "independent_occurrences": len(independent["segments"]),
        "scope_correction": scope_correction,
        "scope_correction_proposals": [correction for correction in
                                       (primary_correction, independent_correction) if correction is not None],
        "projection_constraint_count": projection_constraints,
        "missing_from_primary_count": len(missing_from_primary),
        "missing_from_independent_count": len(missing_from_independent),
        "source_mismatch_count": len(source_mismatches),
        "ui_composition_mismatch_count": len(ui_contract_mismatches),
        "localization_authority_mismatch_count": len(authority_contract_mismatches),
        "order_mismatch": order_mismatch,
        "user_visible_exclusion_count": len(visible_exclusions),
        "static_scope": primary_scope,
        "static_scope_tracking_mismatch": scope_tracking_mismatch,
        "static_complete": complete,
        "issue_codes": issue_codes,
        "gap_score": gap_score,
        "continuation_required": not complete,
        "identity_gate": {
            "status": (
                "namespace_mismatch"
                if namespace_mismatch
                else "comparison_keys_bound"
                if comparison_keys_complete
                else "stable"
            ),
            "raw_id_overlap_count": raw_id_overlap_count,
            "raw_id_overlap_ratio": round(raw_id_overlap_ratio, 6),
            "source_multiset_overlap_count": source_overlap_count,
            "source_multiset_overlap_ratio": round(source_overlap_ratio, 6),
            "source_multiset_overlap_measured": source_overlap_measured,
            "primary_comparison_key_count": primary_comparison_key_count,
            "independent_comparison_key_count": independent_comparison_key_count,
            "comparison_keys_complete": comparison_keys_complete,
            "requires_identity_alias_evidence": namespace_mismatch,
            "primary_ids_are_canonical": True,
        },
        "file_bindings": {
            "primary": _file_binding(primary_path),
            "independent": _file_binding(independent_path),
        },
    }


def save_inventory_attestation(
    work_dir: Path, primary_path: Path, independent_path: Path
) -> dict[str, Any]:
    state_path = work_dir / ".game-l10n" / "inventory-attestation.json"
    previous: dict[str, Any] | None = None
    if state_path.is_file():
        try:
            candidate = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                isinstance(candidate, dict)
                and candidate.get("schema") == "parr0t.inventory-attestation.v1"
            ):
                previous = candidate
        except (OSError, ValueError, json.JSONDecodeError):
            previous = None

    # Validate both inventory documents before advancing canonical ID lineage.
    basis_cache: dict[Path, _BasisSnapshot] = {}
    primary_document = _inventory_document(primary_path, defer_projection_constraints=True, _basis_cache=basis_cache)
    independent_document = _inventory_document(independent_path, defer_projection_constraints=True, _basis_cache=basis_cache)
    primary_payload = primary_document.payload
    attestation = _verify_inventory_payloads(
        primary_path, independent_path, primary_document, independent_document
    )
    _validate_scope_correction_history(work_dir, attestation, previous)
    if (
        previous is not None
        and (
            "INVENTORY_IDENTITY_NAMESPACE_MISMATCH" in attestation["issue_codes"]
            or "INVENTORY_IDENTITY_NAMESPACE_MISMATCH" in previous.get("issue_codes", [])
        )
        and len(primary_payload["segments"]) > int(previous.get("primary_occurrences", previous.get("primary_segments", 0)))
    ):
        raise ConfigurationError(
            "E_INVENTORY_IDENTITY_NAMESPACE_MISMATCH: reconcile identity aliases before primary expansion"
        )
    identity_binding = _write_primary_identity_binding(
        work_dir, primary_path, primary_payload, previous,
        primary_fingerprint=attestation["primary_fingerprint"],
        scope_correction=attestation["scope_correction"],
    )
    attestation["primary_identity_binding"] = identity_binding

    previous_reconciliation = (
        previous.get("reconciliation", {}) if isinstance(previous, dict) else {}
    )
    previous_score = (
        previous.get("gap_score") if isinstance(previous, dict) else None
    )
    current_vector = {
        "missing_from_primary_count": attestation["missing_from_primary_count"],
        "missing_from_independent_count": attestation[
            "missing_from_independent_count"
        ],
        "source_mismatch_count": attestation["source_mismatch_count"],
        "ui_composition_mismatch_count": attestation[
            "ui_composition_mismatch_count"
        ],
        "localization_authority_mismatch_count": attestation[
            "localization_authority_mismatch_count"
        ],
        "scope_correction_mismatch": "INVENTORY_SCOPE_CORRECTION_MISMATCH"
        in attestation["issue_codes"],
        "projection_constraint_count": attestation["projection_constraint_count"],
        "identity_namespace_mismatch": "INVENTORY_IDENTITY_NAMESPACE_MISMATCH"
        in attestation["issue_codes"],
        "user_visible_exclusion_count": attestation[
            "user_visible_exclusion_count"
        ],
        "order_mismatch": attestation["order_mismatch"],
        "static_scope_tracking_mismatch": attestation[
            "static_scope_tracking_mismatch"
        ],
        "declared_complete": "INVENTORY_NOT_DECLARED_COMPLETE"
        not in attestation["issue_codes"],
    }
    previous_vector = (
        previous_reconciliation.get("gap_vector")
        if isinstance(previous_reconciliation, dict)
        else None
    )
    same_inputs = bool(
        previous
        and previous.get("primary_fingerprint") == attestation["primary_fingerprint"]
        and previous.get("independent_fingerprint")
        == attestation["independent_fingerprint"]
    )
    if attestation["static_complete"]:
        progress_state = "complete"
        unchanged_observations = 0
    elif previous is None:
        progress_state = "initial"
        unchanged_observations = 0
    elif same_inputs:
        progress_state = "unchanged"
        unchanged_observations = int(
            previous_reconciliation.get("unchanged_observations", 0)
        ) + 1
    elif isinstance(previous_score, int) and attestation["gap_score"] < previous_score:
        progress_state = "improved"
        unchanged_observations = 0
    elif (
        previous_vector == current_vector
        and type(previous.get("agreed_target_count")) is int
        and attestation["agreed_target_count"] > previous["agreed_target_count"]
        and attestation["primary_segments"] >= previous.get("primary_segments", 0)
        and attestation["independent_segments"] >= previous.get("independent_segments", 0)
    ):
        # More independently agreeing active targets is net progress even while
        # an unchanged completeness declaration keeps the weighted gap at one.
        # Raw occurrence growth and input-fingerprint churn alone do not qualify.
        progress_state = "improved"
        unchanged_observations = 0
    else:
        progress_state = "changed"
        unchanged_observations = int(previous_reconciliation.get("unchanged_observations", 0)) + 1
    attestation["reconciliation"] = {
        "generation": int(previous_reconciliation.get("generation", 0)) + 1,
        "progress_state": progress_state,
        "gap_score": attestation["gap_score"],
        "previous_gap_score": previous_score,
        "gap_vector": current_vector,
        "unchanged_observations": unchanged_observations,
        "continuation_required": attestation["continuation_required"],
        "terminal": False,
    }
    _atomic_json(state_path, attestation)
    return attestation


def require_inventory_attestation(work_dir: Path, primary_path: Path) -> dict[str, Any]:
    state_path = work_dir / ".game-l10n" / "inventory-attestation.json"
    if not state_path.is_file():
        raise ConfigurationError(
            "Independent source inventory has not been verified; run verify-inventory first"
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") != "static_verified" or state.get("static_complete") is not True:
        raise ConfigurationError("Independent source inventory is incomplete")
    if state.get("primary_path") != str(primary_path.resolve()):
        raise ConfigurationError("Verified primary inventory does not match this context input")
    correction = state.get("scope_correction")
    if correction is not None:
        basis_file = correction.get("basis_file") if isinstance(correction, dict) else None
        if (not isinstance(basis_file, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.json", basis_file) is None):
            raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_BASIS_STALE")
        independent_path = Path(str(state.get("independent_path", "")))
        try:
            for directory in {primary_path.parent.resolve(), independent_path.parent.resolve()}:
                basis_path = directory / basis_file
                if basis_path.resolve().parent != directory:
                    raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_BASIS_STALE")
                with basis_path.open("rb") as handle:
                    if file_digest(handle, "sha256").hexdigest() != correction.get("basis_sha256"):
                        raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_BASIS_STALE")
            independent_binding = _file_binding(independent_path)
        except OSError as exc:
            raise ConfigurationError("E_INVENTORY_SCOPE_CORRECTION_BASIS_STALE") from exc
        if state.get("file_bindings", {}).get("independent") != independent_binding:
            independent_payload = _inventory_document(independent_path)
            if inventory_fingerprint(independent_payload.payload) != state.get("independent_fingerprint"):
                raise ConfigurationError("Independent inventory changed after scope correction verification")
    bindings = state.get("file_bindings")
    if (
        isinstance(bindings, dict)
        and bindings.get("primary") == _file_binding(primary_path)
    ):
        return state
    payload = _inventory_document(primary_path)
    if state.get("primary_fingerprint") != inventory_fingerprint(payload.payload):
        raise ConfigurationError("Primary inventory changed after independent verification")
    independent_path = Path(str(state.get("independent_path", "")))
    state["file_bindings"] = {
        "primary": _file_binding(primary_path),
        "independent": (
            _file_binding(independent_path) if independent_path.is_file() else None
        ),
    }
    _atomic_json(state_path, state)
    return state


def record_runtime_coverage(work_dir: Path, report_path: Path) -> dict[str, Any]:
    payload = json.loads(report_path.read_text(encoding="utf-8-sig"))
    allowed = {
        "schema",
        "inventory_fingerprint",
        "observation_method",
        "observed_target_ids",
        "unmatched_visible_count",
        "untranslated_visible_count",
        "unvisited_state_count",
        "known_unobservable_categories",
    }
    if not isinstance(payload, dict) or payload.get("schema") != RUNTIME_SCHEMA:
        raise ConfigurationError(f"Runtime report must use schema {RUNTIME_SCHEMA}")
    if set(payload) - allowed:
        raise ConfigurationError("Runtime report contains unsupported fields")
    if not isinstance(payload.get("observation_method"), str) or not payload[
        "observation_method"
    ].strip():
        raise ConfigurationError("Runtime observation_method must be a non-empty string")
    target_ids = payload.get("observed_target_ids", [])
    counts = {
        name: payload.get(name, 0)
        for name in (
            "unmatched_visible_count",
            "untranslated_visible_count",
            "unvisited_state_count",
        )
    }
    if not isinstance(target_ids, list) or not all(isinstance(item, str) for item in target_ids):
        raise ConfigurationError("Runtime observed_target_ids must be strings")
    if any(not isinstance(value, int) or value < 0 for value in counts.values()):
        raise ConfigurationError("Runtime coverage counts must be non-negative integers")
    categories = payload.get("known_unobservable_categories", [])
    if not isinstance(categories, list) or not all(isinstance(item, str) for item in categories):
        raise ConfigurationError("Runtime known_unobservable_categories must be strings")

    inventory_state = require_inventory_attestation(
        work_dir,
        Path(
            json.loads(
                (work_dir / ".game-l10n" / "inventory-attestation.json").read_text(
                    encoding="utf-8"
                )
            )["primary_path"]
        ),
    )
    if payload.get("inventory_fingerprint") != inventory_state["primary_fingerprint"]:
        raise ConfigurationError("Runtime report targets a different source inventory")
    primary_path = Path(inventory_state["primary_path"])
    primary = _inventory_document(primary_path)
    expected_ids = {row["id"] for row in primary.active_rows}
    observed_ids = set(target_ids)
    unknown_target_count = len(observed_ids - expected_ids)
    unobserved_target_count = len(expected_ids - observed_ids)
    complete = (
        not any(counts.values())
        and not categories
        and not unknown_target_count
        and not unobserved_target_count
    )
    state = {
        "schema": "parr0t.runtime-attestation.v1",
        "status": "runtime_verified" if complete else "observed_incomplete",
        "inventory_fingerprint": payload["inventory_fingerprint"],
        "observation_method": str(payload.get("observation_method", "")),
        "observed_target_count": len(set(target_ids)),
        **counts,
        "unobserved_target_count": unobserved_target_count,
        "unknown_target_count": unknown_target_count,
        "known_unobservable_category_count": len(categories),
        "runtime_complete": complete,
        "issue_codes": [] if complete else ["RUNTIME_COVERAGE_INCOMPLETE"],
    }
    _atomic_json(work_dir / ".game-l10n" / "runtime-attestation.json", state)
    return state


def runtime_coverage_state(work_dir: Path, inventory_fingerprint_value: str) -> dict[str, Any]:
    path = work_dir / ".game-l10n" / "runtime-attestation.json"
    if not path.is_file():
        return {
            "status": "not_run",
            "runtime_complete": False,
            "issue_codes": ["REAL_RUNTIME_NOT_EXECUTED"],
        }
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("inventory_fingerprint") != inventory_fingerprint_value:
        return {
            "status": "stale",
            "runtime_complete": False,
            "issue_codes": ["RUNTIME_COVERAGE_STALE"],
        }
    return state


def completion_attestation(
    segments: list[Segment],
    results: Iterable[TranslationResult],
    *,
    inventory_segments: list[Segment],
    inventory: dict[str, Any],
    runtime: dict[str, Any],
    review_required: bool,
    target_language: str = "",
    language_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = list(results)
    segment_by_id = {item.id: item for item in segments}
    source_by_id = {item.id: item.source for item in segments}
    inventory_by_id = {item.id: item.source for item in inventory_segments}
    result_ids = [item.segment_id for item in rows]
    result_counts = Counter(result_ids)
    duplicate_count = sum(count - 1 for count in result_counts.values() if count > 1)
    missing_ids = set(source_by_id) - set(result_ids)
    extra_ids = set(result_ids) - set(source_by_id)
    source_mismatch_count = sum(
        item.segment_id in source_by_id and item.source != source_by_id[item.segment_id]
        for item in rows
    )
    reviewed = []
    for item in rows:
        strong_verdict = any(
            attempt.stage
            in {
                "review",
                "review-confirm",
                "review-grace-retry",
                "review-refusal-retry",
            }
            and attempt.status == "approved"
            and attempt.candidate_generation == item.candidate_generation
            for attempt in item.attempts
        )
        triage_verdict = any(
            attempt.stage
            in {
                "review-triage",
                "review-triage-confirm",
                "review-triage-grace-retry",
                "review-triage-refusal-retry",
            }
            and attempt.status == "approved"
            and attempt.candidate_generation == item.candidate_generation
            for attempt in item.attempts
        )
        review_tier = item.metadata.get("review_tier")
        final_verdict = (
            strong_verdict
            if review_tier in {"strong", "strong_pending"}
            else strong_verdict or triage_verdict
        )
        if (
            item.status == "reviewed"
            and item.acceptance_authority == "reviewer"
            and item.candidate_digest == stable_hash(item.text)
            and final_verdict
        ):
            reviewed.append(item)
    reused = [item for item in rows if item.metadata.get("candidate_origin") == "existing"]
    source_preserved = [item for item in rows if item.status == "source_preserved"]
    integrity_preserved = [
        item
        for item in source_preserved
        if item.resolution_code == FailureCode.INTEGRITY_INVALID.value
    ]
    route_preserved = [
        item
        for item in source_preserved
        if item.resolution_code != FailureCode.INTEGRITY_INVALID.value
    ]
    accepted_unreviewed = [item for item in rows if item.status == "accepted_unreviewed"]
    failed = [item for item in rows if item.status == "failed"]
    target_language_mismatches = [
        item
        for item in rows
        if item.status not in {"failed", "source_preserved"}
        and target_language
        and target_language_review_reasons(
            item.text,
            target_language=target_language,
            metadata=segment_by_id.get(item.segment_id, Segment("", "")).metadata,
        )
    ]
    terminal_unresolved = [*source_preserved, *accepted_unreviewed]
    unresolved_ids = {item.segment_id for item in terminal_unresolved}
    failed_ids = {item.segment_id for item in failed}
    reviewable_reused = [
        item
        for item in reused
        if item.segment_id not in unresolved_ids | failed_ids
    ]
    reused_reviewed = [
        item for item in reviewed if item.metadata.get("candidate_origin") == "existing"
    ]
    review_denominator = len(rows) - len(terminal_unresolved) - len(failed)
    review_complete = not review_required or (
        len(reviewed) == review_denominator
        and len(reused_reviewed) == len(reviewable_reused)
    )
    alignment_complete = not (
        missing_ids or extra_ids or duplicate_count or source_mismatch_count
    )
    full_inventory_scope = source_by_id == inventory_by_id
    issue_codes: list[str] = []
    if not alignment_complete:
        issue_codes.append("RESULT_INVENTORY_MISMATCH")
    if not review_complete:
        issue_codes.append("SEMANTIC_REVIEW_INCOMPLETE")
    if not full_inventory_scope:
        issue_codes.append("PARTIAL_TRANSLATION_SCOPE")
    if route_preserved:
        issue_codes.append("SOURCE_PRESERVED_AFTER_ROUTE_EXHAUSTION")
    if integrity_preserved:
        issue_codes.append("SOURCE_PRESERVED_AFTER_INTEGRITY_REJECTION")
    if accepted_unreviewed:
        issue_codes.append("USER_ACCEPTED_UNREVIEWED_AFTER_ROUTE_EXHAUSTION")
    if target_language_mismatches:
        issue_codes.append("TARGET_LANGUAGE_MISMATCH")
    issue_codes.extend(runtime.get("issue_codes", []))
    blocking_unresolved = sum(
        item.segment_id in unresolved_ids
        and bool(segment_by_id[item.segment_id].metadata.get("blocking_on_failure", False))
        for item in rows
        if item.segment_id in source_by_id
    )
    target_language_complete = not target_language_mismatches
    accounting_complete = bool(
        alignment_complete
        and review_complete
        and not failed
        and target_language_complete
    )
    target_completion = bool(
        accounting_complete and blocking_unresolved == 0
    )
    return {
        "schema": COMPLETION_SCHEMA,
        "source_inventory": {
            "status": inventory["status"],
            "static_complete": inventory["static_complete"],
            "segment_count": inventory["primary_segments"],
            "fingerprint": inventory["primary_fingerprint"],
        },
        "result_alignment": {
            "complete": alignment_complete,
            "manifest_missing_count": len(missing_ids),
            "extra_count": len(extra_ids),
            "duplicate_count": duplicate_count,
            "source_mismatch_count": source_mismatch_count,
        },
        "target_scope": {
            "full_inventory": full_inventory_scope,
            "target_count": len(source_by_id),
            "inventory_count": len(inventory_by_id),
        },
        "semantic_review": {
            "required": review_required,
            "complete": review_complete,
            "reviewed_count": len(reviewed),
            "review_denominator": review_denominator,
            "reused_candidate_count": len(reused),
            "reused_reviewed_count": len(reused_reviewed),
            "source_preserved_count": len(source_preserved),
            "accepted_unreviewed_count": len(accepted_unreviewed),
            "terminal_unresolved_count": len(terminal_unresolved),
            "blocking_unresolved_count": blocking_unresolved,
            "failed_count": len(failed),
        },
        "target_language": {
            "enforced": language_binding is not None,
            "binding": language_binding,
            "complete": target_language_complete,
            "mismatch_count": len(target_language_mismatches),
        },
        "runtime_coverage": runtime,
        "accounting_complete": accounting_complete,
        "semantic_completion": bool(accounting_complete and not terminal_unresolved),
        "target_completion": target_completion,
        "static_completion": bool(
            inventory["static_complete"]
            and full_inventory_scope
            and target_completion
        ),
        "runtime_completion": bool(
            inventory["static_complete"]
            and full_inventory_scope
            and target_completion
            and runtime.get("runtime_complete") is True
        ),
        "issue_codes": list(dict.fromkeys(issue_codes)),
    }


def save_completion_attestation(work_dir: Path, attestation: dict[str, Any]) -> Path:
    path = work_dir / ".game-l10n" / "completion-attestation.json"
    _atomic_json(path, attestation)
    return path
