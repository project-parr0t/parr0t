from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .errors import ConfigurationError
from .models import Segment, TranslationResult
from .store import stable_hash


INVENTORY_SCHEMA = "parr0t.source-inventory.v1"
RUNTIME_SCHEMA = "parr0t.runtime-coverage.v1"
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


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _inventory_document(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
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
    if not isinstance(segments, list) or not segments:
        raise ConfigurationError("Inventory must contain at least one segment")
    return payload


def _segment_map(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for index, row in enumerate(payload["segments"], start=1):
        if not isinstance(row, dict):
            raise ConfigurationError(f"Inventory segment {index} must be an object")
        target_id = row.get("id")
        source = row.get("source")
        if not isinstance(target_id, str) or not target_id:
            raise ConfigurationError(f"Inventory segment {index} requires a stable id")
        if not isinstance(source, str):
            raise ConfigurationError(f"Inventory segment {target_id!r} requires source text")
        if target_id in result:
            raise ConfigurationError(f"Inventory contains duplicate target id: {target_id}")
        result[target_id] = source
    return result


def _ui_contract_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in payload["segments"]:
        target_id = str(row["id"])
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ConfigurationError(f"Inventory segment {target_id!r} metadata must be an object")
        contract = {key: metadata[key] for key in UI_COMPOSITION_FIELDS if key in metadata}
        if not contract:
            result[target_id] = {}
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
        max_chars = contract.get("ui_max_chars")
        if max_chars is not None and (
            not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 0
        ):
            raise ConfigurationError(
                f"Inventory segment {target_id!r} ui_max_chars must be a non-negative integer"
            )
        result[target_id] = contract
    return result


def inventory_fingerprint(payload: dict[str, Any]) -> str:
    return stable_hash(
        {
            "schema": payload["schema"],
            "method": payload["method"],
            "implementation_fingerprint": payload["implementation_fingerprint"],
            "coverage": payload["coverage"],
            "segments": payload["segments"],
        }
    )


def verify_inventory_pair(primary_path: Path, independent_path: Path) -> dict[str, Any]:
    primary = _inventory_document(primary_path)
    independent = _inventory_document(independent_path)
    if primary["method"].strip().casefold() == independent["method"].strip().casefold():
        raise ConfigurationError("Inventory methods must be independently named")
    if primary["implementation_fingerprint"] == independent["implementation_fingerprint"]:
        raise ConfigurationError("Inventory methods must use different implementation fingerprints")

    primary_rows = _segment_map(primary)
    independent_rows = _segment_map(independent)
    primary_ui_contracts = _ui_contract_map(primary)
    independent_ui_contracts = _ui_contract_map(independent)
    primary_order = list(primary_rows)
    independent_order = list(independent_rows)
    primary_ids = set(primary_rows)
    independent_ids = set(independent_rows)
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
    order_mismatch = primary_order != independent_order
    visible_exclusions = [
        item
        for payload in (primary, independent)
        for item in payload["coverage"].get("exclusions", [])
        if item.get("affects_user_visible_text") is True
    ]
    declared_complete = all(
        payload["coverage"]["declared_complete"] for payload in (primary, independent)
    )
    complete = not (
        missing_from_primary
        or missing_from_independent
        or source_mismatches
        or ui_contract_mismatches
        or order_mismatch
        or visible_exclusions
        or not declared_complete
    )
    issue_codes: list[str] = []
    if missing_from_primary:
        issue_codes.append("INDEPENDENT_INVENTORY_FOUND_UNEXTRACTED_TARGETS")
    if missing_from_independent:
        issue_codes.append("INDEPENDENT_INVENTORY_MISSED_PRIMARY_TARGETS")
    if source_mismatches:
        issue_codes.append("INVENTORY_SOURCE_MISMATCH")
    if ui_contract_mismatches:
        issue_codes.append("INVENTORY_UI_COMPOSITION_MISMATCH")
    if order_mismatch:
        issue_codes.append("INVENTORY_ORDER_MISMATCH")
    if visible_exclusions:
        issue_codes.append("USER_VISIBLE_SCOPE_EXCLUDED")
    if not declared_complete:
        issue_codes.append("INVENTORY_NOT_DECLARED_COMPLETE")
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
        "primary_segments": len(primary_rows),
        "independent_segments": len(independent_rows),
        "missing_from_primary_count": len(missing_from_primary),
        "missing_from_independent_count": len(missing_from_independent),
        "source_mismatch_count": len(source_mismatches),
        "ui_composition_mismatch_count": len(ui_contract_mismatches),
        "order_mismatch": order_mismatch,
        "user_visible_exclusion_count": len(visible_exclusions),
        "static_complete": complete,
        "issue_codes": issue_codes,
    }


def save_inventory_attestation(
    work_dir: Path, primary_path: Path, independent_path: Path
) -> dict[str, Any]:
    attestation = verify_inventory_pair(primary_path, independent_path)
    _atomic_json(work_dir / ".game-l10n" / "inventory-attestation.json", attestation)
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
    payload = _inventory_document(primary_path)
    if state.get("primary_fingerprint") != inventory_fingerprint(payload):
        raise ConfigurationError("Primary inventory changed after independent verification")
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
    primary = _inventory_document(Path(inventory_state["primary_path"]))
    expected_ids = set(_segment_map(primary))
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
) -> dict[str, Any]:
    rows = list(results)
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
        final_verdict = any(
            attempt.stage in {"review", "review-confirm"}
            and attempt.status == "approved"
            and attempt.candidate_generation == item.candidate_generation
            for attempt in item.attempts
        )
        if (
            item.status == "reviewed"
            and item.acceptance_authority == "reviewer"
            and item.candidate_digest == stable_hash(item.text)
            and final_verdict
        ):
            reviewed.append(item)
    reused = [item for item in rows if item.metadata.get("candidate_origin") == "existing"]
    reused_reviewed = [item for item in reviewed if item.metadata.get("candidate_origin") == "existing"]
    source_preserved = [item for item in rows if item.status == "source_preserved"]
    failed = [item for item in rows if item.status == "failed"]
    review_denominator = len(rows) - len(source_preserved) - len(failed)
    review_complete = not review_required or (
        len(reviewed) == review_denominator and len(reused_reviewed) == len(reused)
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
    if source_preserved:
        issue_codes.append("SOURCE_PRESERVED_AFTER_ROUTE_EXHAUSTION")
    issue_codes.extend(runtime.get("issue_codes", []))
    target_completion = bool(
        alignment_complete and review_complete and not failed and not source_preserved
    )
    return {
        "schema": "parr0t.completion-attestation.v1",
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
            "failed_count": len(failed),
        },
        "runtime_coverage": runtime,
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
