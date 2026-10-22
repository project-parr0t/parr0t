from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .control_plane import (
    control_plane_state,
    require_control_plane_entry,
    require_control_plane_ready,
    validate_control_envelope,
)
from .errors import ConfigurationError
from .privacy import is_within
from .store import stable_hash


DATA_PLANE_TASK_SCHEMA = "parr0t.data-plane-task.v1"
DATA_PLANE_RELAY_SCHEMA = "parr0t.data-plane-relay.v1"
DATA_PLANE_RECEIPT_SCHEMA = "parr0t.data-plane-receipt.v1"

DATA_PLANE_PROFILES: dict[str, dict[str, str]] = {
    "source_probe": {
        "stage": "discovery",
        "artifact": "game-l10n.json",
        "counterpart": "",
    },
    "primary_inventory": {
        "stage": "inventory",
        "artifact": "inputs/primary-inventory.json",
        "counterpart": "inputs/independent-inventory.json",
    },
    "independent_inventory": {
        "stage": "inventory",
        "artifact": "inputs/independent-inventory.json",
        "counterpart": "inputs/primary-inventory.json",
    },
    "inventory_reconciler": {
        "stage": "inventory",
        "artifact": ".game-l10n/inventory-attestation.json",
        "counterpart": "",
    },
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _relative(work_dir: Path, path: Path) -> str:
    resolved = path.resolve()
    if not is_within(resolved, work_dir):
        raise ConfigurationError("E_DATA_PLANE_TASK_PATH: task path left the workspace")
    return resolved.relative_to(work_dir).as_posix()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_data_plane_task(
    work_dir: Path,
    output_dir: Path,
    *,
    worker_role: str,
    config: dict[str, Any],
    sdk_activation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare one exact, private discovery or inventory worker task.

    The returned object is parent-safe. Source roots and private task instructions
    exist only inside the task file handed to a fresh isolated relay.
    """

    require_control_plane_ready(work_dir)
    require_control_plane_entry(work_dir)
    control_generation = int(control_plane_state(work_dir).get("generation", 0))
    profile = DATA_PLANE_PROFILES.get(worker_role)
    if profile is None:
        raise ConfigurationError("E_DATA_PLANE_TASK_ROLE: unsupported data-plane role")
    output_dir = output_dir.resolve()
    if not is_within(output_dir, work_dir):
        raise ConfigurationError("E_DATA_PLANE_TASK_PATH: output must stay inside the workspace")

    roots = [str(Path(item).resolve()) for item in config.get("game_roots", [])]
    if not roots:
        raise ConfigurationError("E_DATA_PLANE_TASK_SOURCE: no configured game root")
    if any(not Path(item).exists() for item in roots):
        raise ConfigurationError("E_DATA_PLANE_TASK_SOURCE: configured game root is missing")

    selection = config.get("sdk_selection", {})
    if worker_role == "source_probe":
        if not selection.get("inventory_fingerprint"):
            raise ConfigurationError(
                "E_DATA_PLANE_TASK_SDK_INVENTORY: run sdk-inventory before source discovery"
            )
    elif sdk_activation is None:
        raise ConfigurationError(
            "E_DATA_PLANE_TASK_SDK_ACTIVATION: inventory tasks require an active SDK"
        )

    artifact = (work_dir / profile["artifact"]).resolve()
    envelope = (output_dir / "control-envelope.json").resolve()
    counterpart = (
        (work_dir / profile["counterpart"]).resolve()
        if profile["counterpart"]
        else None
    )
    private_payload: dict[str, Any] = {
        "schema": DATA_PLANE_TASK_SCHEMA,
        "worker_role": worker_role,
        "stage": profile["stage"],
        "control_generation": control_generation,
        "workspace_root": str(work_dir.resolve()),
        "source_roots": roots,
        "sdk_root": str(Path(config.get("sdk_root", "")).resolve()),
        "sdk_inventory_fingerprint": selection.get("inventory_fingerprint", ""),
        "sdk_activation": sdk_activation or {},
        "output_artifact": _relative(work_dir, artifact),
        "output_envelope": _relative(work_dir, envelope),
        "counterpart_inventory_forbidden": (
            _relative(work_dir, counterpart) if counterpart is not None else ""
        ),
        "requirements": [
            "source_roots_are_read_only",
            "no_install_launch_ocr_or_publication",
            "write_private_artifacts_only_inside_workspace",
            "return_only_control_envelope_path_to_relay",
            "relay_returns_only_core_receipt_path_to_parent",
        ],
    }
    if worker_role == "inventory_reconciler":
        attestation_path = work_dir / ".game-l10n" / "inventory-attestation.json"
        if not attestation_path.is_file():
            raise ConfigurationError(
                "E_DATA_PLANE_TASK_INVENTORY: reconciliation requires an inventory attestation"
            )
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        reconciliation = attestation.get("reconciliation", {})
        private_payload["inventory_gate"] = {
            "primary_path": attestation.get("primary_path"),
            "independent_path": attestation.get("independent_path"),
            "primary_fingerprint": attestation.get("primary_fingerprint"),
            "independent_fingerprint": attestation.get("independent_fingerprint"),
            "gap_score": attestation.get("gap_score"),
            "issue_codes": attestation.get("issue_codes", []),
            "reconciliation_generation": reconciliation.get("generation", 0),
        }
    if worker_role in {"primary_inventory", "independent_inventory"}:
        private_payload["requirements"].extend(
            [
                "scan_source_once_using_selected_sdk_role_method",
                "do_not_read_or_derive_from_counterpart_inventory",
                "retain_unresolved_player_visible_candidates",
                "emit_inventory_comparison_key_for_method_specific_ids",
            ]
        )
    elif worker_role == "inventory_reconciler":
        private_payload["requirements"].extend(
            [
                "reuse_existing_inventory_scans_and_cached_evidence",
                "do_not_repeat_full_source_scans",
                "resolve_only_reported_gap_categories",
                "use_inventory_comparison_key_for_proven_method_specific_aliases",
                "retain_unresolved_player_visible_candidates",
                "run_core_verify_inventory_and_bind_current_attestation",
                "incomplete_with_measured_progress_requires_another_bounded_step",
            ]
        )
    else:
        private_payload["requirements"].extend(
            [
                "inspect_only_metadata_needed_for_locale_engine_and_version",
                "record_sdk_evaluation_and_selection_inside_workspace",
            ]
        )

    task_id = stable_hash(private_payload)
    task = {**private_payload, "task_id": task_id}
    task_path = output_dir / "task.json"
    _atomic_json(task_path, task)
    relay = {
        "schema": DATA_PLANE_RELAY_SCHEMA,
        "task_id": task_id,
        "task_path": _relative(work_dir, task_path),
        "task_sha256": _digest(task_path),
        "worker_role": worker_role,
        "stage": profile["stage"],
        "expected_envelope": _relative(work_dir, envelope),
        "max_workers": 1,
        "relay_authority": "exact_dispatch_only",
        "worker_history": "none",
        "worker_delegation_allowed": False,
    }
    relay_path = output_dir / "relay.json"
    _atomic_json(relay_path, relay)
    return {
        "schema": DATA_PLANE_RELAY_SCHEMA,
        "status": "ready",
        "worker_role": worker_role,
        "stage": profile["stage"],
        "relay_path": _relative(work_dir, relay_path),
        "task_id": task_id,
        "control_generation": control_generation,
    }


def seal_data_plane_task(work_dir: Path, relay_path: Path) -> dict[str, Any]:
    """Validate one exact data-plane result and emit a metadata-only receipt."""

    require_control_plane_ready(work_dir)
    require_control_plane_entry(work_dir)
    resolved_relay = relay_path.resolve()
    if not is_within(resolved_relay, work_dir) or not resolved_relay.is_file():
        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: relay must be a workspace file")
    relay = json.loads(resolved_relay.read_text(encoding="utf-8"))
    required_relay = {
        "schema",
        "task_id",
        "task_path",
        "task_sha256",
        "worker_role",
        "stage",
        "expected_envelope",
        "max_workers",
        "relay_authority",
        "worker_history",
        "worker_delegation_allowed",
    }
    if (
        not isinstance(relay, dict)
        or set(relay) != required_relay
        or relay.get("schema") != DATA_PLANE_RELAY_SCHEMA
        or relay.get("max_workers") != 1
        or relay.get("relay_authority") != "exact_dispatch_only"
        or relay.get("worker_history") != "none"
        or relay.get("worker_delegation_allowed") is not False
    ):
        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: relay contract is invalid")

    task_path = (work_dir / str(relay["task_path"])).resolve()
    if (
        not is_within(task_path, work_dir)
        or not task_path.is_file()
        or _digest(task_path) != relay["task_sha256"]
    ):
        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: bound task changed")
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task_identity = dict(task) if isinstance(task, dict) else {}
    task_identity.pop("task_id", None)
    if (
        not isinstance(task, dict)
        or task.get("schema") != DATA_PLANE_TASK_SCHEMA
        or task.get("task_id") != relay["task_id"]
        or stable_hash(task_identity) != relay["task_id"]
        or task.get("worker_role") != relay["worker_role"]
        or task.get("stage") != relay["stage"]
        or task.get("output_envelope") != relay["expected_envelope"]
        or task.get("control_generation")
        != int(control_plane_state(work_dir).get("generation", 0))
    ):
        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: task binding is invalid")

    envelope_path = (work_dir / str(relay["expected_envelope"])).resolve()
    result = validate_control_envelope(
        work_dir,
        envelope_path,
        expected_role=str(relay["worker_role"]),
        expected_stage=str(relay["stage"]),
    )
    expected_artifact = str(task["output_artifact"])
    if expected_artifact not in result["artifact_paths"]:
        raise ConfigurationError(
            "E_DATA_PLANE_RESULT_INVALID: envelope did not bind the required artifact"
        )
    artifact_path = (work_dir / expected_artifact).resolve()
    if result["worker_role"] in {"primary_inventory", "independent_inventory"}:
        inventory = json.loads(artifact_path.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(inventory, dict)
            or inventory.get("schema") != "parr0t.source-inventory.v1"
        ):
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: inventory artifact schema is invalid"
            )
        if result["status"] == "success" and (
            inventory.get("coverage", {}).get("declared_complete") is not True
        ):
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: successful inventory is not declared complete"
            )
    elif result["worker_role"] == "source_probe" and result["status"] == "success":
        config = json.loads(artifact_path.read_text(encoding="utf-8"))
        selection = config.get("sdk_selection", {})
        if (
            selection.get("status") != "selected"
            or selection.get("inventory_fingerprint")
            != task.get("sdk_inventory_fingerprint")
        ):
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: source probe did not record SDK selection"
            )
    elif result["worker_role"] == "inventory_reconciler":
        attestation = json.loads(artifact_path.read_text(encoding="utf-8"))
        prior_generation = int(
            task.get("inventory_gate", {}).get("reconciliation_generation", 0)
        )
        current_generation = int(
            attestation.get("reconciliation", {}).get("generation", 0)
        )
        if current_generation <= prior_generation:
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: reconciliation did not run core verification"
            )
        if result["status"] == "success" and (
            attestation.get("status") != "static_verified"
            or attestation.get("static_complete") is not True
        ):
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: successful reconciliation is incomplete"
            )
    receipt = {
        "schema": DATA_PLANE_RECEIPT_SCHEMA,
        "task_id": relay["task_id"],
        "worker_role": result["worker_role"],
        "stage": result["stage"],
        "status": result["status"],
        "counts": result["counts"],
        "issue_codes": result["issue_codes"],
        "artifact_paths": result["artifact_paths"],
        "queue_transitions": result["queue_transitions"],
        "control_generation": int(control_plane_state(work_dir).get("generation", 0)),
    }
    receipt_path = resolved_relay.with_name("receipt.json")
    _atomic_json(receipt_path, receipt)
    return {
        "schema": DATA_PLANE_RECEIPT_SCHEMA,
        "status": receipt["status"],
        "worker_role": receipt["worker_role"],
        "stage": receipt["stage"],
        "counts": receipt["counts"],
        "issue_codes": receipt["issue_codes"],
        "receipt_path": _relative(work_dir, receipt_path),
    }
