"""Core-owned, opaque evidence for pending inventory constraint repair.

Inventory parsing and deterministic checks belong at preparation/sealing, never
in the status loop. A worker's categorical assertion is only a request to check.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .coverage import (
    _DISPLAY_SCOPE_VALIDATION_VERSION,
    _validate_scope_correction_history,
    verify_inventory_pair,
)
from .formats import read_segments
from .privacy import is_within
from .store import stable_hash
from .validators import preflight_constraint_issues


CONSTRAINT_SCHEMA = "parr0t.inventory-constraint-pending.v1"
CONSTRAINT_ACTION = "await_inventory_constraint_repair"
CONSTRAINT_CODE = "inventory_constraint_repair_required"
CONSTRAINT_REPAIR_ACTIONS = (
    "verify_inventory",
    "repair_cached_inventory_constraints",
    "correct_inventory_scope_with_evidence",
)

def has_inventory_constraint(codes: Any) -> bool:
    """Accept inventory diagnostics, never mixed provider or transport failure."""
    allowed = {
        "constraint_unsatisfiable", "task_incomplete", "inventory_not_declared_complete",
        "independent_inventory_missed_primary_targets",
        "independent_inventory_found_unextracted_targets", "inventory_order_mismatch",
        "inventory_ui_composition_mismatch",
    }
    return (
        isinstance(codes, list)
        and all(isinstance(code, str) and code in allowed for code in codes)
        and "constraint_unsatisfiable" in codes
    )



def artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inventory_constraint_binding(
    work_dir: Path,
    attestation: dict[str, Any],
    config: dict[str, Any],
    sdk_activation: dict[str, Any],
    runtime: dict[str, Any],
    evidence_paths: list[str],
    *,
    _digests: dict[Path, str] | None = None,
) -> str:
    """Bind current bytes, eligibility fingerprints and execution capability."""
    digests = {} if _digests is None else _digests

    def digest(path: Path) -> str:
        if path not in digests:
            digests[path] = artifact_digest(path)
        return digests[path]

    artifacts = {}
    for role in ("primary", "independent"):
        path = Path(str(attestation[f"{role}_path"])).resolve()
        if not is_within(path, work_dir) or not path.is_file():
            raise ValueError("inventory binding")
        artifacts[role] = {
            "path": str(path),
            "sha256": digest(path),
            "fingerprint": attestation[f"{role}_fingerprint"],
        }
    for index, relative in enumerate(evidence_paths):
        path = (work_dir / relative).resolve()
        if not is_within(path, work_dir) or not path.is_file():
            raise ValueError("inventory evidence binding")
        artifacts[f"evidence-{index}"] = {
            "path": str(path), "sha256": digest(path)
        }
    return stable_hash({
        "artifacts": artifacts,
        "config": config,
        "sdk_activation": sdk_activation,
        "native_runtime": runtime,
    })


def observe_inventory_constraints(
    work_dir: Path,
    attestation: dict[str, Any],
    config: dict[str, Any],
    sdk_activation: dict[str, Any],
    runtime: dict[str, Any],
    *,
    reuse_projection_baseline: bool = False,
) -> dict[str, Any]:
    """Verify constraint evidence; reuse byte-identical projection inputs only at preparation."""
    primary = Path(str(attestation["primary_path"])).resolve()
    independent = Path(str(attestation["independent_path"])).resolve()
    if not all(is_within(path, work_dir) for path in (primary, independent)):
        raise ValueError("inventory binding")
    if (
        reuse_projection_baseline
        and attestation.get("display_scope_validation_version") == _DISPLAY_SCOPE_VALIDATION_VERSION
        and type(attestation.get("projection_constraint_count")) is int
        and attestation["projection_constraint_count"] > 0
        and "INVENTORY_SCOPE_PROJECTION_REQUIRED" in attestation.get("issue_codes", ())
        and "constraint_inputs" in attestation
    ):
        try:
            return _observe_verified_constraints(
                work_dir, attestation, config, sdk_activation, runtime, check_bytes=True,
            )
        except (OSError, ValueError, TypeError, KeyError):
            # A changed or incomplete byte binding is not verification evidence.
            pass
    verified = verify_inventory_pair(primary, independent)
    _validate_scope_correction_history(work_dir, verified, attestation)
    if any(attestation.get(key) != value for key, value in verified.items()
           if key != "constraint_inputs"):
        raise ValueError("inventory attestation")
    return _observe_verified_constraints(
        work_dir, verified, config, sdk_activation, runtime,
        check_bytes=not verified["projection_constraint_count"],
    )


def _observe_verified_constraints(
    work_dir: Path,
    verified: dict[str, Any],
    config: dict[str, Any],
    sdk_activation: dict[str, Any],
    runtime: dict[str, Any],
    *,
    check_bytes: bool,
) -> dict[str, Any]:
    root = work_dir.resolve()
    primary = Path(verified["primary_path"]).resolve()
    independent = Path(verified["independent_path"]).resolve()
    inputs = verified["constraint_inputs"]
    if not isinstance(inputs, list) or not 2 <= len(inputs) <= 4:
        raise ValueError("inventory input binding")
    evidence_paths = []
    digests = {}
    for index, entry in enumerate(inputs):
        path = Path(entry["path"]).resolve()
        expected = entry["sha256"]
        if not path.is_relative_to(root) or re.fullmatch(r"[a-f0-9]{64}", expected) is None:
            raise ValueError("inventory input binding")
        if index < 2:
            if path != (primary, independent)[index] or path in digests:
                raise ValueError("inventory input binding")
        else:
            if path.parent not in {primary.parent, independent.parent} or path in {primary, independent}:
                raise ValueError("inventory evidence binding")
            evidence_paths.append(path.relative_to(root).as_posix())
        if path in digests and digests[path] != expected:
            raise ValueError("conflicting inventory input digests")
        digests[path] = expected

    evidence = []
    for issue_code in ("INVENTORY_SCOPE_CORRECTION_MISMATCH", "INVENTORY_SCOPE_PROJECTION_REQUIRED"):
        if issue_code in verified["issue_codes"]:
            evidence.append(stable_hash({
                "issue_code": issue_code,
                "primary_fingerprint": verified["primary_fingerprint"],
                "independent_fingerprint": verified["independent_fingerprint"],
            }))
    if not verified["projection_constraint_count"]:
        patterns = config.get("routing", {}).get("extra_protected_patterns", [])
        for role, path in (("primary", primary), ("independent", independent)):
            for segment in read_segments(path):
                if preflight_constraint_issues(segment, extra_patterns=patterns):
                    evidence.append(stable_hash({
                        "role": role,
                        "id": segment.id,
                        "source": segment.source,
                        "metadata": segment.metadata,
                    }))
    if check_bytes and any(artifact_digest(path) != expected for path, expected in digests.items()):
        raise ValueError("inventory input bytes changed")
    return {
        "input_binding": inventory_constraint_binding(
            work_dir, verified, config, sdk_activation, runtime, evidence_paths, _digests=digests,
        ),
        "evidence_binding": stable_hash(evidence),
        "constraint_count": len(evidence),
        "gap_score": verified["gap_score"],
        "evidence_paths": evidence_paths,
    }


def persistent_inventory_constraint(
    baseline: Any, current: dict[str, Any], attestation: dict[str, Any]
) -> bool:
    """Only identical, evidenced constraints with no verified progress hold."""
    return bool(
        isinstance(baseline, dict)
        and set(baseline) == set(current)
        and baseline == current
        and type(current.get("constraint_count")) is int
        and current["constraint_count"] > 0
        and attestation.get("status") == "incomplete"
        and attestation.get("static_complete") is False
        and attestation.get("reconciliation", {}).get("progress_state") == "unchanged"
    )
