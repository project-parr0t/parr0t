"""Core-owned, opaque evidence for pending inventory constraint repair.

Inventory parsing and deterministic checks belong at preparation/sealing, never
in the status loop. A worker's categorical assertion is only a request to check.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .coverage import _validate_scope_correction_history, verify_inventory_pair
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
) -> str:
    """Bind current bytes, eligibility fingerprints and execution capability."""
    artifacts = {}
    for role in ("primary", "independent"):
        path = Path(str(attestation[f"{role}_path"])).resolve()
        if not is_within(path, work_dir) or not path.is_file():
            raise ValueError("inventory binding")
        artifacts[role] = {
            "path": str(path),
            "sha256": artifact_digest(path),
            "fingerprint": attestation[f"{role}_fingerprint"],
        }
    for index, relative in enumerate(evidence_paths):
        path = (work_dir / relative).resolve()
        if not is_within(path, work_dir) or not path.is_file():
            raise ValueError("inventory evidence binding")
        artifacts[f"evidence-{index}"] = {
            "path": str(path), "sha256": artifact_digest(path)
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
) -> dict[str, Any]:
    """Recompute inventory truth and impossible render constraints once."""
    primary = Path(str(attestation["primary_path"])).resolve()
    independent = Path(str(attestation["independent_path"])).resolve()
    if not all(is_within(path, work_dir) for path in (primary, independent)):
        raise ValueError("inventory binding")
    verified = verify_inventory_pair(primary, independent)
    _validate_scope_correction_history(work_dir, verified, attestation)
    if any(attestation.get(key) != value for key, value in verified.items()):
        raise ValueError("inventory attestation")
    evidence = []
    evidence_paths = []
    for path in (primary, independent):
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        correction = payload.get("coverage", {}).get("scope_correction")
        if correction is not None:
            basename = correction["basis"]["file"]
            if not isinstance(basename, str) or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*\.json", basename
            ) is None:
                raise ValueError("inventory evidence binding")
            basis = (path.parent / basename).resolve()
            if basis.parent != path.parent or not is_within(basis, work_dir):
                raise ValueError("inventory evidence binding")
            evidence_paths.append(basis.relative_to(work_dir.resolve()).as_posix())
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
    return {
        "input_binding": inventory_constraint_binding(
            work_dir, attestation, config, sdk_activation, runtime, evidence_paths
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
