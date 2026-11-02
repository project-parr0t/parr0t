from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .context_analysis import (
    CONTEXT_PREFLIGHT_POLICY_VERSION,
    CONTEXT_PREFLIGHT_SCHEMA,
    CONTEXT_STATE_SCHEMA,
    build_context_preflight,
    context_chunks,
    context_contract_fingerprints,
    context_attestation_for_mode,
    infer_context_mode,
    infer_segment_domains,
    merge_context_parts,
    require_complete_context,
    semantic_context_segments,
    validate_context_part,
)
from .control_plane import (
    control_plane_state,
    require_control_plane_entry,
    require_control_plane_ready,
    validate_control_envelope,
)
from .coverage import require_inventory_attestation
from .errors import ConfigurationError, ProviderError
from .formats import read_segments
from .image_localization import (
    SCOPE_PATH as IMAGE_SCOPE_PATH,
    public_image_localization_state,
    require_image_localization_requested,
    verify_image_localization_results,
    verify_image_ui_inventory,
)
from .language_policy import language_binding
from .models import ContextMode, ContextPack, Segment
from .privacy import is_within
from .store import stable_hash


DATA_PLANE_TASK_SCHEMA = "parr0t.data-plane-task.v1"
LEGACY_DATA_PLANE_RELAY_SCHEMA = "parr0t.data-plane-relay.v1"
LEGACY_DATA_PLANE_RELAY_V2_SCHEMA = "parr0t.data-plane-relay.v2"
LEGACY_DATA_PLANE_RELAY_V3_SCHEMA = "parr0t.data-plane-relay.v3"
DATA_PLANE_RELAY_SCHEMA = "parr0t.data-plane-relay.v4"
LEGACY_DATA_PLANE_RELAY_CONTRACT_SCHEMA = "parr0t.data-plane-relay-contract.v1"
LEGACY_DATA_PLANE_RELAY_CONTRACT_V2_SCHEMA = "parr0t.data-plane-relay-contract.v2"
DATA_PLANE_RELAY_CONTRACT_SCHEMA = "parr0t.data-plane-relay-contract.v3"
LEGACY_DATA_PLANE_RECEIPT_SCHEMA = "parr0t.data-plane-receipt.v1"
DATA_PLANE_RECEIPT_SCHEMA = "parr0t.data-plane-receipt.v2"
DATA_PLANE_EXECUTION_PLAN_SCHEMA = "parr0t.data-plane-execution-plan.v1"
DATA_PLANE_RELAY_LIFECYCLE_SCHEMA = "parr0t.data-plane-relay-lifecycle.v1"
CONTEXT_WORK_MANIFEST_SCHEMA = "parr0t.context-work-manifest.v1"
CONTEXT_CHUNK_CLAIM_SCHEMA = "parr0t.context-chunk-claim.v1"
CONTEXT_CHUNK_RESULT_SCHEMA = "parr0t.context-chunk-result.v1"
CONTEXT_FRAGMENT_SCHEMA = "parr0t.context-fragment.v1"
CONTEXT_RESULT_ISSUE_CODES = {
    "fallback_required",
    "malformed_response",
    "provider_error",
    "rate_limited",
    "quota_exhausted",
    "authentication_error",
    "timeout",
    "capacity_error",
    "worker_error",
}

MIN_CONTEXT_CHUNK_CHARS = 2048
MAX_CONTEXT_WORKERS = 6


def _context_row(segment: Segment) -> dict[str, Any]:
    return {
        **asdict(segment),
        "context_domains": sorted(infer_segment_domains(segment)),
    }


def _segment_from_context_row(row: dict[str, Any]) -> Segment:
    return Segment(
        id=str(row["id"]),
        source=str(row["source"]),
        speaker=str(row.get("speaker", "")),
        scene=str(row.get("scene", "")),
        previous=str(row.get("previous", "")),
        next=str(row.get("next", "")),
        metadata=dict(row.get("metadata", {})),
    )


def _context_binding(
    segments: list[Segment],
    *,
    source_inventory_fingerprint: str,
    resolved_context_mode: str,
    contract_fingerprints: dict[str, Any],
) -> dict[str, Any]:
    binding = {
        "source_inventory_fingerprint": source_inventory_fingerprint,
        "source_digest": stable_hash([asdict(item) for item in segments]),
        "ordered_evidence_ids": [item.id for item in segments],
        "context_preflight_policy_version": CONTEXT_PREFLIGHT_POLICY_VERSION,
        "context_mode": resolved_context_mode,
        "context_contract_fingerprints": contract_fingerprints,
    }
    return {**binding, "binding_id": stable_hash(binding)}


def _context_fragment_index(work_dir: Path) -> dict[str, list[tuple[Path, dict[str, Any]]]]:
    root = work_dir / ".game-l10n" / "context-progress" / "fragments"
    indexed: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    if not root.is_dir():
        return indexed
    for path in sorted(root.glob("*/*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        binding_id = payload.get("binding_id") if isinstance(payload, dict) else None
        if isinstance(binding_id, str) and binding_id:
            indexed.setdefault(binding_id, []).append((path, payload))
    return indexed


def _validate_context_fragment(
    payload: dict[str, Any],
    *,
    binding: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "binding_id",
        "result_id",
        "binding",
        "provider",
        "context_part",
    }:
        raise ValueError("invalid context fragment")
    context_part = payload.get("context_part")
    provider = payload.get("provider")
    if (
        payload.get("schema") != CONTEXT_FRAGMENT_SCHEMA
        or payload.get("binding_id") != binding["binding_id"]
        or payload.get("binding") != binding
        or not isinstance(provider, str)
        or not provider
        or not isinstance(context_part, dict)
        or payload.get("result_id")
        != stable_hash(
            {"binding": binding, "provider": provider, "context_part": context_part}
        )
    ):
        raise ValueError("invalid context fragment")
    allowed_ids = set(binding["ordered_evidence_ids"])
    validate_context_part(
        context_part,
        mode=str(binding["context_mode"]),
        allowed_evidence_ids=allowed_ids,
    )
    segments = [_segment_from_context_row(row) for row in rows]
    pack = merge_context_parts([context_part], str(binding["context_mode"]))
    attestation = context_attestation_for_mode(
        segments,
        pack,
        context_mode=str(binding["context_mode"]),
        analyzer_routes=[{"name": provider, "type": "native_collaboration"}],
        preflight=build_context_preflight(segments),
    )
    if pack.context_mode != attestation["context_mode"]:
        raise ValueError("context fragment mode mismatch")
    require_complete_context(attestation)
    return payload


def _find_context_fragment(
    index: dict[str, list[tuple[Path, dict[str, Any]]]],
    *,
    binding: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[Path, dict[str, Any]] | None:
    for path, payload in index.get(str(binding["binding_id"]), []):
        try:
            return path, _validate_context_fragment(
                payload, binding=binding, rows=rows
            )
        except (ValueError, TypeError, KeyError, ProviderError):
            continue
    return None


def _write_context_manifest(
    work_dir: Path,
    *,
    segments: list[Segment],
    preflight: dict[str, Any],
    source_inventory_fingerprint: str,
    context_mode: str,
    chunk_chars: int,
    contract_fingerprints: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    semantic = semantic_context_segments(segments, preflight)
    resolved_mode = (
        infer_context_mode(segments)
        if context_mode == ContextMode.AUTO.value
        else context_mode
    )
    by_id = {item.id: item for item in semantic}
    ordered_ids = [item.id for item in semantic]
    positions = {item_id: index for index, item_id in enumerate(ordered_ids)}
    fragment_index = _context_fragment_index(work_dir)
    reusable: list[tuple[int, int, dict[str, Any], list[dict[str, Any]], Path]] = []
    for binding_id, candidates in fragment_index.items():
        for _, payload in candidates:
            binding = payload.get("binding", {}) if isinstance(payload, dict) else {}
            ids = (
                binding.get("ordered_evidence_ids", [])
                if isinstance(binding, dict)
                else []
            )
            if (
                not isinstance(ids, list)
                or not ids
                or any(item_id not in by_id for item_id in ids)
                or [positions[item_id] for item_id in ids]
                != list(range(positions[ids[0]], positions[ids[0]] + len(ids)))
            ):
                continue
            chunk = [by_id[item_id] for item_id in ids]
            expected_binding = _context_binding(
                chunk,
                source_inventory_fingerprint=source_inventory_fingerprint,
                resolved_context_mode=resolved_mode,
                contract_fingerprints=contract_fingerprints,
            )
            if binding_id != expected_binding["binding_id"]:
                continue
            rows = [_context_row(item) for item in chunk]
            found = _find_context_fragment(
                fragment_index, binding=expected_binding, rows=rows
            )
            if found is not None:
                reusable.append(
                    (positions[ids[0]], -len(ids), expected_binding, rows, found[0])
                )
                break
    covered: set[str] = set()
    units: list[dict[str, Any]] = []
    for source_index, _, binding, rows, fragment_path in sorted(reusable):
        ids = set(binding["ordered_evidence_ids"])
        if ids & covered:
            continue
        covered.update(ids)
        units.append(
            {
                "source_index": source_index,
                "binding": binding,
                "binding_id": binding["binding_id"],
                "rows": rows,
                "initial_status": "complete",
                "fragment_path": _relative(work_dir, fragment_path),
            }
        )
    pending_runs: list[list[Segment]] = []
    current: list[Segment] = []
    for segment in semantic:
        if segment.id in covered:
            if current:
                pending_runs.append(current)
                current = []
        else:
            current.append(segment)
    if current:
        pending_runs.append(current)
    for run in pending_runs:
        for chunk in context_chunks(run, chunk_chars):
            binding = _context_binding(
                chunk,
                source_inventory_fingerprint=source_inventory_fingerprint,
                resolved_context_mode=resolved_mode,
                contract_fingerprints=contract_fingerprints,
            )
            units.append(
                {
                    "source_index": positions[chunk[0].id],
                    "binding": binding,
                    "binding_id": binding["binding_id"],
                    "rows": [_context_row(item) for item in chunk],
                    "initial_status": "pending",
                    "fragment_path": "",
                }
            )
    units.sort(key=lambda item: int(item["source_index"]))
    unsigned = {
        "schema": CONTEXT_WORK_MANIFEST_SCHEMA,
        "source_inventory_fingerprint": source_inventory_fingerprint,
        "source_segments_digest": preflight["source_segments_digest"],
        "preflight_digest": stable_hash(preflight),
        "preflight_policy_version": CONTEXT_PREFLIGHT_POLICY_VERSION,
        "configured_context_mode": context_mode,
        "resolved_context_mode": resolved_mode,
        "context_contract_fingerprints": contract_fingerprints,
        "chunk_chars": chunk_chars,
        "source_records": len(segments),
        "semantic_records": len(semantic),
        "deterministic_records": len(segments) - len(semantic),
        "chunks": units,
    }
    manifest = {**unsigned, "manifest_id": stable_hash(unsigned)}
    path = (
        work_dir
        / ".game-l10n"
        / "context-progress"
        / "manifests"
        / f"{manifest['manifest_id']}.json"
    )
    _atomic_json(path, manifest)
    return path, manifest

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
    "context_analyzer": {
        "stage": "context",
        "artifact": "context/context-pack.json",
        "counterpart": "",
    },
    "image_ui_inventory": {
        "stage": "image_inventory",
        "artifact": "inputs/image-ui-inventory.json",
        "counterpart": "",
    },
    "image_localizer": {
        "stage": "image_localization",
        "artifact": "translations/image-ui-results.json",
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


def _load_context_task(work_dir: Path, task_path: Path) -> dict[str, Any]:
    resolved = task_path.resolve()
    if not is_within(resolved, work_dir) or not resolved.is_file():
        raise ConfigurationError("E_CONTEXT_TASK_INVALID: task must be a workspace file")
    try:
        task = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("E_CONTEXT_TASK_INVALID: task is unreadable") from exc
    identity = dict(task) if isinstance(task, dict) else {}
    identity.pop("task_id", None)
    if (
        not isinstance(task, dict)
        or task.get("schema") != DATA_PLANE_TASK_SCHEMA
        or task.get("worker_role") != "context_analyzer"
        or task.get("task_id") != stable_hash(identity)
        or task.get("control_generation")
        != int(control_plane_state(work_dir).get("generation", 0))
    ):
        raise ConfigurationError("E_CONTEXT_TASK_INVALID: task binding is invalid")
    manifest_path = (work_dir / str(task.get("input_context_manifest", ""))).resolve()
    if (
        not is_within(manifest_path, work_dir)
        or not manifest_path.is_file()
        or _digest(manifest_path) != task.get("context_manifest_sha256")
    ):
        raise ConfigurationError("E_CONTEXT_TASK_INVALID: context manifest is unavailable")
    return task


def _load_context_manifest(
    work_dir: Path, task: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    path = (work_dir / str(task["input_context_manifest"])).resolve()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("E_CONTEXT_MANIFEST_INVALID: manifest is unreadable") from exc
    identity = dict(manifest) if isinstance(manifest, dict) else {}
    manifest_id = identity.pop("manifest_id", None)
    chunks = manifest.get("chunks") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != CONTEXT_WORK_MANIFEST_SCHEMA
        or manifest_id != stable_hash(identity)
        or manifest_id != task.get("context_manifest_id")
        or not isinstance(chunks, list)
    ):
        raise ConfigurationError("E_CONTEXT_MANIFEST_INVALID: manifest binding is invalid")
    return path, manifest


def _context_progress(
    work_dir: Path, manifest: dict[str, Any]
) -> tuple[list[tuple[dict[str, Any], Path, dict[str, Any]]], list[dict[str, Any]]]:
    index = _context_fragment_index(work_dir)
    complete: list[tuple[dict[str, Any], Path, dict[str, Any]]] = []
    pending: list[dict[str, Any]] = []
    for chunk in manifest["chunks"]:
        if not isinstance(chunk, dict):
            raise ConfigurationError("E_CONTEXT_MANIFEST_INVALID: chunk shape is invalid")
        binding = chunk.get("binding")
        rows = chunk.get("rows")
        if (
            not isinstance(binding, dict)
            or chunk.get("binding_id") != binding.get("binding_id")
            or not isinstance(rows, list)
            or not rows
        ):
            raise ConfigurationError("E_CONTEXT_MANIFEST_INVALID: chunk binding is invalid")
        found = _find_context_fragment(index, binding=binding, rows=rows)
        if found is None:
            pending.append(chunk)
        else:
            complete.append((chunk, found[0], found[1]))
    return complete, pending


def _context_safe_counts(
    work_dir: Path, task: dict[str, Any]
) -> dict[str, int]:
    _, manifest = _load_context_manifest(work_dir, task)
    complete, pending = _context_progress(work_dir, manifest)
    return {
        "total": len(manifest["chunks"]),
        "success": len(complete),
        "error": 0,
        "pending": len(pending),
    }


def claim_context_chunk(
    work_dir: Path, task_path: Path, *, slot: int
) -> dict[str, Any]:
    """Atomically claim one unfinished native context chunk for an isolated worker."""

    require_control_plane_ready(work_dir)
    require_control_plane_entry(work_dir)
    task = _load_context_task(work_dir, task_path)
    max_workers = int(task.get("context_parallelism", {}).get("max_workers", 0))
    if isinstance(slot, bool) or slot < 1 or slot > max_workers:
        raise ConfigurationError("E_CONTEXT_CLAIM_INVALID: slot is not bound to this task")
    _, manifest = _load_context_manifest(work_dir, task)
    claim_root = (
        work_dir
        / ".game-l10n"
        / "context-progress"
        / "claims"
        / str(task["task_id"])
    )
    slot_state_path = claim_root / "slots" / f"slot-{slot}.json"
    slot_state = {"cursor": -1, "active_claim_path": ""}
    if slot_state_path.is_file():
        try:
            candidate = json.loads(slot_state_path.read_text(encoding="utf-8"))
            if (
                isinstance(candidate, dict)
                and isinstance(candidate.get("cursor"), int)
                and not isinstance(candidate.get("cursor"), bool)
                and isinstance(candidate.get("active_claim_path"), str)
            ):
                slot_state = candidate
        except (OSError, json.JSONDecodeError):
            pass
    active_relative = str(slot_state["active_claim_path"])
    if active_relative:
        active_path = (work_dir / active_relative).resolve()
        if is_within(active_path, work_dir) and active_path.is_file():
            try:
                existing = json.loads(active_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if existing.get("slot") == slot and existing.get("status") == "claimed":
                return {
                    "status": "claimed",
                    "slot": slot,
                    "claim_path": active_relative,
                    "record_count": len(existing.get("rows", [])),
                }
    chunks = list(manifest["chunks"])
    start = max(0, int(slot_state["cursor"]) + 1)
    for ordinal in range(start, len(chunks)):
        chunk = chunks[ordinal]
        if chunk.get("initial_status") == "complete":
            continue
        claim_dir = claim_root / str(chunk["binding_id"])
        try:
            claim_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        unsigned = {
            "schema": CONTEXT_CHUNK_CLAIM_SCHEMA,
            "task_id": task["task_id"],
            "manifest_id": manifest["manifest_id"],
            "binding_id": chunk["binding_id"],
            "binding": chunk["binding"],
            "source_index": chunk["source_index"],
            "slot": slot,
            "provider": task["context_route"],
            "rows": chunk["rows"],
        }
        claim = {
            **unsigned,
            "claim_id": stable_hash(unsigned),
            "status": "claimed",
            "issue_code": "",
            "fragment_path": "",
        }
        claim_path = claim_dir / "claim.json"
        _atomic_json(claim_path, claim)
        _atomic_json(
            slot_state_path,
            {
                "cursor": ordinal,
                "active_claim_path": _relative(work_dir, claim_path),
            },
        )
        return {
            "status": "claimed",
            "slot": slot,
            "claim_path": _relative(work_dir, claim_path),
            "record_count": len(chunk["rows"]),
        }
    _atomic_json(
        slot_state_path,
        {"cursor": len(chunks), "active_claim_path": ""},
    )
    counts = _context_safe_counts(work_dir, task)
    return {"status": "exhausted", "slot": slot, "claim_path": "", **counts}


def commit_context_fragment(
    work_dir: Path,
    task_path: Path,
    claim_path: Path,
    result_path: Path,
) -> dict[str, Any]:
    """Validate one exact worker result and commit a reusable private fragment."""

    require_control_plane_ready(work_dir)
    require_control_plane_entry(work_dir)
    task = _load_context_task(work_dir, task_path)
    resolved_claim = claim_path.resolve()
    resolved_result = result_path.resolve()
    if (
        not is_within(resolved_claim, work_dir)
        or not resolved_claim.is_file()
        or not is_within(resolved_result, work_dir)
        or not resolved_result.is_file()
    ):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: claim or result is unavailable")
    try:
        claim = json.loads(resolved_claim.read_text(encoding="utf-8"))
        result = json.loads(resolved_result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: result is unreadable") from exc
    claim_unsigned = {
        key: claim.get(key)
        for key in (
            "schema",
            "task_id",
            "manifest_id",
            "binding_id",
            "binding",
            "source_index",
            "slot",
            "provider",
            "rows",
        )
    }
    if (
        not isinstance(claim, dict)
        or claim.get("schema") != CONTEXT_CHUNK_CLAIM_SCHEMA
        or claim.get("task_id") != task["task_id"]
        or claim.get("claim_id") != stable_hash(claim_unsigned)
        or claim.get("status") != "claimed"
    ):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: claim is not active")
    if not isinstance(result, dict) or set(result) != {
        "schema",
        "claim_id",
        "status",
        "context_part",
        "issue_code",
    }:
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: result shape is invalid")
    status = result.get("status")
    issue_code = result.get("issue_code")
    context_part = result.get("context_part")
    if result.get("schema") != CONTEXT_CHUNK_RESULT_SCHEMA or result.get(
        "claim_id"
    ) != claim.get("claim_id"):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: result binding is invalid")
    if status != "success":
        if (
            status not in {"fallback_required", "error"}
            or issue_code not in CONTEXT_RESULT_ISSUE_CODES
            or context_part != {}
        ):
            raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: failure is not categorical")
        _atomic_json(
            resolved_claim,
            {**claim, "status": "failed", "issue_code": issue_code},
        )
        return {
            "status": status,
            "slot": claim["slot"],
            "record_count": len(claim["rows"]),
            "issue_code": issue_code,
        }
    if issue_code != "" or not isinstance(context_part, dict):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: success result is invalid")
    binding = claim["binding"]
    provider = str(claim["provider"])
    fragment = {
        "schema": CONTEXT_FRAGMENT_SCHEMA,
        "binding_id": claim["binding_id"],
        "result_id": stable_hash(
            {"binding": binding, "provider": provider, "context_part": context_part}
        ),
        "binding": binding,
        "provider": provider,
        "context_part": context_part,
    }
    try:
        _validate_context_fragment(fragment, binding=binding, rows=claim["rows"])
    except (ValueError, TypeError, KeyError, ProviderError) as exc:
        raise ConfigurationError(
            "E_CONTEXT_FRAGMENT_INVALID: semantic result failed deterministic validation"
        ) from exc
    fragment_path = (
        work_dir
        / ".game-l10n"
        / "context-progress"
        / "fragments"
        / str(claim["binding_id"])
        / f"{fragment['result_id']}.json"
    )
    _atomic_json(fragment_path, fragment)
    _atomic_json(
        resolved_claim,
        {
            **claim,
            "status": "committed",
            "issue_code": "",
            "fragment_path": _relative(work_dir, fragment_path),
        },
    )
    return {
        "status": "committed",
        "slot": claim["slot"],
        "record_count": len(claim["rows"]),
    }


def _expected_context_pack_from_fragments(
    work_dir: Path, task: dict[str, Any]
) -> tuple[ContextPack, list[tuple[dict[str, Any], Path, dict[str, Any]]], dict[str, Any]]:
    _, manifest = _load_context_manifest(work_dir, task)
    complete, pending = _context_progress(work_dir, manifest)
    if pending:
        raise ConfigurationError(
            "E_CONTEXT_FINALIZE_INCOMPLETE: context fragments are still pending"
        )
    complete.sort(key=lambda item: int(item[0]["source_index"]))
    pack = merge_context_parts(
        [item[2]["context_part"] for item in complete],
        str(manifest["resolved_context_mode"]),
    )
    return pack, complete, manifest


def _context_fragment_routes(
    complete: list[tuple[dict[str, Any], Path, dict[str, Any]]],
    fallback_route: str,
) -> list[dict[str, Any]]:
    providers = list(
        dict.fromkeys(
            str(item[2]["provider"])
            for item in complete
            if str(item[2].get("provider", ""))
        )
    )
    if not providers and fallback_route:
        providers = [fallback_route]
    return [
        {"name": provider, "type": "native_collaboration"}
        for provider in providers
    ]


def finalize_context_fragments(
    work_dir: Path, task_path: Path
) -> dict[str, Any]:
    """Deterministically merge committed fragments and write the bound envelope."""

    require_control_plane_ready(work_dir)
    require_control_plane_entry(work_dir)
    task = _load_context_task(work_dir, task_path)
    _, manifest = _load_context_manifest(work_dir, task)
    complete, pending = _context_progress(work_dir, manifest)
    envelope_path = (work_dir / str(task["output_envelope"])).resolve()
    if pending:
        envelope = {
            "schema": "parr0t.control-envelope.v2",
            "worker_role": "context_analyzer",
            "stage": "context",
            "status": "partial",
            "target_ids": [],
            "counts": {
                "total": len(manifest["chunks"]),
                "success": len(complete),
                "error": 0,
                "pending": len(pending),
            },
            "issue_codes": ["context_execution_incomplete"],
            "artifact_paths": [],
            "queue_transitions": [],
            "route_evidence_path": "",
        }
        _atomic_json(envelope_path, envelope)
        return {"status": "partial", **envelope["counts"]}
    pack, complete, manifest = _expected_context_pack_from_fragments(work_dir, task)
    source_path = (work_dir / str(task["input_inventory"])).resolve()
    segments = read_segments(source_path)
    expected_preflight = build_context_preflight(segments)
    attestation = context_attestation_for_mode(
        segments,
        pack,
        context_mode=str(task.get("context_mode", ContextMode.AUTO.value)),
        analyzer_routes=_context_fragment_routes(
            complete, str(task.get("context_route", ""))
        ),
        preflight=expected_preflight,
    )
    if pack.context_mode != attestation["context_mode"]:
        raise ConfigurationError("E_CONTEXT_FINALIZE_INVALID: context mode changed")
    try:
        require_complete_context(attestation)
    except ProviderError as exc:
        raise ConfigurationError(
            "E_CONTEXT_FINALIZE_INVALID: merged context is incomplete"
        ) from exc
    artifact_path = (work_dir / str(task["output_artifact"])).resolve()
    if not is_within(artifact_path, work_dir):
        raise ConfigurationError("E_CONTEXT_FINALIZE_INVALID: output left the workspace")
    _atomic_json(artifact_path, asdict(pack))
    envelope = {
        "schema": "parr0t.control-envelope.v2",
        "worker_role": "context_analyzer",
        "stage": "context",
        "status": "success",
        "target_ids": [],
        "counts": {
            "total": len(manifest["chunks"]),
            "success": len(complete),
            "error": 0,
            "pending": 0,
        },
        "issue_codes": [],
        "artifact_paths": [str(task["output_artifact"])],
        "queue_transitions": [],
        "route_evidence_path": "",
    }
    _atomic_json(envelope_path, envelope)
    return {"status": "success", **envelope["counts"]}


def _with_state_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "state_fingerprint": stable_hash(payload)}


def _new_relay_lifecycle(
    *,
    task_id: str,
    control_generation: int,
    required_worker_slots: list[int],
    finalizer_required: bool,
) -> dict[str, Any]:
    return _with_state_fingerprint(
        {
            "schema": DATA_PLANE_RELAY_LIFECYCLE_SCHEMA,
            "task_id": task_id,
            "control_generation": control_generation,
            "required_worker_slots": required_worker_slots,
            "worker_states": {str(slot): "pending" for slot in required_worker_slots},
            "finalizer_required": finalizer_required,
            "finalizer_state": "pending" if finalizer_required else "not_required",
            "event_count": 0,
        }
    )


def _execution_plan(
    *,
    worker_role: str,
    max_workers: int,
    lifecycle_path: str,
) -> dict[str, Any]:
    tracking_required = worker_role == "context_analyzer"
    required_worker_slots = list(range(1, max_workers + 1))
    finalizer_required = tracking_required
    finalizer_only = tracking_required and max_workers == 0
    return {
        "schema": DATA_PLANE_EXECUTION_PLAN_SCHEMA,
        "tracking_required": tracking_required,
        "strategy": (
            "context_finalizer_only"
            if finalizer_only
            else "fixed_slot_wave_then_finalizer"
            if tracking_required
            else "single_worker"
        ),
        "required_worker_slots": required_worker_slots,
        "worker_start_policy": (
            "no_semantic_workers_pending"
            if finalizer_only
            else
            "start_all_required_slots_once_before_any_worker_end"
            if max_workers > 1
            else "start_required_slot_once"
        ),
        "worker_terminal_policy": (
            "no_semantic_workers_pending"
            if finalizer_only
            else "wait_for_every_required_slot_to_end"
        ),
        "implicit_timeout_policy": "forbidden",
        "timeout_seconds": None,
        "finalizer_required": finalizer_required,
        "finalizer_start_policy": (
            "start_immediately_without_semantic_workers"
            if finalizer_only
            else "after_every_required_worker_slot_ended"
            if finalizer_required
            else "not_applicable"
        ),
        "completion_policy": (
            "all_required_slots_ended_and_required_finalizer_ended"
        ),
        "lifecycle_path": lifecycle_path,
        "record_event_command": (
            "game-l10n record-data-plane-relay-event --workspace <workspace> "
            "--relay <relay-path> --event <event> [--slot <slot>]"
            if tracking_required
            else ""
        ),
    }


def _valid_execution_plan(relay: dict[str, Any]) -> bool:
    plan = relay.get("execution_plan")
    if not isinstance(plan, dict) or set(plan) != {
        "schema",
        "tracking_required",
        "strategy",
        "required_worker_slots",
        "worker_start_policy",
        "worker_terminal_policy",
        "implicit_timeout_policy",
        "timeout_seconds",
        "finalizer_required",
        "finalizer_start_policy",
        "completion_policy",
        "lifecycle_path",
        "record_event_command",
    }:
        return False
    max_workers = relay.get("max_workers")
    tracking_required = relay.get("worker_role") == "context_analyzer"
    finalizer_only = tracking_required and max_workers == 0
    required_slots = list(range(1, max_workers + 1)) if isinstance(max_workers, int) else []
    return (
        plan.get("schema") == DATA_PLANE_EXECUTION_PLAN_SCHEMA
        and plan.get("tracking_required") is tracking_required
        and plan.get("strategy")
        == (
            "context_finalizer_only"
            if finalizer_only
            else "fixed_slot_wave_then_finalizer"
            if tracking_required
            else "single_worker"
        )
        and plan.get("required_worker_slots") == required_slots
        and plan.get("worker_start_policy")
        == (
            "no_semantic_workers_pending"
            if finalizer_only
            else
            "start_all_required_slots_once_before_any_worker_end"
            if isinstance(max_workers, int) and max_workers > 1
            else "start_required_slot_once"
        )
        and plan.get("worker_terminal_policy")
        == (
            "no_semantic_workers_pending"
            if finalizer_only
            else "wait_for_every_required_slot_to_end"
        )
        and plan.get("implicit_timeout_policy") == "forbidden"
        and plan.get("timeout_seconds") is None
        and plan.get("finalizer_required") is tracking_required
        and plan.get("finalizer_start_policy")
        == (
            "start_immediately_without_semantic_workers"
            if finalizer_only
            else "after_every_required_worker_slot_ended"
            if tracking_required
            else "not_applicable"
        )
        and plan.get("completion_policy")
        == "all_required_slots_ended_and_required_finalizer_ended"
        and isinstance(plan.get("lifecycle_path"), str)
        and (
            bool(plan.get("lifecycle_path"))
            if tracking_required
            else plan.get("lifecycle_path") == ""
        )
        and plan.get("record_event_command")
        == (
            "game-l10n record-data-plane-relay-event --workspace <workspace> "
            "--relay <relay-path> --event <event> [--slot <slot>]"
            if tracking_required
            else ""
        )
    )


def _safe_execution_summary(value: Any) -> dict[str, Any] | None:
    fields = {
        "tracking_required",
        "required_workers",
        "started_workers",
        "terminal_workers",
        "unstarted_slots",
        "unterminated_slots",
        "finalizer_required",
        "finalizer_started",
        "finalizer_terminal",
        "implicit_timeout_allowed",
        "complete",
    }
    if not isinstance(value, dict) or set(value) != fields:
        return None
    integer_fields = ("required_workers", "started_workers", "terminal_workers")
    if any(
        not isinstance(value.get(key), int) or isinstance(value.get(key), bool)
        for key in integer_fields
    ):
        return None
    required = value["required_workers"]
    started = value["started_workers"]
    terminal = value["terminal_workers"]
    unstarted = value.get("unstarted_slots")
    unterminated = value.get("unterminated_slots")
    if value.get("tracking_required") is False:
        if (
            required < 1
            or required > MAX_CONTEXT_WORKERS
            or started != 0
            or terminal != 0
            or unstarted != []
            or unterminated != []
            or value.get("finalizer_required") is not False
            or value.get("finalizer_started") is not False
            or value.get("finalizer_terminal") is not False
            or value.get("implicit_timeout_allowed") is not False
            or value.get("complete") is not True
        ):
            return None
        return dict(value)
    if (
        value["tracking_required"] is not True
        or value["finalizer_required"] is not True
        or required < 0
        or required > MAX_CONTEXT_WORKERS
        or started < 0
        or started > required
        or terminal < 0
        or terminal > started
        or not isinstance(unstarted, list)
        or not isinstance(unterminated, list)
        or any(
            not isinstance(slot, int)
            or isinstance(slot, bool)
            or slot < 1
            or slot > required
            for slot in unstarted + unterminated
        )
        or len(set(unstarted + unterminated)) != len(unstarted) + len(unterminated)
        or started != required - len(unstarted)
        or terminal != started - len(unterminated)
        or any(
            not isinstance(value.get(key), bool)
            for key in (
                "tracking_required",
                "finalizer_required",
                "finalizer_started",
                "finalizer_terminal",
                "implicit_timeout_allowed",
                "complete",
            )
        )
        or value["implicit_timeout_allowed"] is not False
        or value["finalizer_terminal"] and not value["finalizer_started"]
        or value["finalizer_started"] and terminal != required
        or value["complete"]
        is not (
            terminal == required
            and (
                not value["finalizer_required"] or value["finalizer_terminal"]
            )
        )
    ):
        return None
    return dict(value)


def current_data_plane_receipt(
    work_dir: Path, worker_role: str
) -> dict[str, Any] | None:
    """Return only a current, parent-safe receipt for status recovery."""

    profile = DATA_PLANE_PROFILES.get(worker_role)
    if profile is None:
        return None
    path = work_dir / ".game-l10n" / "data-plane" / worker_role / "receipt.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    legacy_required = {
        "schema",
        "task_id",
        "worker_role",
        "stage",
        "status",
        "counts",
        "issue_codes",
        "artifact_paths",
        "queue_transitions",
        "control_generation",
    }
    current_required = legacy_required | {"execution"}
    schema = payload.get("schema") if isinstance(payload, dict) else None
    safe_execution = (
        _safe_execution_summary(payload.get("execution"))
        if schema == DATA_PLANE_RECEIPT_SCHEMA
        else None
    )
    if (
        not isinstance(payload, dict)
        or (
            schema == LEGACY_DATA_PLANE_RECEIPT_SCHEMA
            and set(payload) != legacy_required
        )
        or (schema == DATA_PLANE_RECEIPT_SCHEMA and set(payload) != current_required)
        or schema not in {LEGACY_DATA_PLANE_RECEIPT_SCHEMA, DATA_PLANE_RECEIPT_SCHEMA}
        or payload.get("worker_role") != worker_role
        or payload.get("stage") != profile["stage"]
        or payload.get("control_generation")
        != int(control_plane_state(work_dir).get("generation", 0))
        or not isinstance(payload.get("issue_codes"), list)
        or (schema == DATA_PLANE_RECEIPT_SCHEMA and safe_execution is None)
    ):
        return None
    counts = payload.get("counts", {})
    safe_counts = {
        key: int(counts.get(key, 0))
        for key in ("total", "success", "error", "pending")
        if isinstance(counts, dict)
        and isinstance(counts.get(key, 0), int)
        and not isinstance(counts.get(key, 0), bool)
    }
    return {
        "status": payload.get("status"),
        "issue_codes": list(payload["issue_codes"]),
        "counts": safe_counts,
        **(
            {"execution": safe_execution}
            if schema == DATA_PLANE_RECEIPT_SCHEMA and safe_execution is not None
            else {}
        ),
    }


def prepare_data_plane_task(
    work_dir: Path,
    output_dir: Path,
    *,
    worker_role: str,
    config: dict[str, Any],
    sdk_activation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare one exact, private discovery, inventory, or context worker task.

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
            "E_DATA_PLANE_TASK_SDK_ACTIVATION: data-plane tasks require an active SDK"
        )
    if worker_role in {"image_ui_inventory", "image_localizer"}:
        require_image_localization_requested(work_dir)
        image_state = public_image_localization_state(work_dir)
        required_state = (
            "scope_required"
            if worker_role == "image_ui_inventory"
            else "localization_required"
        )
        if image_state.get("status") != required_state:
            raise ConfigurationError(
                "E_IMAGE_TASK_STATE: image data-plane task is not the next required action"
            )

    artifact_relative = profile["artifact"]
    if worker_role == "context_analyzer":
        artifact_relative = str(
            config.get("files", {}).get("context_pack", artifact_relative)
        )
    artifact = (work_dir / artifact_relative).resolve()
    counterpart = (
        (work_dir / profile["counterpart"]).resolve()
        if profile["counterpart"]
        else None
    )
    attempt = 1
    previous_context_receipt_success = 0
    previous_context_baseline = 0
    previous_context_no_progress = 0
    previous_task_path = output_dir / "task.json"
    if previous_task_path.is_file():
        try:
            previous_task = json.loads(previous_task_path.read_text(encoding="utf-8"))
            previous_attempt = previous_task.get("attempt", 0)
            valid_previous_attempt = (
                isinstance(previous_attempt, int)
                and not isinstance(previous_attempt, bool)
                and previous_attempt >= 1
            )
            previous_receipt_path = output_dir / "receipt.json"
            previous_receipt = (
                json.loads(previous_receipt_path.read_text(encoding="utf-8"))
                if previous_receipt_path.is_file()
                else None
            )
            if worker_role == "context_analyzer":
                previous_counts = (
                    previous_receipt.get("counts", {})
                    if isinstance(previous_receipt, dict)
                    else {}
                )
                if isinstance(previous_counts, dict):
                    value = previous_counts.get("success", 0)
                    if isinstance(value, int) and not isinstance(value, bool):
                        previous_context_receipt_success = max(0, value)
                baseline = previous_task.get("context_progress_baseline_success", 0)
                if isinstance(baseline, int) and not isinstance(baseline, bool):
                    previous_context_baseline = max(0, baseline)
                no_progress = previous_task.get("context_no_progress_attempts", 0)
                if isinstance(no_progress, int) and not isinstance(no_progress, bool):
                    previous_context_no_progress = max(0, no_progress)
            receipt_closes_previous = (
                isinstance(previous_receipt, dict)
                and previous_receipt.get("schema")
                in {LEGACY_DATA_PLANE_RECEIPT_SCHEMA, DATA_PLANE_RECEIPT_SCHEMA}
                and previous_receipt.get("task_id") == previous_task.get("task_id")
                and previous_receipt.get("worker_role") == worker_role
                and previous_receipt.get("stage") == profile["stage"]
                and previous_receipt.get("control_generation") == control_generation
            )
            if valid_previous_attempt and receipt_closes_previous:
                attempt = previous_attempt + 1
            elif valid_previous_attempt:
                if previous_task.get("control_generation") != control_generation:
                    # A recorded parent leak invalidates only the old control-plane
                    # binding. Preserve the stage attempt and private progress while
                    # issuing a fresh relay bound to the new generation.
                    attempt = previous_attempt
                else:
                    previous_relay_path = output_dir / "relay.json"
                    previous_relay, bound_task = _load_bound_data_plane_relay(
                        work_dir, previous_relay_path
                    )
                    if bound_task.get("task_id") == previous_task.get("task_id"):
                        return {
                            "schema": previous_relay["schema"],
                            "status": "ready",
                            "worker_role": worker_role,
                            "stage": profile["stage"],
                            "relay_path": _relative(work_dir, previous_relay_path),
                            "task_id": previous_task["task_id"],
                            "control_generation": control_generation,
                        }
        except (OSError, json.JSONDecodeError):
            attempt = 1
    # Every attempt and control-plane generation owns a distinct response path.
    # Older envelopes remain immutable evidence and can never be resealed as a
    # later attempt merely because their file name was reused.
    envelope = (
        output_dir / f"control-envelope.g{control_generation}.attempt-{attempt}.json"
    ).resolve()
    worker_route = ""
    worker_model_family = ""
    relay_max_workers = 1
    worker_delegation_allowed = False
    private_payload: dict[str, Any] = {
        "schema": DATA_PLANE_TASK_SCHEMA,
        "worker_role": worker_role,
        "stage": profile["stage"],
        "attempt": attempt,
        "control_generation": control_generation,
        "workspace_root": str(work_dir.resolve()),
        "source_roots": [] if worker_role == "context_analyzer" else roots,
        "sdk_root": str(Path(config.get("sdk_root", "")).resolve()),
        "sdk_inventory_fingerprint": selection.get("inventory_fingerprint", ""),
        "sdk_activation": sdk_activation or {},
        "language_binding": language_binding(work_dir, config),
        "output_artifact": _relative(work_dir, artifact),
        "output_envelope": _relative(work_dir, envelope),
        "counterpart_inventory_forbidden": (
            _relative(work_dir, counterpart) if counterpart is not None else ""
        ),
        "requirements": [
            "source_roots_are_read_only",
            (
                "no_install_launch_or_publication"
                if worker_role in {"image_ui_inventory", "image_localizer"}
                else "no_install_launch_ocr_or_publication"
            ),
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
            "primary_id_fingerprint": attestation.get("primary_id_fingerprint"),
            "independent_id_fingerprint": attestation.get(
                "independent_id_fingerprint"
            ),
            "primary_identity_binding": attestation.get(
                "primary_identity_binding", {}
            ),
            "identity_gate": attestation.get("identity_gate", {}),
            "gap_score": attestation.get("gap_score"),
            "issue_codes": attestation.get("issue_codes", []),
            "reconciliation_generation": reconciliation.get("generation", 0),
        }
    if worker_role == "context_analyzer":
        attestation_path = work_dir / ".game-l10n" / "inventory-attestation.json"
        if not attestation_path.is_file():
            raise ConfigurationError(
                "E_DATA_PLANE_TASK_INVENTORY: context analysis requires a verified inventory"
            )
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        primary_path = Path(str(attestation.get("primary_path", ""))).resolve()
        verified = require_inventory_attestation(work_dir, primary_path)
        context_mode = str(
            config.get("project", {}).get("context_mode", ContextMode.AUTO.value)
        )
        context_routes = list(config.get("routing", {}).get("context_analyzers", []))
        if not context_routes or not all(
            isinstance(item, str) and item.strip() for item in context_routes
        ):
            raise ConfigurationError(
                "E_DATA_PLANE_TASK_ROUTE: context analysis requires configured routes"
            )
        route_index = (attempt - 1) % len(context_routes)
        route_cycle = (attempt - 1) // len(context_routes)
        worker_route = context_routes[route_index]
        worker_model_family = (
            worker_route.removeprefix("native-")
            if worker_route.startswith("native-")
            else "external"
        )
        base_context_chunk_chars = int(
            config.get("routing", {}).get("context_chunk_chars", 30000)
        )
        if base_context_chunk_chars <= 0:
            raise ConfigurationError(
                "E_DATA_PLANE_TASK_ROUTE: context chunk size must be positive"
            )
        made_progress = previous_context_receipt_success > previous_context_baseline
        context_progress_baseline = max(
            previous_context_baseline, previous_context_receipt_success
        )
        context_no_progress_attempts = (
            0 if made_progress else previous_context_no_progress + (1 if attempt > 1 else 0)
        )
        failed_route_cycles = context_no_progress_attempts // len(context_routes)
        context_chunk_chars = max(
            MIN_CONTEXT_CHUNK_CHARS,
            base_context_chunk_chars // (2**failed_route_cycles),
        )
        configured_context_workers = min(
            MAX_CONTEXT_WORKERS,
            max(
                1,
                int(
                    config.get("routing", {}).get(
                        "context_max_workers",
                        config.get("routing", {}).get("max_workers", 1),
                    )
                ),
            ),
        )
        context_segments = read_segments(primary_path)
        context_preflight = build_context_preflight(context_segments)
        context_fingerprints = context_contract_fingerprints()
        context_preflight_path = (
            work_dir / ".game-l10n" / "context-progress" / "preflight.json"
        )
        _atomic_json(context_preflight_path, context_preflight)
        context_manifest_path, context_manifest = _write_context_manifest(
            work_dir,
            segments=context_segments,
            preflight=context_preflight,
            source_inventory_fingerprint=verified["primary_fingerprint"],
            context_mode=context_mode,
            chunk_chars=context_chunk_chars,
            contract_fingerprints=context_fingerprints,
        )
        _, pending_context_chunks = _context_progress(work_dir, context_manifest)
        relay_max_workers = min(
            configured_context_workers, len(pending_context_chunks)
        )
        worker_delegation_allowed = relay_max_workers > 1
        private_payload.update(
            {
                "input_inventory": _relative(work_dir, primary_path),
                "input_context_preflight": _relative(
                    work_dir, context_preflight_path
                ),
                "context_preflight_sha256": _digest(context_preflight_path),
                "context_contract_fingerprints": context_fingerprints,
                "input_context_manifest": _relative(
                    work_dir, context_manifest_path
                ),
                "context_manifest_sha256": _digest(context_manifest_path),
                "context_manifest_id": context_manifest["manifest_id"],
                "source_inventory_fingerprint": verified["primary_fingerprint"],
                "output_state": ".game-l10n/context-state.json",
                "context_mode": context_mode,
                "context_routes": context_routes,
                "context_route": worker_route,
                "context_route_index": route_index,
                "context_route_attempt": route_cycle + 1,
                "context_base_chunk_chars": base_context_chunk_chars,
                "context_chunk_chars": context_chunk_chars,
                "context_chunk_measurement": (
                    "serialized_payload_chars_including_ids_source_and_metadata"
                ),
                "context_progress_baseline_success": context_progress_baseline,
                "context_no_progress_attempts": context_no_progress_attempts,
                "context_parallelism": {
                    "max_workers": relay_max_workers,
                    "pending_chunks": len(pending_context_chunks),
                    "completed_chunks": len(context_manifest["chunks"])
                    - len(pending_context_chunks),
                    "assignment": "atomic_lowest_unfinished_chunk_claim_loop",
                    "claim_root": ".game-l10n/context-progress/claims",
                    "fragment_root": ".game-l10n/context-progress/fragments",
                    "fragment_ownership": "one_active_chunk_per_slot",
                    "worker_loop": "claim_process_commit_repeat_until_exhausted",
                    "shared_artifact_writer": "single_fresh_finalizer_after_wave",
                    "merge_order": "source_chunk_index",
                    "reuse_completed_fragments": True,
                    "retry_only_unfinished_chunks": True,
                },
                "context_protocol": {
                    "manifest_schema": CONTEXT_WORK_MANIFEST_SCHEMA,
                    "claim_schema": CONTEXT_CHUNK_CLAIM_SCHEMA,
                    "result_schema": CONTEXT_CHUNK_RESULT_SCHEMA,
                    "fragment_schema": CONTEXT_FRAGMENT_SCHEMA,
                    "claim_command": (
                        "game-l10n claim-context-chunk --workspace <workspace> "
                        "--task <task-path> --slot <slot>"
                    ),
                    "commit_command": (
                        "game-l10n commit-context-fragment --workspace <workspace> "
                        "--task <task-path> --claim <claim-path> --result <result-path>"
                    ),
                    "finalize_command": (
                        "game-l10n finalize-context-fragments --workspace <workspace> "
                        "--task <task-path>"
                    ),
                    "direct_context_pack_write_forbidden": True,
                    "success_requires_every_manifest_chunk": True,
                },
                "context_recovery": {
                    "resume_private_progress": True,
                    "retry_scope": "smallest_unfinished_context_chunk",
                    "route_selection": "core_round_robin",
                    "chunk_reduction": "halve_only_after_a_complete_no_progress_route_cycle",
                    "progress_resets_chunk_reduction": True,
                    "minimum_chunk_chars": MIN_CONTEXT_CHUNK_CHARS,
                    "sealed_failure_is_job_terminal": False,
                    "unchanged_progress_limit_applies": False,
                },
                "context_preflight_contract": {
                    "schema": CONTEXT_PREFLIGHT_SCHEMA,
                    "policy_version": CONTEXT_PREFLIGHT_POLICY_VERSION,
                    "dispositions": [
                        "deterministic_ui",
                        "semantic_required",
                        "uncertain_ui",
                    ],
                    "semantic_input": ["semantic_required", "uncertain_ui"],
                    "deterministic_skip": ["deterministic_ui"],
                    "buttons_are_ui_but_never_prove_deterministic_skip": True,
                    "uncertainty_fails_to_semantic_analysis": True,
                },
                "context_summary_reuse_contract": {
                    "reuse_requires_exact_match": [
                        "fragment_source_digest",
                        "fragment_ordered_evidence_ids",
                        "context_preflight_policy_version",
                        "context_mode",
                        "context_contract_fingerprints",
                    ],
                    "changed_chunk_ceiling_repartitions_only_unfinished_records": True,
                    "stale_or_unbound_summary_is_input_only": True,
                    "summary_presence_never_proves_reuse": True,
                    "reuse_completed_legacy_pack_without_rewrite": True,
                },
                "context_pack_fields": [item.name for item in fields(ContextPack)],
                "context_pack_contract": {
                    "string_fields": [
                        "story_summary",
                        "context_mode",
                        "functional_summary",
                        "knowledge_summary",
                    ],
                    "array_fields": [
                        "global_style_rules",
                        "relationship_timeline",
                        "reference_cues",
                        "terminology_rules",
                    ],
                    "object_fields": [
                        "scene_summaries",
                        "character_guides",
                        "screen_flows",
                        "feature_guides",
                        "knowledge_nodes",
                    ],
                    "evidence_line_ids_required_for": [
                        "screen_flows",
                        "feature_guides",
                        "knowledge_nodes",
                    ],
                },
            }
        )
    if worker_role == "image_localizer":
        scope_path = work_dir / IMAGE_SCOPE_PATH
        if not scope_path.is_file():
            raise ConfigurationError(
                "E_IMAGE_SCOPE_REQUIRED: image localization requires a verified UI-image scope"
            )
        scope = json.loads(scope_path.read_text(encoding="utf-8"))
        inventory_path = (work_dir / str(scope.get("inventory_path", ""))).resolve()
        if not is_within(inventory_path, work_dir) or not inventory_path.is_file():
            raise ConfigurationError(
                "E_IMAGE_SCOPE_REQUIRED: verified UI-image inventory is unavailable"
            )
        private_payload.update(
            {
                "input_image_inventory": _relative(work_dir, inventory_path),
                "input_image_inventory_sha256": _digest(inventory_path),
                "output_image_root": "localized/image-ui",
            }
        )
    if worker_role in {"primary_inventory", "independent_inventory"}:
        private_payload["requirements"].extend(
            [
                "scan_source_once_using_selected_sdk_role_method",
                "do_not_read_or_derive_from_counterpart_inventory",
                "retain_unresolved_player_visible_candidates",
                "emit_inventory_comparison_key_for_method_specific_ids",
                "report_selected_vs_observed_engine_identity_conflicts_as_sdk_identity_mismatch",
            ]
        )
    elif worker_role == "inventory_reconciler":
        private_payload["requirements"].extend(
            [
                "reuse_existing_inventory_scans_and_cached_evidence",
                "do_not_repeat_full_source_scans",
                "resolve_only_reported_gap_categories",
                "use_inventory_comparison_key_for_proven_method_specific_aliases",
                "treat_global_id_disagreement_as_identity_namespace_reconciliation_not_content_loss",
                "never_rename_remove_or_reorder_checkpoint_bound_primary_ids",
                "preserve_primary_ids_as_downstream_canonical_identity",
                "repair_method_specific_aliases_on_the_independent_inventory_with_deterministic_evidence",
                "do_not_match_duplicate_source_text_without_occurrence_or_location_evidence",
                "retain_unresolved_player_visible_candidates",
                "run_core_verify_inventory_and_bind_current_attestation",
                "incomplete_with_measured_progress_requires_another_bounded_step",
            ]
        )
    elif worker_role == "context_analyzer":
        private_payload["requirements"].extend(
            [
                "read_only_the_verified_primary_inventory_and_existing_context_metadata",
                "read_and_obey_the_core_generated_context_preflight_before_chunking",
                "never_treat_ui_or_button_classification_alone_as_context_free",
                "send_semantic_required_and_uncertain_ui_records_to_context_analysis",
                "skip_only_deterministic_ui_records_with_positive_static_authority",
                "reuse_a_prior_summary_only_when_every_bound_fingerprint_matches",
                "treat_unbound_or_stale_summaries_as_non_authoritative_input_only",
                "analyze_complete_flow_character_voice_tone_terminology_and_references",
                "use_only_the_core_bound_context_route_for_this_attempt",
                "never_send_the_whole_inventory_in_one_semantic_request",
                "measure_chunk_limits_over_the_complete_serialized_request_rows_including_metadata",
                "process_deterministic_bounded_context_chunks_without_returning_text_to_parent",
                "write_one_merged_context_pack_using_exact_context_pack_fields",
                "preserve_evidence_line_ids_for_every_required_domain",
                "on_partial_or_operational_failure_record_resumable_private_progress",
                "continue_unaffected_chunks_when_one_chunk_requires_fallback",
                "relay_assigns_unique_context_worker_slots_from_one_to_max_workers",
                "workers_must_use_core_claim_context_chunk_command",
                "each_worker_keeps_at_most_one_core_atomic_claim_active_at_a_time",
                "workers_must_write_exact_context_chunk_result_schema",
                "workers_must_use_core_commit_context_fragment_command",
                "after_committing_a_fragment_claim_the_next_lowest_unfinished_chunk",
                "workers_write_only_slot_owned_fragments_never_the_shared_context_pack",
                "after_the_wave_one_fresh_finalizer_runs_core_finalize_context_fragments",
                "finalizer_reuses_completed_fragments_and_retries_only_missing_chunks",
                "when_pending_chunks_are_zero_start_no_semantic_workers_and_run_only_the_finalizer",
                "never_treat_context_refusal_or_unchanged_progress_as_whole_job_terminal",
                "do_not_translate_review_apply_pack_install_launch_or_delegate",
            ]
        )
    elif worker_role == "image_ui_inventory":
        private_payload["requirements"].extend(
            [
                "user_request_is_recorded_but_does_not_prove_any_image_is_a_target",
                "start_from_engine_ui_bindings_or_user_identified_ui_assets",
                "do_not_recursively_ocr_all_images",
                "use_ocr_only_on_bounded_ui_candidates_and_confirm_each_text_region_visually",
                "include_only_player_visible_images_with_definite_ui_binding_and_text",
                "keep_uncertain_images_out_of_scope_and_account_for_them_without_guessing",
                "preserve_source_asset_paths_hashes_dimensions_regions_and_atlas_evidence",
                "do_not_translate_edit_apply_pack_install_launch_or_delegate",
            ]
        )
    elif worker_role == "image_localizer":
        private_payload["requirements"].extend(
            [
                "read_only_assets_and_regions_bound_by_input_image_inventory",
                "do_not_scan_or_ocr_any_unlisted_image",
                "translate_only_confirmed_text_regions_into_project_target_language",
                "preserve_format_dimensions_alpha_atlas_layout_and_pixels_outside_text_regions",
                "record_independent_visual_review_or_explicit_user_acceptance_for_each_asset",
                "write_localized_assets_only_under_output_image_root",
                "do_not_apply_pack_install_launch_publish_or_delegate",
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
    lifecycle_path = output_dir / "relay-lifecycle.json"
    lifecycle_relative = (
        _relative(work_dir, lifecycle_path)
        if worker_role == "context_analyzer"
        else ""
    )
    execution_plan = _execution_plan(
        worker_role=worker_role,
        max_workers=relay_max_workers,
        lifecycle_path=lifecycle_relative,
    )
    if execution_plan["tracking_required"]:
        _atomic_json(
            lifecycle_path,
            _new_relay_lifecycle(
                task_id=task_id,
                control_generation=control_generation,
                required_worker_slots=list(execution_plan["required_worker_slots"]),
                finalizer_required=bool(execution_plan["finalizer_required"]),
            ),
        )
    relay = {
        "schema": DATA_PLANE_RELAY_SCHEMA,
        "task_id": task_id,
        "task_path": _relative(work_dir, task_path),
        "task_sha256": _digest(task_path),
        "worker_role": worker_role,
        "stage": profile["stage"],
        "expected_envelope": _relative(work_dir, envelope),
        "max_workers": relay_max_workers,
        "relay_authority": "exact_dispatch_only",
        "worker_history": "none",
        "worker_delegation_allowed": worker_delegation_allowed,
        "worker_route": worker_route,
        "worker_model_family": worker_model_family,
        "attempt": attempt,
        "sealed_failure_is_job_terminal": False,
        "execution_plan": execution_plan,
        "relay_contract": {
            "schema": DATA_PLANE_RELAY_CONTRACT_SCHEMA,
            "role": "isolated_data_plane_relay",
            "authority": "exact_dispatch_and_categorical_seal_only",
            "child_response_policy": "never_read_interpret_summarize_or_forward",
            "worker_terminal_action": "seal_data_plane_task_with_worker_ended",
            "parent_response": "receipt_path_only",
            "route_policy": "dispatch_exact_core_bound_worker_route",
            "failure_policy": "seal_then_return_to_status_nonterminal",
            "private_artifact_policy": "parent_must_not_open_task_or_artifacts",
            "execution_policy": "follow_execution_plan_exactly",
            "implicit_timeout_policy": "forbidden_unless_core_plan_binds_one",
            "completion_evidence_policy": "core_lifecycle_state_required_before_seal",
            "terminal_seal_command": (
                "game-l10n seal-data-plane-task --workspace <workspace> "
                "--relay <relay-path> --worker-ended"
            ),
        },
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


def _load_bound_data_plane_relay(
    work_dir: Path, relay_path: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate immutable relay/task bindings before result collection."""

    require_control_plane_ready(work_dir)
    require_control_plane_entry(work_dir)
    resolved_relay = relay_path.resolve()
    if not is_within(resolved_relay, work_dir) or not resolved_relay.is_file():
        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: relay must be a workspace file")
    relay = json.loads(resolved_relay.read_text(encoding="utf-8"))
    legacy_relay_fields = {
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
    v2_relay_fields = legacy_relay_fields | {"relay_contract"}
    v3_relay_fields = v2_relay_fields | {
        "worker_route",
        "worker_model_family",
        "attempt",
        "sealed_failure_is_job_terminal",
    }
    relay_fields = v3_relay_fields | {"execution_plan"}
    relay_schema = relay.get("schema") if isinstance(relay, dict) else None
    relay_contract = relay.get("relay_contract") if isinstance(relay, dict) else None
    valid_legacy_contract = (
        isinstance(relay_contract, dict)
        and set(relay_contract)
        == {
            "schema",
            "role",
            "authority",
            "child_response_policy",
            "worker_terminal_action",
            "parent_response",
            "terminal_seal_command",
        }
        and relay_contract.get("schema") == LEGACY_DATA_PLANE_RELAY_CONTRACT_SCHEMA
        and relay_contract.get("role") == "isolated_data_plane_relay"
        and relay_contract.get("authority")
        == "exact_dispatch_and_categorical_seal_only"
        and relay_contract.get("child_response_policy")
        == "never_read_interpret_summarize_or_forward"
        and relay_contract.get("worker_terminal_action")
        == "seal_data_plane_task_with_worker_ended"
        and relay_contract.get("parent_response") == "receipt_path_only"
        and relay_contract.get("terminal_seal_command")
        == (
            "game-l10n seal-data-plane-task --workspace <workspace> "
            "--relay <relay-path> --worker-ended"
        )
    )
    valid_v2_contract = (
        isinstance(relay_contract, dict)
        and set(relay_contract)
        == {
            "schema",
            "role",
            "authority",
            "child_response_policy",
            "worker_terminal_action",
            "parent_response",
            "route_policy",
            "failure_policy",
            "private_artifact_policy",
            "terminal_seal_command",
        }
        and relay_contract.get("schema")
        == LEGACY_DATA_PLANE_RELAY_CONTRACT_V2_SCHEMA
        and relay_contract.get("role") == "isolated_data_plane_relay"
        and relay_contract.get("authority")
        == "exact_dispatch_and_categorical_seal_only"
        and relay_contract.get("child_response_policy")
        == "never_read_interpret_summarize_or_forward"
        and relay_contract.get("worker_terminal_action")
        == "seal_data_plane_task_with_worker_ended"
        and relay_contract.get("parent_response") == "receipt_path_only"
        and relay_contract.get("route_policy")
        == "dispatch_exact_core_bound_worker_route"
        and relay_contract.get("failure_policy")
        == "seal_then_return_to_status_nonterminal"
        and relay_contract.get("private_artifact_policy")
        == "parent_must_not_open_task_or_artifacts"
        and relay_contract.get("terminal_seal_command")
        == (
            "game-l10n seal-data-plane-task --workspace <workspace> "
            "--relay <relay-path> --worker-ended"
        )
    )
    valid_contract = (
        isinstance(relay_contract, dict)
        and set(relay_contract)
        == {
            "schema",
            "role",
            "authority",
            "child_response_policy",
            "worker_terminal_action",
            "parent_response",
            "route_policy",
            "failure_policy",
            "private_artifact_policy",
            "execution_policy",
            "implicit_timeout_policy",
            "completion_evidence_policy",
            "terminal_seal_command",
        }
        and relay_contract.get("schema") == DATA_PLANE_RELAY_CONTRACT_SCHEMA
        and relay_contract.get("role") == "isolated_data_plane_relay"
        and relay_contract.get("authority")
        == "exact_dispatch_and_categorical_seal_only"
        and relay_contract.get("child_response_policy")
        == "never_read_interpret_summarize_or_forward"
        and relay_contract.get("worker_terminal_action")
        == "seal_data_plane_task_with_worker_ended"
        and relay_contract.get("parent_response") == "receipt_path_only"
        and relay_contract.get("route_policy")
        == "dispatch_exact_core_bound_worker_route"
        and relay_contract.get("failure_policy")
        == "seal_then_return_to_status_nonterminal"
        and relay_contract.get("private_artifact_policy")
        == "parent_must_not_open_task_or_artifacts"
        and relay_contract.get("execution_policy") == "follow_execution_plan_exactly"
        and relay_contract.get("implicit_timeout_policy")
        == "forbidden_unless_core_plan_binds_one"
        and relay_contract.get("completion_evidence_policy")
        == "core_lifecycle_state_required_before_seal"
        and relay_contract.get("terminal_seal_command")
        == (
            "game-l10n seal-data-plane-task --workspace <workspace> "
            "--relay <relay-path> --worker-ended"
        )
    )
    if (
        not isinstance(relay, dict)
        or (
            relay_schema == LEGACY_DATA_PLANE_RELAY_SCHEMA
            and set(relay) != legacy_relay_fields
        )
        or (
            relay_schema == LEGACY_DATA_PLANE_RELAY_V2_SCHEMA
            and (set(relay) != v2_relay_fields or not valid_legacy_contract)
        )
        or (
            relay_schema == LEGACY_DATA_PLANE_RELAY_V3_SCHEMA
            and (
                set(relay) != v3_relay_fields
                or not valid_v2_contract
                or not isinstance(relay.get("attempt"), int)
                or isinstance(relay.get("attempt"), bool)
                or relay.get("attempt", 0) < 1
                or not isinstance(relay.get("worker_route"), str)
                or not isinstance(relay.get("worker_model_family"), str)
                or relay.get("sealed_failure_is_job_terminal") is not False
            )
        )
        or (
            relay_schema == DATA_PLANE_RELAY_SCHEMA
            and (
                set(relay) != relay_fields
                or not valid_contract
                or not _valid_execution_plan(relay)
                or not isinstance(relay.get("attempt"), int)
                or isinstance(relay.get("attempt"), bool)
                or relay.get("attempt", 0) < 1
                or not isinstance(relay.get("worker_route"), str)
                or not isinstance(relay.get("worker_model_family"), str)
                or relay.get("sealed_failure_is_job_terminal") is not False
            )
        )
        or relay_schema
        not in {
            LEGACY_DATA_PLANE_RELAY_SCHEMA,
            LEGACY_DATA_PLANE_RELAY_V2_SCHEMA,
            LEGACY_DATA_PLANE_RELAY_V3_SCHEMA,
            DATA_PLANE_RELAY_SCHEMA,
        }
        or not isinstance(relay.get("max_workers"), int)
        or isinstance(relay.get("max_workers"), bool)
        or relay.get("max_workers", 0) < (
            0 if relay.get("worker_role") == "context_analyzer" else 1
        )
        or relay.get("max_workers", 0) > MAX_CONTEXT_WORKERS
        or (
            relay.get("worker_role") != "context_analyzer"
            and relay.get("max_workers") != 1
        )
        or relay.get("relay_authority") != "exact_dispatch_only"
        or relay.get("worker_history") != "none"
        or relay.get("worker_delegation_allowed")
        is not (
            relay.get("worker_role") == "context_analyzer"
            and relay.get("max_workers", 1) > 1
        )
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

    return relay, task


def _validated_relay_lifecycle(
    work_dir: Path, relay: dict[str, Any]
) -> dict[str, Any] | None:
    if relay.get("schema") != DATA_PLANE_RELAY_SCHEMA:
        return None
    plan = relay["execution_plan"]
    if plan.get("tracking_required") is not True:
        return None
    lifecycle_path = (work_dir / str(plan["lifecycle_path"])).resolve()
    if not is_within(lifecycle_path, work_dir) or not lifecycle_path.is_file():
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: lifecycle state is missing"
        )
    try:
        state = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: lifecycle state is unreadable"
        ) from exc
    fields_without_fingerprint = {
        "schema",
        "task_id",
        "control_generation",
        "required_worker_slots",
        "worker_states",
        "finalizer_required",
        "finalizer_state",
        "event_count",
    }
    if not isinstance(state, dict) or set(state) != fields_without_fingerprint | {
        "state_fingerprint"
    }:
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: lifecycle shape is invalid"
        )
    unsigned = {key: state[key] for key in fields_without_fingerprint}
    required_slots = plan["required_worker_slots"]
    worker_states = state.get("worker_states")
    if (
        state.get("schema") != DATA_PLANE_RELAY_LIFECYCLE_SCHEMA
        or state.get("task_id") != relay["task_id"]
        or state.get("control_generation")
        != int(control_plane_state(work_dir).get("generation", 0))
        or state.get("required_worker_slots") != required_slots
        or not isinstance(worker_states, dict)
        or set(worker_states) != {str(slot) for slot in required_slots}
        or any(
            value not in {"pending", "running", "ended"}
            for value in worker_states.values()
        )
        or state.get("finalizer_required") is not plan["finalizer_required"]
        or state.get("finalizer_state")
        not in ({"pending", "running", "ended"} if plan["finalizer_required"] else {"not_required"})
        or not isinstance(state.get("event_count"), int)
        or isinstance(state.get("event_count"), bool)
        or state.get("event_count", -1) < 0
        or state.get("state_fingerprint") != stable_hash(unsigned)
    ):
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: lifecycle binding is invalid"
        )
    return state


def _relay_execution_summary(
    work_dir: Path, relay: dict[str, Any]
) -> dict[str, Any]:
    plan = relay.get("execution_plan", {})
    tracking_required = (
        relay.get("schema") == DATA_PLANE_RELAY_SCHEMA
        and plan.get("tracking_required") is True
    )
    if not tracking_required:
        return {
            "tracking_required": False,
            "required_workers": int(relay.get("max_workers", 1)),
            "started_workers": 0,
            "terminal_workers": 0,
            "unstarted_slots": [],
            "unterminated_slots": [],
            "finalizer_required": False,
            "finalizer_started": False,
            "finalizer_terminal": False,
            "implicit_timeout_allowed": False,
            "complete": True,
        }
    state = _validated_relay_lifecycle(work_dir, relay)
    assert state is not None
    required_slots = list(state["required_worker_slots"])
    worker_states = state["worker_states"]
    unstarted = [slot for slot in required_slots if worker_states[str(slot)] == "pending"]
    unterminated = [slot for slot in required_slots if worker_states[str(slot)] == "running"]
    terminal_workers = sum(
        worker_states[str(slot)] == "ended" for slot in required_slots
    )
    finalizer_state = state["finalizer_state"]
    finalizer_required = bool(state["finalizer_required"])
    complete = (
        terminal_workers == len(required_slots)
        and (not finalizer_required or finalizer_state == "ended")
    )
    return {
        "tracking_required": True,
        "required_workers": len(required_slots),
        "started_workers": len(required_slots) - len(unstarted),
        "terminal_workers": terminal_workers,
        "unstarted_slots": unstarted,
        "unterminated_slots": unterminated,
        "finalizer_required": finalizer_required,
        "finalizer_started": finalizer_state in {"running", "ended"},
        "finalizer_terminal": finalizer_state == "ended",
        "implicit_timeout_allowed": False,
        "complete": complete,
    }


def record_data_plane_relay_event(
    work_dir: Path,
    relay_path: Path,
    *,
    event: str,
    slot: int | None = None,
) -> dict[str, Any]:
    """Record categorical relay lifecycle without observing semantic output."""

    relay, _ = _load_bound_data_plane_relay(work_dir, relay_path)
    if relay.get("schema") != DATA_PLANE_RELAY_SCHEMA:
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: legacy relays do not use lifecycle events"
        )
    state = _validated_relay_lifecycle(work_dir, relay)
    if state is None:
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: this relay has no tracked lifecycle"
        )
    worker_states = dict(state["worker_states"])
    required_slots = list(state["required_worker_slots"])
    finalizer_state = str(state["finalizer_state"])
    if event in {"worker-started", "worker-ended"}:
        if slot not in required_slots:
            raise ConfigurationError(
                "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: worker slot is not bound"
            )
        key = str(slot)
        if event == "worker-started":
            if worker_states[key] == "pending":
                worker_states[key] = "running"
            elif worker_states[key] not in {"running", "ended"}:
                raise ConfigurationError(
                    "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: worker start transition is invalid"
                )
        else:
            if any(value == "pending" for value in worker_states.values()):
                raise ConfigurationError(
                    "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: every required slot must start before any worker ends"
                )
            if worker_states[key] == "running":
                worker_states[key] = "ended"
            elif worker_states[key] != "ended":
                raise ConfigurationError(
                    "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: worker end transition is invalid"
                )
    elif event in {"finalizer-started", "finalizer-ended"}:
        if slot is not None:
            raise ConfigurationError(
                "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: finalizer events do not take a slot"
            )
        if state["finalizer_required"] is not True:
            raise ConfigurationError(
                "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: finalizer is not bound"
            )
        if any(value != "ended" for value in worker_states.values()):
            raise ConfigurationError(
                "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: finalizer requires every worker slot to end"
            )
        if event == "finalizer-started":
            if finalizer_state == "pending":
                finalizer_state = "running"
            elif finalizer_state not in {"running", "ended"}:
                raise ConfigurationError(
                    "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: finalizer start transition is invalid"
                )
        else:
            if finalizer_state == "running":
                finalizer_state = "ended"
            elif finalizer_state != "ended":
                raise ConfigurationError(
                    "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: finalizer end transition is invalid"
                )
    else:
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_LIFECYCLE_INVALID: unsupported lifecycle event"
        )

    updated = _with_state_fingerprint(
        {
            "schema": state["schema"],
            "task_id": state["task_id"],
            "control_generation": state["control_generation"],
            "required_worker_slots": required_slots,
            "worker_states": worker_states,
            "finalizer_required": state["finalizer_required"],
            "finalizer_state": finalizer_state,
            "event_count": int(state["event_count"]) + 1,
        }
    )
    lifecycle_path = (work_dir / relay["execution_plan"]["lifecycle_path"]).resolve()
    _atomic_json(lifecycle_path, updated)
    return {
        "status": "recorded",
        "event": event,
        "slot": slot,
        "execution": _relay_execution_summary(work_dir, relay),
    }


def _seal_bound_data_plane_task(
    work_dir: Path, relay: dict[str, Any], task: dict[str, Any]
) -> dict[str, Any]:
    """Validate one exact bound result and emit its metadata-only receipt."""

    envelope_path = (work_dir / str(relay["expected_envelope"])).resolve()
    result = validate_control_envelope(
        work_dir,
        envelope_path,
        expected_role=str(relay["worker_role"]),
        expected_stage=str(relay["stage"]),
    )
    native_context_counts: dict[str, int] | None = None
    if (
        result["worker_role"] == "context_analyzer"
        and "input_context_manifest" in task
    ):
        native_context_counts = _context_safe_counts(work_dir, task)
        expected_status = (
            "success" if native_context_counts["pending"] == 0 else "partial"
        )
        if result["status"] != expected_status or result["counts"] != native_context_counts:
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: context envelope is not bound to fragment progress"
            )
    expected_artifact = str(task["output_artifact"])
    artifact_required = (
        result["worker_role"] != "context_analyzer" or result["status"] == "success"
    )
    if artifact_required and expected_artifact not in result["artifact_paths"]:
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
        prior_identity_binding = task.get("inventory_gate", {}).get(
            "primary_identity_binding", {}
        )
        current_identity_binding = attestation.get("primary_identity_binding", {})
        prior_identity_records = (
            prior_identity_binding.get("records", 0)
            if isinstance(prior_identity_binding, dict)
            else 0
        )
        current_identity_records = (
            current_identity_binding.get("records", 0)
            if isinstance(current_identity_binding, dict)
            else 0
        )
        if (
            not isinstance(current_identity_binding, dict)
            or not isinstance(prior_identity_records, int)
            or isinstance(prior_identity_records, bool)
            or not isinstance(current_identity_records, int)
            or isinstance(current_identity_records, bool)
            or current_identity_binding.get("policy")
            != "append_only_preserve_existing_id_order"
            or current_identity_binding.get("id_fingerprint")
            != attestation.get("primary_id_fingerprint")
            or current_identity_records < prior_identity_records
        ):
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: reconciliation violated canonical "
                "primary ID lineage"
            )
        if result["status"] == "success" and (
            attestation.get("status") != "static_verified"
            or attestation.get("static_complete") is not True
        ):
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: successful reconciliation is incomplete"
            )
    elif result["worker_role"] == "context_analyzer" and result["status"] == "success":
        native_manifest_bound = all(
            key in task
            for key in (
                "input_context_manifest",
                "context_manifest_sha256",
                "context_manifest_id",
            )
        )
        completed_fragments: list[
            tuple[dict[str, Any], Path, dict[str, Any]]
        ] = []
        expected_native_pack: ContextPack | None = None
        native_manifest: dict[str, Any] | None = None
        if native_manifest_bound:
            expected_native_pack, completed_fragments, native_manifest = (
                _expected_context_pack_from_fragments(work_dir, task)
            )
            expected_counts = {
                "total": len(native_manifest["chunks"]),
                "success": len(completed_fragments),
                "error": 0,
                "pending": 0,
            }
            if result["counts"] != expected_counts:
                raise ConfigurationError(
                    "E_DATA_PLANE_RESULT_INVALID: context envelope counts do not match fragments"
                )
        source_path = (work_dir / str(task.get("input_inventory", ""))).resolve()
        if not is_within(source_path, work_dir) or not source_path.is_file():
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: bound context inventory is missing"
            )
        preflight_fields = {
            "input_context_preflight",
            "context_preflight_sha256",
            "context_contract_fingerprints",
        }
        present_preflight_fields = preflight_fields & set(task)
        preflight_path: Path | None = None
        if present_preflight_fields:
            if present_preflight_fields != preflight_fields:
                raise ConfigurationError(
                    "E_DATA_PLANE_RESULT_INVALID: bound context contract is incomplete"
                )
            preflight_path = (
                work_dir / str(task["input_context_preflight"])
            ).resolve()
            if (
                not is_within(preflight_path, work_dir)
                or not preflight_path.is_file()
                or _digest(preflight_path) != task["context_preflight_sha256"]
                or task["context_contract_fingerprints"]
                != context_contract_fingerprints()
            ):
                raise ConfigurationError(
                    "E_DATA_PLANE_RESULT_INVALID: bound context contract is missing or changed"
                )
        try:
            segments = read_segments(source_path)
            expected_preflight = build_context_preflight(segments)
            if preflight_path is not None:
                preflight_payload = json.loads(
                    preflight_path.read_text(encoding="utf-8")
                )
                if preflight_payload != expected_preflight:
                    raise ValueError("context preflight")
            context_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            expected_fields = {item.name for item in fields(ContextPack)}
            if (
                not isinstance(context_payload, dict)
                or set(context_payload) != expected_fields
            ):
                raise ValueError("context fields")
            context_pack = ContextPack(**context_payload)
            if expected_native_pack is not None and stable_hash(
                context_payload
            ) != stable_hash(asdict(expected_native_pack)):
                raise ValueError("context artifact was not produced from bound fragments")
            attestation = context_attestation_for_mode(
                segments,
                context_pack,
                context_mode=str(task.get("context_mode", ContextMode.AUTO.value)),
                analyzer_routes=(
                    _context_fragment_routes(
                        completed_fragments, str(task.get("context_route", ""))
                    )
                    if native_manifest_bound
                    else [
                        {
                            "name": str(task.get("context_route", "")),
                            "type": "native_collaboration",
                        }
                    ]
                ),
                preflight=expected_preflight,
            )
            if context_pack.context_mode != attestation["context_mode"]:
                raise ValueError("context mode")
            require_complete_context(attestation)
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            ProviderError,
            json.JSONDecodeError,
        ) as exc:
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: context pack failed deterministic validation"
            ) from exc
        output_body = artifact_path.read_bytes()
        semantic_segment_count = attestation["semantic_segment_count"]
        deterministic_segment_count = attestation["context_preflight"]["counts"][
            "deterministic_ui"
        ]
        validated_parts = (
            len(completed_fragments)
            if native_manifest_bound
            else 1 if semantic_segment_count else 0
        )
        context_state = {
            "schema": CONTEXT_STATE_SCHEMA,
            "status": "success",
            "fingerprint": stable_hash(
                {
                    "task_id": task["task_id"],
                    "context_pack": hashlib.sha256(output_body).hexdigest(),
                }
            ),
            "source_segments_digest": attestation["ordered_script_hash"],
            "source_inventory_fingerprint": task["source_inventory_fingerprint"],
            "output_sha256": hashlib.sha256(output_body).hexdigest(),
            "output_size": len(output_body),
            "attestation": attestation,
            "execution": {
                "status": "success",
                "accounting_basis": (
                    "validated_bound_fragments"
                    if native_manifest_bound
                    else "validated_final_artifact_not_provider_call_count"
                ),
                "total_parts": validated_parts,
                "completed_parts": validated_parts,
                "fallback_part_count": 0,
                "deterministic_segment_count": deterministic_segment_count,
                "semantic_segment_count": semantic_segment_count,
                "parts": (
                    [
                        {
                            "part": index,
                            "provider": fragment[2]["provider"],
                            "routes_attempted": 1,
                            "status": "success",
                            "result_id": fragment[2]["result_id"],
                        }
                        for index, fragment in enumerate(completed_fragments, start=1)
                    ]
                    if native_manifest_bound
                    else [
                        {
                            "part": 1,
                            "provider": str(task.get("context_route", "")),
                            "routes_attempted": 1,
                            "status": "success",
                        }
                    ]
                    if validated_parts
                    else []
                ),
            },
        }
        state_path = (work_dir / str(task["output_state"])).resolve()
        if not is_within(state_path, work_dir):
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: context state path left the workspace"
            )
        _atomic_json(state_path, context_state)
    elif result["worker_role"] == "image_ui_inventory" and result["status"] == "success":
        verify_image_ui_inventory(
            work_dir,
            artifact_path,
            game_roots=[Path(item) for item in task.get("source_roots", [])],
        )
    elif result["worker_role"] == "image_localizer" and result["status"] == "success":
        inventory_path = (work_dir / str(task.get("input_image_inventory", ""))).resolve()
        if (
            not is_within(inventory_path, work_dir)
            or not inventory_path.is_file()
            or _digest(inventory_path) != task.get("input_image_inventory_sha256")
        ):
            raise ConfigurationError(
                "E_DATA_PLANE_RESULT_INVALID: bound image inventory changed"
            )
        verify_image_localization_results(
            work_dir,
            inventory_path,
            artifact_path,
            game_roots=[Path(item) for item in task.get("source_roots", [])],
        )
    current_relay = relay.get("schema") == DATA_PLANE_RELAY_SCHEMA
    execution = _relay_execution_summary(work_dir, relay)
    receipt = {
        "schema": (
            DATA_PLANE_RECEIPT_SCHEMA
            if current_relay
            else LEGACY_DATA_PLANE_RECEIPT_SCHEMA
        ),
        "task_id": relay["task_id"],
        "worker_role": result["worker_role"],
        "stage": result["stage"],
        "status": result["status"],
        "counts": result["counts"],
        "issue_codes": result["issue_codes"],
        "artifact_paths": result["artifact_paths"],
        "queue_transitions": result["queue_transitions"],
        "control_generation": int(control_plane_state(work_dir).get("generation", 0)),
        **({"execution": execution} if current_relay else {}),
    }
    receipt_path = (
        work_dir
        / ".game-l10n"
        / "data-plane"
        / str(result["worker_role"])
        / "receipt.json"
    )
    _atomic_json(receipt_path, receipt)
    return {
        "schema": receipt["schema"],
        "status": receipt["status"],
        "worker_role": receipt["worker_role"],
        "stage": receipt["stage"],
        "counts": receipt["counts"],
        "issue_codes": receipt["issue_codes"],
        **({"execution": execution} if current_relay else {}),
        "receipt_path": _relative(work_dir, receipt_path),
    }


def _seal_ended_data_plane_task(
    work_dir: Path,
    relay: dict[str, Any],
    task: dict[str, Any] | None = None,
    *,
    issue_code: str,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close an ended worker categorically without interpreting its response."""

    current_relay = relay.get("schema") == DATA_PLANE_RELAY_SCHEMA
    if execution is None:
        execution = _relay_execution_summary(work_dir, relay)
    counts = {"total": 1, "success": 0, "error": 1, "pending": 1}
    if (
        relay.get("worker_role") == "context_analyzer"
        and isinstance(task, dict)
        and "input_context_manifest" in task
    ):
        try:
            counts = _context_safe_counts(work_dir, task)
            counts["error"] = 1 if counts["pending"] else 0
        except (ConfigurationError, ValueError, TypeError, KeyError, ProviderError):
            pass
    receipt = {
        "schema": (
            DATA_PLANE_RECEIPT_SCHEMA
            if current_relay
            else LEGACY_DATA_PLANE_RECEIPT_SCHEMA
        ),
        "task_id": relay["task_id"],
        "worker_role": relay["worker_role"],
        "stage": relay["stage"],
        "status": "error",
        "counts": counts,
        "issue_codes": [issue_code],
        "artifact_paths": [],
        "queue_transitions": [],
        "control_generation": int(control_plane_state(work_dir).get("generation", 0)),
        **({"execution": execution} if current_relay else {}),
    }
    receipt_path = (
        work_dir
        / ".game-l10n"
        / "data-plane"
        / str(relay["worker_role"])
        / "receipt.json"
    )
    _atomic_json(receipt_path, receipt)
    return {
        "schema": receipt["schema"],
        "status": receipt["status"],
        "worker_role": receipt["worker_role"],
        "stage": receipt["stage"],
        "counts": receipt["counts"],
        "issue_codes": receipt["issue_codes"],
        **({"execution": execution} if current_relay else {}),
        "receipt_path": _relative(work_dir, receipt_path),
    }


def seal_data_plane_task(
    work_dir: Path,
    relay_path: Path,
    *,
    worker_ended: bool = False,
) -> dict[str, Any]:
    """Seal one data-plane task, including a worker ending without valid output."""

    relay, task = _load_bound_data_plane_relay(work_dir, relay_path)
    execution = _relay_execution_summary(work_dir, relay)
    if not execution["complete"]:
        if worker_ended:
            return _seal_ended_data_plane_task(
                work_dir,
                relay,
                task,
                issue_code="relay_execution_incomplete",
                execution=execution,
            )
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_EXECUTION_INCOMPLETE: required worker slots or finalizer did not finish"
        )
    try:
        return _seal_bound_data_plane_task(work_dir, relay, task)
    except (
        ConfigurationError,
        ProviderError,
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        json.JSONDecodeError,
    ):
        if not worker_ended:
            raise
        envelope_path = (work_dir / str(relay["expected_envelope"])).resolve()
        return _seal_ended_data_plane_task(
            work_dir,
            relay,
            task,
            issue_code=(
                "malformed_response" if envelope_path.is_file() else "task_incomplete"
            ),
            execution=execution,
        )
