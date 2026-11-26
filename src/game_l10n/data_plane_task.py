from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .context_analysis import (
    CONTEXT_PREFLIGHT_POLICY_VERSION,
    CONTEXT_PREFLIGHT_SCHEMA,
    CONTEXT_STATE_SCHEMA,
    build_context_preflight,
    chunk_localization_inputs,
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
from .content_source_policy import (
    content_source_policy,
    validate_content_source_policy,
    validate_output_provenance,
)
from .control_plane import (
    ISSUE_CODES_BY_STAGE,
    control_plane_state,
    safe_artifact_path,
    require_control_plane_entry,
    require_control_plane_ready,
    validate_control_envelope,
)
from .coverage import require_inventory_attestation, validate_inventory_display_scope
from .errors import ConfigurationError, ControlEnvelopeIssueCodeError, ProviderError
from .extractor_preparation import (
    ACTION as EXTRACTOR_PREPARATION_ACTION,
    ATTESTATION_PATH as EXTRACTOR_ATTESTATION_PATH,
    PURPOSE as EXTRACTOR_PREPARATION_PURPOSE,
    apply_extractor_requirements,
    require_extractor_preparation,
    preparation_required,
)
from .formats import load_dataclass_list, read_segments
from .image_localization import (
    SCOPE_PATH as IMAGE_SCOPE_PATH,
    public_image_localization_state,
    require_image_localization_requested,
    verify_image_localization_results,
    verify_image_ui_inventory,
)
from .inventory_scope import DISPLAY_SCOPE_SCHEMA, TEXT_ELIGIBILITY_CONTRACT
from .locale_probe import INVENTORY_CHECKS as LOCALE_INVENTORY_CHECKS, PURPOSE as LOCALE_PROBE_PURPOSE
from .locale_probe import SCHEMA as LOCALE_PROBE_SCHEMA, validate_locale_probe
from .locale_probe import locale_inventory_status
from .screen_review_policy import screen_review_skip_allowed
from .inventory_constraints import (
    CONSTRAINT_ACTION,
    CONSTRAINT_CODE,
    CONSTRAINT_REPAIR_ACTIONS,
    CONSTRAINT_SCHEMA,
    artifact_digest,
    has_inventory_constraint,
    inventory_constraint_binding,
    observe_inventory_constraints,
    persistent_inventory_constraint,
)
from .inventory_scope import ActiveControlStorageError
from .language_policy import language_binding
from .models import CharacterProfile, ContextMode, ContextPack, GlossaryEntry, ProjectContext, Segment
from .native_runtime import (
    RUNTIME_ISSUES, _stale_unadmitted_omp_relay, core_runtime, native_launch_evidence,
    native_launch_summary, native_runtime, require_native_workers_ended,
)
from .omp_isolation import ISOLATION_ISSUE, runtime_isolation, tool_transport_contract
from .privacy import is_within
from .sdk_inventory_repair import inventory_repair_tool, reconcile_cached_sdk_inventory
from .store import exclusive_file_lock, stable_hash


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
CONTEXT_WORKER_EXECUTION_SCHEMA = "parr0t.context-worker-execution.v1"
INVENTORY_WORKER_EXECUTION_SCHEMA = "parr0t.inventory-worker-execution.v1"
DATA_PLANE_OPERATIONAL_FAILURE_CODES = {
    "provider_operational_error",
    "execution_guard_denied",
}
CONTEXT_RESULT_ISSUE_CODES = {
    "fallback_required",
    "malformed_response",
    "output_truncated",
    "provider_error",
    "rate_limited",
    "quota_exhausted",
    "authentication_error",
    "timeout",
    "capacity_error",
    "worker_error",
}

MIN_CONTEXT_CHUNK_CHARS = 2048


def _chunk_localization(chunk: list[Segment], localization: dict[str, Any] | None) -> dict[str, Any]:
    inputs = chunk_localization_inputs(chunk, **(localization or {})) if localization else {}
    return {"localization_inputs": inputs} if inputs else {}


def _context_localization(work_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    """Same locked inputs the API analyzer receives; paths stay inside the workspace."""
    files = config.get("files", {})

    def load(key: str, default: str, cls: type) -> list[Any]:
        path = (work_dir / str(files.get(key, default))).resolve()
        if not is_within(path, work_dir):
            raise ConfigurationError(f"E_DATA_PLANE_TASK_PATH: files.{key} left the workspace")
        return load_dataclass_list(path, cls)

    return {
        # Native workspaces may carry a partial project block; absent facts stay empty.
        "project": ProjectContext(
            **{
                "game_title": "", "source_language": "", "target_language": "",
                **{
                    key: value
                    for key, value in config.get("project", {}).items()
                    if key in {item.name for item in fields(ProjectContext)}
                },
            }
        ),
        "glossary": load("glossary", "context/glossary.json", GlossaryEntry),
        "characters": load("characters", "context/characters.json", CharacterProfile),
    }


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
    localization: dict[str, Any] | None = None,
    content_source_policy: dict[str, Any] | None = None,
    semantic_route: str = "",
    runtime_model: str = "",
) -> dict[str, Any]:
    inputs = _chunk_localization(segments, localization).get("localization_inputs")
    binding = {
        # A fragment analyzed against other locked inputs (glossary, profiles, style) is stale.
        **({"localization_inputs_digest": stable_hash(inputs)} if inputs else {}),
        "source_inventory_fingerprint": source_inventory_fingerprint,
        "source_digest": stable_hash([asdict(item) for item in segments]),
        "ordered_evidence_ids": [item.id for item in segments],
        "context_preflight_policy_version": CONTEXT_PREFLIGHT_POLICY_VERSION,
        "context_mode": resolved_context_mode,
        "context_contract_fingerprints": contract_fingerprints,
        **(
            {
                "semantic_route": semantic_route,
                "runtime_model": runtime_model,
            }
            if content_source_policy is not None
            else {}
        ),
        **(
            {"content_source_policy_digest": content_source_policy["policy_digest"]}
            if content_source_policy is not None
            else {}
        ),
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
    content_source_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_fields = {
        "schema",
        "binding_id",
        "result_id",
        "binding",
        "provider",
        "context_part",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) not in (base_fields, base_fields | {"content_provenance"})
    ):
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
            {
                "binding": binding,
                "provider": provider,
                "context_part": context_part,
                **(
                    {"content_provenance": payload["content_provenance"]}
                    if "content_provenance" in payload
                    else {}
                ),
            }
        )
    ):
        raise ValueError("invalid context fragment")
    if content_source_policy is not None:
        validate_output_provenance(
            payload.get("content_provenance"),
            policy=content_source_policy,
            model=str(binding.get("runtime_model", provider)),
            route=str(binding.get("semantic_route", provider)),
        )
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
    content_source_policy: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    for path, payload in index.get(str(binding["binding_id"]), []):
        try:
            return path, _validate_context_fragment(
                payload,
                binding=binding,
                rows=rows,
                content_source_policy=content_source_policy,
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
    localization: dict[str, Any] | None = None,
    content_source_policy: dict[str, Any] | None = None,
    semantic_route: str = "",
    runtime_model: str = "",
    source_policies: list[dict[str, Any]] | None = None,
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
    policies = {
        policy["policy_digest"]: validate_content_source_policy(policy)
        for policy in (source_policies or [])
    }
    if content_source_policy is not None:
        policies[content_source_policy["policy_digest"]] = content_source_policy
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
            original_policy = policies.get(binding.get("content_source_policy_digest"))
            if binding.get("content_source_policy_digest") is not None and original_policy is None:
                continue
            expected_binding = _context_binding(
                chunk,
                source_inventory_fingerprint=source_inventory_fingerprint,
                resolved_context_mode=resolved_mode,
                contract_fingerprints=contract_fingerprints,
                localization=localization,
                content_source_policy=original_policy,
                semantic_route=str(binding.get("semantic_route", "")),
                runtime_model=str(binding.get("runtime_model", "")),
            )
            if binding_id != expected_binding["binding_id"]:
                continue
            rows = [_context_row(item) for item in chunk]
            found = _find_context_fragment(
                fragment_index,
                binding=expected_binding,
                rows=rows,
                content_source_policy=original_policy,
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
                localization=localization,
                content_source_policy=content_source_policy,
                semantic_route=semantic_route,
                runtime_model=runtime_model,
            )
            units.append(
                {
                    "source_index": positions[chunk[0].id],
                    "binding": binding,
                    "binding_id": binding["binding_id"],
                    "rows": [_context_row(item) for item in chunk],
                    "initial_status": "pending",
                    "fragment_path": "",
                    **_chunk_localization(chunk, localization),
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
        "content_source_policies": list(policies.values()),
        "context_contract_fingerprints": contract_fingerprints,
        **(
            {"content_source_policy_digest": content_source_policy["policy_digest"]}
            if content_source_policy is not None
            else {}
        ),
        **(
            {"content_source_policy": content_source_policy}
            if content_source_policy is not None
            else {}
        ),
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


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def _relative(work_dir: Path, path: Path) -> str:
    resolved = path.resolve()
    if not is_within(resolved, work_dir):
        raise ConfigurationError("E_DATA_PLANE_TASK_PATH: task path left the workspace")
    return resolved.relative_to(work_dir.resolve()).as_posix()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _data_plane_route_chain(
    config: dict[str, Any], worker_role: str, runtime: dict[str, Any] | None,
) -> list[str]:
    """Use configured role routes, never require an unused legacy model slot."""
    routing = config.get("routing", {})
    if not isinstance(routing, dict):
        raise ConfigurationError("E_DATA_PLANE_ROUTE_INVALID: routing must be an object")
    configured = routing.get("data_plane_routes", {})
    if not isinstance(configured, dict) or set(configured) - set(DATA_PLANE_PROFILES):
        raise ConfigurationError("E_DATA_PLANE_ROUTE_INVALID: unsupported data-plane route role")
    if worker_role == "context_analyzer":
        return []
    routes = configured.get(worker_role)
    if routes is None:
        role_routes = routing.get("translators" if worker_role == "image_localizer" else "context_analyzers", [])
        candidates = [
            route for route in role_routes
            if isinstance(route, str) and route.startswith("native-")
            and (runtime is None or route in runtime["models"])
        ]
        if candidates:
            routes = candidates
        elif runtime is not None:
            default_route = next(
                (route for route in ("native-context", "native-sol") if route in runtime["models"]), None,
            )
            if default_route is not None:
                routes = [default_route]
            elif len(runtime["models"]) == 1:
                routes = list(runtime["models"])
            else:
                raise ConfigurationError("E_DATA_PLANE_ROUTE_REQUIRED: configure the exact native route for this role")
        else:
            routes = ["native-sol"]
    if (
        not isinstance(routes, list) or not routes
        or any(not isinstance(route, str) or re.fullmatch(r"native-[a-z0-9][a-z0-9._-]*", route) is None for route in routes)
        or len(set(routes)) != len(routes)
    ):
        raise ConfigurationError("E_DATA_PLANE_ROUTE_INVALID: invalid configured native route chain")
    return list(routes)


def _approved_operational_fallback(task: dict[str, Any], issue_codes: list[str]) -> bool:
    """Only the immutable task policy can authorize its observed failure category."""
    policy = task.get("execution_environment_policy")
    observed = set(issue_codes) & DATA_PLANE_OPERATIONAL_FAILURE_CODES
    return bool(
        observed and isinstance(policy, dict)
        and policy.get("schema") == "parr0t.execution-environment-policy.v1"
        and policy.get("host") == "omp" and policy.get("authorization") == "user_requested"
        and policy.get("fallback_strategy") == "next_configured_native_route"
        and policy.get("preserve_successes") is True and policy.get("preserve_isolation") is True
        and policy.get("silent_model_switch_allowed") is False
        and isinstance(policy.get("fallback_on"), list)
        and all(isinstance(code, str) for code in policy["fallback_on"])
        and observed <= set(policy["fallback_on"])
    )


def _validated_image_candidate_preparation(
    work_dir: Path, config: dict[str, Any], evidence: dict[str, Any],
) -> tuple[dict[str, Any], set[str]]:
    from .image_localization import _safe_relative, require_image_inspection_requested

    policy = require_image_inspection_requested(work_dir)
    context = config["extractor_requirements"]["source_metadata"]["image_candidate_discovery"]
    if context["requested_scope"] != policy["requested_scope"]:
        raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
    original_path = (work_dir / context["path"]).resolve()
    _relative(work_dir, original_path)
    original_body = original_path.read_bytes()
    if hashlib.sha256(original_body).hexdigest() != context["sha256"]:
        raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
    original = json.loads(original_body)
    if (
        not isinstance(original, dict) or original.get("schema") != "parr0t.image-inspection-candidates.v1"
        or original.get("requested_scope") != policy["requested_scope"]
        or not isinstance(original.get("unexamined_sources"), list)
        or not isinstance(original.get("resolved_source_bindings", []), list)
    ):
        raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
    cache = (work_dir / "inputs/image-assets").resolve()
    if not is_within(cache, work_dir):
        raise ConfigurationError("E_IMAGE_CANDIDATE_SOURCE_INVALID")
    roots = [Path(root).resolve() for root in config["game_roots"] if Path(root).resolve() != cache]
    if original.get("source_roots") != [str(root) for root in roots]:
        raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
    path = work_dir / "inputs/image-inspection-candidates.json"
    body = path.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    if not any(record.get("outputs", {}).get("inputs/image-inspection-candidates.json") == digest for record in evidence["checks"]):
        raise ConfigurationError("E_IMAGE_CANDIDATE_OUTPUT_UNATTESTED")
    payload = json.loads(body)
    if (
        payload.get("schema") != "parr0t.image-inspection-candidates.v1"
        or payload.get("requested_scope") != policy["requested_scope"]
        or payload.get("unexamined_sources") != []
        or not isinstance(payload.get("source_bindings"), list)
        or not isinstance(payload.get("bound_local_assets"), list)
    ):
        raise ConfigurationError("E_IMAGE_CANDIDATE_DISCOVERY_INCOMPLETE")
    source_records = [*original["unexamined_sources"], *original.get("resolved_source_bindings", [])]
    originals = {
        (item.get("source_root_index"), item.get("relative_path")): item.get("sha256")
        for item in source_records
    }
    if len(originals) != len(source_records):
        raise ConfigurationError("E_IMAGE_CANDIDATE_SOURCE_INVALID")
    resolved: set[str] = set()
    covered = set()
    for binding in payload["source_bindings"]:
        if not isinstance(binding, dict) or set(binding) != {"source_root_index", "relative_path", "sha256", "status"}:
            raise ConfigurationError("E_IMAGE_CANDIDATE_SOURCE_INVALID")
        index, relative = binding["source_root_index"], _safe_relative(binding["relative_path"])
        key = (index, relative)
        if (
            type(index) is not int or not 0 <= index < len(roots)
            or binding["status"] != "complete" or key in covered
            or not isinstance(binding["sha256"], str)
            or originals.get(key) != binding["sha256"]
        ):
            raise ConfigurationError("E_IMAGE_CANDIDATE_SOURCE_INVALID")
        source = (roots[index] / relative).resolve()
        if not is_within(source, roots[index]) or not source.is_file() or _digest(source) != binding["sha256"]:
            raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
        resolved.add(str(source))
        covered.add(key)
    if covered != set(originals):
        raise ConfigurationError("E_IMAGE_CANDIDATE_DISCOVERY_INCOMPLETE")
    seen = set()
    for entry in payload["bound_local_assets"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ConfigurationError("E_IMAGE_CANDIDATE_SOURCE_INVALID")
        source = Path(entry["path"]).resolve()
        if (
            str(source) != entry["path"] or str(source) in seen
            or not any(is_within(source, root) for root in [*roots, cache])
            or not source.is_file() or _digest(source) != entry["sha256"]
        ):
            raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
        seen.add(str(source))
    return payload, resolved


def validate_image_candidate_preparation(
    work_dir: Path, config: dict[str, Any], evidence: dict[str, Any],
) -> None:
    """Require real complete output before the new SDK capability is ready."""
    if "bounded-image-font-candidate-discovery" not in (config.get("extractor_requirements") or {}).get("required_capabilities", []):
        return
    try:
        _validated_image_candidate_preparation(work_dir.resolve(), config, evidence)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ConfigurationError("E_IMAGE_CANDIDATE_DISCOVERY_INCOMPLETE") from error


def _sdk_image_candidates(
    work_dir: Path, config: dict[str, Any] | None = None,
    sdk_activation: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], set[str]] | None:
    if not (work_dir / "inputs/image-inspection-candidates.json").is_file():
        return None
    try:
        if config is None:
            from .workspace import load_config
            config = load_config(work_dir)
        if "bounded-image-font-candidate-discovery" not in (config.get("extractor_requirements") or {}).get("required_capabilities", []):
            return None
        evidence = json.loads((work_dir / EXTRACTOR_ATTESTATION_PATH).read_text(encoding="utf-8"))
        activation = sdk_activation if sdk_activation is not None else evidence["binding"]["activation"]
        require_extractor_preparation(work_dir, config, activation)
        return _validated_image_candidate_preparation(work_dir, config, evidence)
    except (ConfigurationError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def image_candidate_source_roots(
    work_dir: Path, game_roots: list[Path], *, config: dict[str, Any] | None = None,
    sdk_activation: dict[str, Any] | None = None,
) -> list[Path]:
    """Expose only readiness-attested derived image inputs, without execution."""
    work_dir = work_dir.resolve()
    cache = (work_dir / "inputs/image-assets").resolve()
    roots = [Path(root).resolve() for root in game_roots if Path(root).resolve() != cache]
    if config is None and (work_dir / "inputs/image-inspection-candidates.json").is_file():
        try:
            from .workspace import load_config
            config = load_config(work_dir)
        except (ConfigurationError, OSError, ValueError):
            return roots
    if config is not None and roots != [
        Path(root).resolve() for root in config.get("game_roots", [])
        if Path(root).resolve() != cache
    ]:
        return roots
    validated = _sdk_image_candidates(work_dir, config, sdk_activation)
    if validated is not None and any(is_within(Path(entry["path"]), cache) for entry in validated[0]["bound_local_assets"]):
        return [*roots, cache]
    return roots


def _image_candidate_repair(
    work_dir: Path, output_dir: Path, config: dict[str, Any], discovery: dict[str, Any],
    inputs: dict[str, str],
) -> dict[str, Any]:
    previous = config.get("extractor_requirements") or {}
    requirements = {
        **previous,
        "required_capabilities": sorted(set(previous.get("required_capabilities", [])) | {"bounded-image-font-candidate-discovery"}),
        "required_formats": sorted(set(previous.get("required_formats", [])) | {"image-font-resources"}),
        "source_metadata": {
            **previous.get("source_metadata", {}),
            "image_candidate_discovery": {
                "path": inputs["input_image_candidates"], "sha256": inputs["input_image_candidates_sha256"],
                "requested_scope": discovery["requested_scope"],
                "output_manifest": "inputs/image-inspection-candidates.json", "asset_root": "inputs/image-assets",
                "output_contract": {
                    "schema": "parr0t.image-inspection-candidates.v1",
                    "required_fields": ["schema", "requested_scope", "bound_local_assets", "unexamined_sources", "source_bindings"],
                    "bound_local_assets": "unique canonical absolute file paths and sha256, under original roots or fixed asset_root",
                    "source_bindings": "one {source_root_index,relative_path,sha256,status:complete} for every original unexamined source AND retained resolved_source_bindings entry; unchanged bytes required",
                    "unexamined_sources": "must be empty; incomplete discovery cannot attest readiness",
                    "checks": "functional check output must be the exact output_manifest and attest every decoded asset hash; no echo or fabricated absence",
                },
                "observed_sdk_output_sha256": (
                    _digest(work_dir / "inputs/image-inspection-candidates.json")
                    if (work_dir / "inputs/image-inspection-candidates.json").is_file() else ""
                ),
            },
        },
    }
    metadata = previous.get("source_metadata", {})
    prior_context = metadata.get("image_candidate_discovery")
    active_context = requirements["source_metadata"]["image_candidate_discovery"]
    if prior_context is not None and prior_context != active_context:
        history = metadata.get("image_candidate_discovery_history", [])
        if not isinstance(prior_context, dict) or not isinstance(history, list):
            raise ConfigurationError("E_IMAGE_CANDIDATE_REQUIREMENTS_INVALID")
        requirements["source_metadata"]["image_candidate_discovery_history"] = [*history, prior_context]
    path = output_dir / "candidate-inputs" / (stable_hash(requirements) + "-requirements.json")
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != requirements:
            raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
    else:
        _atomic_json(path, requirements)
    return {
        "status": "pending", "worker_role": "image_ui_inventory", "dispatch_allowed": False,
        "next_action": EXTRACTOR_PREPARATION_ACTION, "candidate_discovery": inputs["input_image_candidates"],
        "extractor_requirements_path": _relative(work_dir, path), "extractor_requirements_sha256": _digest(path),
        "issue_codes": ["image_source_inventory_required"], "repair_actions": [EXTRACTOR_PREPARATION_ACTION],
    }


def _image_inventory_candidates(
    work_dir: Path, output_dir: Path, roots: list[Path], inventory_path: Path,
    config: dict[str, Any], sdk_activation: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Discover exact read-only candidates without granting a source-tree reader."""
    from .image_localization import (
        bound_inspection_assets, bound_local_assets, require_image_inspection_requested,
    )

    policy = require_image_inspection_requested(work_dir)
    image_suffixes = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".dib", ".tif", ".tiff",
        ".tga", ".dds", ".ktx", ".ktx2", ".pvr", ".astc", ".pkm", ".basis",
        ".exr", ".hdr", ".svg", ".ico", ".cur", ".icns", ".psd", ".avif", ".heic", ".heif",
    }
    font_suffixes = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".fnt", ".fon", ".bdf", ".pcf"}
    non_media_suffixes = {
        ".txt", ".json", ".jsonl", ".csv", ".tsv", ".xml", ".yaml", ".yml",
        ".ini", ".cfg", ".conf", ".md", ".log", ".css", ".js", ".mjs", ".cjs",
        ".ts", ".py", ".pyc", ".rpy", ".rpyc", ".lua", ".rb", ".ks", ".tjs",
        ".cs", ".h", ".c", ".cpp", ".hlsl", ".glsl", ".shader", ".pdb", ".map",
        ".ogg", ".wav", ".mp3", ".flac", ".aac", ".m4a", ".mp4", ".webm", ".avi",
        ".ogv", ".mov", ".mkv", ".srt", ".vtt", ".sig", ".manifest",
    }
    font_only = policy["requested_scope"] == "font_visual_inspection"
    suffixes = font_suffixes if font_only else image_suffixes | font_suffixes
    known_suffixes = non_media_suffixes | image_suffixes | font_suffixes
    assets: dict[str, dict[str, str]] = {}
    candidates: set[Path] = set()
    unexamined: list[dict[str, Any]] = []
    inputs: dict[str, str] = {}
    metadata_issues: list[str] = []

    def snapshot(path: Path, name: str) -> Path:
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        destination = (output_dir / "candidate-inputs" / digest / name).resolve()
        _relative(work_dir, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if _digest(destination) != digest:
                raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
        else:
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_bytes(body)
            temporary.replace(destination)
        return destination
    sdk_candidates = _sdk_image_candidates(work_dir, config, sdk_activation)
    resolved_sources: set[str] = set()
    if sdk_candidates is not None:
        sdk_payload, resolved_sources = sdk_candidates
        for entry in sdk_payload["bound_local_assets"]:
            assets[entry["path"]] = entry
        sdk_input = snapshot(work_dir / "inputs/image-inspection-candidates.json", "sdk-image-candidates.json")
        inputs["input_image_sdk_candidates"] = _relative(work_dir, sdk_input)
        inputs["input_image_sdk_candidates_sha256"] = _digest(sdk_input)

    if inventory_path.is_file():
        try:
            prior_inventory = snapshot(inventory_path, "image-inventory.json")
            for entry in bound_local_assets(work_dir, prior_inventory, game_roots=roots, inspection_only=True):
                if Path(entry["path"]).suffix.lower() in suffixes:
                    assets[entry["path"]] = entry
            inputs["input_image_inventory"] = _relative(work_dir, prior_inventory)
            inputs["input_image_inventory_sha256"] = _digest(prior_inventory)
        except (ConfigurationError, OSError, ValueError):
            metadata_issues.append("prior_image_inventory_unavailable")
    attestation_path = work_dir / ".game-l10n/inventory-attestation.json"
    if policy["status"] == "requested" and attestation_path.is_file():
        try:
            attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
            primary = Path(attestation["primary_path"]).resolve()
            if not is_within(primary, work_dir):
                raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_INVALID")
            verified = require_inventory_attestation(work_dir, primary)
            binding = verified["file_bindings"]["primary"]
            metadata = {
                "schema": "parr0t.image-source-inventory-metadata.v1",
                "primary_path": _relative(work_dir, primary), "primary_sha256": _digest(primary),
                "file_binding": binding, "raw_source_text_is_not_bound": True,
            }
            source_inventory = output_dir / "candidate-inputs" / (stable_hash(metadata) + "-source-inventory.json")
            if source_inventory.exists():
                if json.loads(source_inventory.read_text(encoding="utf-8")) != metadata:
                    raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
            else:
                _atomic_json(source_inventory, metadata)
            current = primary.stat()
            if binding != {"size": current.st_size, "mtime_ns": current.st_mtime_ns}:
                raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
            inputs["input_image_source_inventory"] = _relative(work_dir, source_inventory)
            inputs["input_image_source_inventory_sha256"] = _digest(source_inventory)
        except (ConfigurationError, OSError, ValueError, KeyError, TypeError):
            metadata_issues.append("canonical_source_inventory_unavailable")
    for root_index, root in enumerate(roots):
        if not root.is_dir():
            raise ConfigurationError("E_IMAGE_CANDIDATE_SOURCE_INVALID: source root is not a directory")

        def failed(error: OSError) -> None:
            unexamined.append({"source_root_index": root_index, "reason": "source_directory_unreadable"})

        for directory, names, filenames in os.walk(root, topdown=True, onerror=failed, followlinks=False):
            folder = Path(directory)
            kept = []
            for name in sorted(names):
                path = folder / name
                if name in {".game-l10n", ".git", ".omp"} or path.resolve() in {work_dir / ".game-l10n", work_dir / "localized"}:
                    continue
                try:
                    info = path.lstat()
                    linked = stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                except OSError:
                    linked = True
                if linked:
                    unexamined.append({"source_root_index": root_index, "relative_path": path.relative_to(root).as_posix(), "reason": "linked_source_unexamined"})
                else:
                    kept.append(name)
            names[:] = kept
            for name in sorted(filenames):
                path = folder / name
                relative = path.relative_to(root).as_posix()
                try:
                    info = path.lstat()
                    linked = stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                    if linked or not stat.S_ISREG(info.st_mode):
                        raise OSError()
                    canonical = path.resolve()
                    if str(canonical) in assets:
                        continue
                    if str(canonical) in resolved_sources:
                        continue
                    if path.suffix.lower() in suffixes:
                        candidates.add(canonical)
                    elif path.suffix.lower() not in known_suffixes:
                        with path.open("rb") as source:
                            header = source.read(64)
                        font = header[:4] in {b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"wOFF", b"wOF2", b"true", b"typ1"}
                        image = (
                            header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"DDS "))
                            or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
                        )
                        if font or (image and not font_only):
                            candidates.add(canonical)
                        elif not image:
                            unexamined.append({"source_root_index": root_index, "relative_path": relative, "sha256": _digest(canonical), "reason": "opaque_source_requires_sdk_inventory"})
                except OSError:
                    unexamined.append({"source_root_index": root_index, "relative_path": relative, "reason": "source_file_unexamined"})
    if candidates:
        for entry in bound_inspection_assets(work_dir, sorted(candidates), game_roots=roots):
            assets[entry["path"]] = entry
    discovery = {
        "schema": "parr0t.image-inspection-candidates.v1",
        "requested_scope": policy["requested_scope"], "request_revision": policy["request_revision"],
        "method": "canonical_inventory_and_read_only_file_discovery",
        "source_roots": [str(root) for root in roots],
        "asset_source_roots": [
            str(root) for root in (
                [*roots, (work_dir / "inputs/image-assets").resolve()]
                if sdk_candidates is not None and any(
                    is_within(Path(entry["path"]), (work_dir / "inputs/image-assets").resolve())
                    for entry in sdk_candidates[0]["bound_local_assets"]
                ) else roots
            )
        ],
        "bound_local_assets": [assets[path] for path in sorted(assets)],
        "resolved_source_bindings": sdk_candidates[0]["source_bindings"] if sdk_candidates is not None else [],
        "unexamined_sources": unexamined, "metadata_issue_codes": sorted(set(metadata_issues)),
        "candidate_classification_is_not_display_or_completeness_evidence": True,
    }
    path = output_dir / "candidate-inputs" / (stable_hash(discovery) + ".json")
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != discovery:
            raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
    else:
        _atomic_json(path, discovery)
    inputs["input_image_candidates"] = _relative(work_dir, path)
    inputs["input_image_candidates_sha256"] = _digest(path)
    return discovery, inputs


def _operational_failure_path(relay_path: Path, task_id: str) -> Path:
    return relay_path.with_name(f"operational-failure-{task_id}.json")


ENGINE_MEDIA_BINDINGS_PURPOSE = "engine_media_bindings"
IMAGE_COHORT_REVIEW_PURPOSE = "image_cohort_visual_review"
LOCALE_LOCAL_PACKAGE_PURPOSE = "locale_local_package"
_PURPOSE_ROLES = {
    EXTRACTOR_PREPARATION_PURPOSE: "source_probe",
    ENGINE_MEDIA_BINDINGS_PURPOSE: "image_ui_inventory",
    IMAGE_COHORT_REVIEW_PURPOSE: "image_localizer",
    LOCALE_PROBE_PURPOSE: "source_probe",
    LOCALE_LOCAL_PACKAGE_PURPOSE: "source_probe",
}


def data_plane_receipt_path(work_dir: Path, worker_role: str, *, purpose: str = "") -> Path:
    """Keep each bound purpose's lifecycle separate from its worker role."""
    if worker_role not in DATA_PLANE_PROFILES:
        raise ConfigurationError("E_DATA_PLANE_TASK_ROLE: unsupported data-plane role")
    if purpose and _PURPOSE_ROLES.get(purpose) != worker_role:
        raise ConfigurationError("E_DATA_PLANE_TASK_PURPOSE: unsupported bound worker purpose")
    return work_dir / ".game-l10n" / "data-plane" / (purpose or worker_role) / "receipt.json"


def _active_data_plane_dir(stage_dir: Path) -> Path:
    locator = stage_dir / "active-launch-revision.json"
    try:
        revision = json.loads(locator.read_text(encoding="utf-8"))["revision"]
    except FileNotFoundError:
        if not os.path.lexists(locator):
            return stage_dir
        raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: launch revision locator is missing") from None
    except (ValueError, TypeError, KeyError):
        raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: invalid launch revision locator") from None
    if not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{32}", revision) is None:
        raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: invalid launch revision locator")
    destination = stage_dir / "launch-revisions" / revision
    if destination.resolve() != destination or not (destination / "relay.json").is_file():
        raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: launch revision is missing or redirected")
    return destination


def _require_current_data_plane_relay(relay_path: Path) -> None:
    relay_dir = relay_path.resolve().parent
    stage_dir = relay_dir.parent.parent if relay_dir.parent.name == "launch-revisions" else relay_dir
    if _active_data_plane_dir(stage_dir) != relay_dir:
        raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: data-plane relay was superseded")


def _revisable_data_plane_relay(work_dir: Path, relay_path: Path, relay: dict) -> bool:
    return (
        not os.path.lexists(work_dir / relay["expected_envelope"])
        and not os.path.lexists(_operational_failure_path(relay_path, relay["task_id"]))
        and _stale_unadmitted_omp_relay(work_dir, relay_path, relay)
    )


def current_data_plane_operational_failure(
    work_dir: Path, worker_role: str, *, purpose: str = ""
) -> dict[str, Any] | None:
    stage_dir = data_plane_receipt_path(work_dir, worker_role, purpose=purpose).parent
    relay_path = _active_data_plane_dir(stage_dir) / "relay.json"
    if not relay_path.is_file():
        return None
    try:
        relay = json.loads(relay_path.read_text(encoding="utf-8"))
        if not isinstance(relay, dict) or not isinstance(
            relay.get("launch_contract"), dict
        ):
            return None
        task_id = relay.get("task_id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-f0-9]{64}", task_id):
            return None
        failure_path = _operational_failure_path(relay_path, task_id)
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    expected = {
        "schema": "parr0t.data-plane-operational-failure.v1",
        "task_id": task_id,
        "worker_role": worker_role,
        "stage": relay.get("stage"),
        "control_generation": int(control_plane_state(work_dir).get("generation", 0)),
        "owner": {
            "host": relay.get("launch_contract", {}).get("host"),
            "relay_role": relay.get("relay_contract", {}).get("role"),
        },
    }
    if (
        not isinstance(failure, dict)
        or any(failure.get(key) != value for key, value in expected.items())
        or failure.get("failure_code") not in DATA_PLANE_OPERATIONAL_FAILURE_CODES
    ):
        return None
    receipt_path = data_plane_receipt_path(work_dir, worker_role, purpose=purpose)
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = {}
        if (
            isinstance(receipt, dict)
            and receipt.get("task_id") == task_id
            and receipt.get("status") == "success"
        ):
            return None
    workers = failure.get("workers")
    if (
        not isinstance(workers, list)
        or any(
            not isinstance(row, dict) or not isinstance(row.get("worker_id"), str)
            for row in workers
        )
    ):
        return None
    from .native_runtime import _load

    try:
        _, contract, ledger = _load(work_dir, relay_path)
        expected_workers = [
            {
                "task": name,
                "worker_id": row["worker_id"],
                "model": row["model"],
                "observed_model": row["observed_model"],
            }
            for name, row in ledger["workers"].items()
            if name in contract["tasks"]
        ]
    except (ConfigurationError, OSError, ValueError, TypeError, KeyError):
        return None
    if any(worker not in expected_workers for worker in workers) or (
        failure.get("terminal") is True and failure.get("terminal_workers", workers) != expected_workers
    ):
        return None
    return {
        "failure_code": failure["failure_code"],
        "worker_ids": [row["worker_id"] for row in expected_workers],
        "owner": failure.get("owner"),
        "cancellation_required": bool(expected_workers) and failure.get("terminal") is not True,
        "terminal": failure.get("terminal") is True,
    }



def _load_context_task(work_dir: Path, task_path: Path) -> dict[str, Any]:
    resolved = task_path.resolve()
    if not is_within(resolved, work_dir) or not resolved.is_file():
        raise ConfigurationError("E_CONTEXT_TASK_INVALID: task must be a workspace file")
    relay, task = _load_bound_data_plane_relay(work_dir, resolved.with_name("relay.json"))
    if (
        (work_dir / str(relay["task_path"])).resolve() != resolved
        or task.get("worker_role") != "context_analyzer"
        or task.get("stage") != "context"
    ):
        raise ConfigurationError("E_CONTEXT_TASK_INVALID: task is not bound to this relay")
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
        or (
            manifest.get("content_source_policies") is not None
            and manifest["content_source_policies"] != task.get("context_source_policies")
        )
        or (
            manifest.get("content_source_policy_digest") is not None
            and manifest.get("content_source_policy_digest")
            != task["content_source_policy"]["policy_digest"]
        )
        or (
            manifest.get("content_source_policy") is not None
            and manifest.get("content_source_policy")
            != task["content_source_policy"]
        )
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
        source_policy = next(
            (policy for policy in manifest.get("content_source_policies", [manifest.get("content_source_policy")])
             if policy is not None and policy.get("policy_digest") == binding.get("content_source_policy_digest")),
            None,
        )
        if binding.get("content_source_policy_digest") is not None and source_policy is None:
            raise ConfigurationError("E_CONTEXT_MANIFEST_INVALID: fragment source policy lineage is missing")
        if source_policy is not None:
            validate_content_source_policy(source_policy)
        found = _find_context_fragment(
            index,
            binding=binding,
            rows=rows,
            content_source_policy=source_policy,
        )
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


def _require_context_claim_binding(
    work_dir: Path,
    task: dict[str, Any],
    manifest: dict[str, Any],
    claim_path: Path,
    claim: dict[str, Any],
    *,
    slot: int | None = None,
) -> None:
    if not isinstance(claim, dict):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: claim binding is invalid")
    claim_slot = claim.get("slot")
    chunk = next((
        item for item in manifest["chunks"]
        if isinstance(item, dict) and item.get("binding_id") == claim.get("binding_id")
    ), None)
    if (
        type(claim_slot) is not int
        or not 1 <= claim_slot <= int(task["context_parallelism"]["max_workers"])
        or (slot is not None and claim_slot != slot)
        or chunk is None
    ):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: claim ownership is invalid")
    claim_root = work_dir / ".game-l10n/context-progress/claims" / task["task_id"]
    expected_path = claim_root / chunk["binding_id"] / "claim.json"
    unsigned = {
        "schema": CONTEXT_CHUNK_CLAIM_SCHEMA,
        "task_id": task["task_id"],
        "manifest_id": manifest["manifest_id"],
        "binding_id": chunk["binding_id"],
        "binding": chunk["binding"],
        "source_index": chunk["source_index"],
        "slot": claim_slot,
        "provider": task["context_route"],
        "rows": chunk["rows"],
        "content_source_policy": task["content_source_policy"],
    }
    if chunk.get("localization_inputs"):
        unsigned["localization_inputs"] = chunk["localization_inputs"]
    try:
        owner = json.loads((claim_root / "slots" / f"slot-{claim_slot}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: claim owner is unavailable") from exc
    if (
        claim_path.resolve() != expected_path.resolve()
        or set(claim) != set(unsigned) | {"claim_id", "status", "issue_code", "fragment_path"}
        or any(claim.get(key) != value for key, value in unsigned.items())
        or claim.get("claim_id") != stable_hash(unsigned)
        or not isinstance(owner, dict)
        or not isinstance(owner.get("active_claim_path"), str)
        or (work_dir / owner["active_claim_path"]).resolve() != expected_path.resolve()
    ):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: claim is not bound to this task and slot")


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
        if not is_within(active_path, work_dir) or not active_path.is_file():
            raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: active claim is unavailable")
        try:
            existing = json.loads(active_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: active claim is unreadable") from exc
        _require_context_claim_binding(work_dir, task, manifest, active_path, existing, slot=slot)
        if existing.get("status") == "claimed":
            return {
                "status": "claimed",
                "slot": slot,
                "claim_path": active_relative,
                "record_count": len(existing["rows"]),
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
            "content_source_policy": task["content_source_policy"],
            **(
                {"localization_inputs": chunk["localization_inputs"]}
                if chunk.get("localization_inputs")
                else {}
            ),
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
    _, manifest = _load_context_manifest(work_dir, task)
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
    _require_context_claim_binding(work_dir, task, manifest, resolved_claim, claim)
    if claim.get("status") != "claimed":
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: claim is not active")
    result_fields = {
        "schema",
        "claim_id",
        "status",
        "context_part",
        "issue_code",
    }
    if (
        not isinstance(result, dict)
        or set(result) not in (
            result_fields,
            result_fields | {"content_provenance"},
        )
    ):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: result shape is invalid")
    status = result.get("status")
    issue_code = result.get("issue_code")
    context_part = result.get("context_part")
    if result.get("schema") != CONTEXT_CHUNK_RESULT_SCHEMA or result.get(
        "claim_id"
    ) != claim.get("claim_id"):
        raise ConfigurationError("E_CONTEXT_FRAGMENT_INVALID: result binding is invalid")
    provider = str(claim["provider"])
    binding = claim["binding"]
    try:
        provenance = validate_output_provenance(
            result.get("content_provenance"),
            policy=task["content_source_policy"],
            model=str(binding.get("runtime_model", provider)),
            route=str(binding.get("semantic_route", provider)),
        )
    except ValueError as exc:
        if str(exc) == "E_EXTERNAL_CONTENT_PROVENANCE_DENIED":
            raise ConfigurationError(str(exc)) from None
        raise ConfigurationError("E_OUTPUT_PROVENANCE_INVALID") from None
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
    fragment = {
        "schema": CONTEXT_FRAGMENT_SCHEMA,
        "binding_id": claim["binding_id"],
        "result_id": stable_hash(
            {
                "binding": binding,
                "provider": provider,
                "context_part": context_part,
                "content_provenance": provenance,
            }
        ),
        "binding": binding,
        "provider": provider,
        "context_part": context_part,
        "content_provenance": provenance,
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
    optional = {"terminal_unstarted_slots", "finalizer_skipped"}
    if not isinstance(value, dict) or set(value) - optional != fields:
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
    terminal_unstarted = value.get("terminal_unstarted_slots", [])
    finalizer_skipped = value.get("finalizer_skipped", False)
    if value.get("tracking_required") is False:
        if (
            required < 1
            or terminal_unstarted != [] or finalizer_skipped is not False
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
        or not isinstance(terminal_unstarted, list) or type(finalizer_skipped) is not bool
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
            for slot in unstarted + unterminated + terminal_unstarted
        )
        or len(set(unstarted + unterminated + terminal_unstarted)) != len(unstarted) + len(unterminated) + len(terminal_unstarted)
        or started != required - len(unstarted) - len(terminal_unstarted)
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
        or finalizer_skipped and value["finalizer_started"]
        or value["complete"]
        is not (
            terminal + len(terminal_unstarted) == required
            and (
                not value["finalizer_required"] or value["finalizer_terminal"] or finalizer_skipped
            )
        )
    ):
        return None
    return dict(value)


def current_data_plane_receipt(
    work_dir: Path, worker_role: str, *, purpose: str = ""
) -> dict[str, Any] | None:
    """Return only a current, parent-safe receipt for status recovery."""

    profile = DATA_PLANE_PROFILES.get(worker_role)
    if profile is None:
        return None
    path = data_plane_receipt_path(work_dir, worker_role, purpose=purpose)
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


def current_locale_probe(
    work_dir: Path, config: dict[str, Any], sdk_activation: dict[str, Any],
) -> dict[str, Any] | None:
    """Read the canonical probe binding without exposing the probe artifact."""
    receipt_path = data_plane_receipt_path(work_dir, "source_probe", purpose=LOCALE_PROBE_PURPOSE)
    if not os.path.lexists(receipt_path.parent):
        return None
    pending = {
        "status": "pending", "inventory_status": "pending", "issue_code": "locale_applicability_pending",
    }
    try:
        relay_path = _active_data_plane_dir(receipt_path.parent) / "relay.json"
        _, task = _load_bound_data_plane_relay(work_dir, relay_path)
        if (
            task.get("purpose") != LOCALE_PROBE_PURPOSE
            or task.get("sdk_activation") != sdk_activation
            or task.get("language_binding") != language_binding(work_dir, config)
            or task.get("source_roots") != [str(Path(root).resolve()) for root in config.get("game_roots", [])]
        ):
            return pending
        public = current_data_plane_receipt(work_dir, "source_probe", purpose=LOCALE_PROBE_PURPOSE)
        if public is None:
            return pending
        receipt = json.loads(receipt_path.read_bytes())
        artifact = task["output_artifact"]
        if receipt.get("task_id") != task["task_id"] or receipt.get("artifact_paths") != [artifact]:
            return pending
        path = (work_dir / artifact).resolve()
        if not is_within(path, work_dir):
            return pending
        body = path.read_bytes()
        digest_path = path.with_suffix(".sha256.txt")
        if digest_path.is_file() and digest_path.read_text(encoding="ascii") != hashlib.sha256(body).hexdigest():
            return pending
        payload = json.loads(body)
        code = validate_locale_probe(payload, task["task_id"], {
            **receipt, "issue_codes": [], "target_ids": [], "route_evidence_path": "",
        })
        if public["issue_codes"] != [code] or (public.get("execution") or {}).get("complete") is not True:
            return pending
        checks = payload["checks"]
        return {
            "status": payload["outcome"], "issue_code": code,
            "inventory_status": locale_inventory_status(checks),
            "evidence_fingerprint": payload["evidence_sha256"],
            "checks": checks,
        }
    except (ConfigurationError, OSError, ValueError, KeyError, TypeError):
        return pending


def _recover_locale_envelope(
    work_dir: Path, relay_path: Path, receipt_path: Path, receipt_bytes: bytes,
    task: dict[str, Any], sdk_activation: dict[str, Any], config: dict[str, Any],
) -> dict[str, Any] | None:
    """Recover a malformed wrapper, never an invalid substantive result."""
    if (task.get("sdk_activation") != sdk_activation
            or task.get("language_binding") != language_binding(work_dir, config)
            or task.get("source_roots") != [str(Path(root).resolve()) for root in config.get("game_roots", [])]):
        return None
    from .locale_probe import validate_locale_probe

    for key in ("output_artifact", "output_envelope"):
        path = work_dir / task[key]
        if path.resolve() != path or not is_within(path, work_dir):
            return None
    artifact_path = work_dir / task["output_artifact"]
    envelope_path = work_dir / task["output_envelope"]
    try:
        body = artifact_path.read_bytes()
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return None
        partial = payload.get("outcome") == "inconclusive"
        envelope = {
            "schema": "parr0t.control-envelope.v2", "worker_role": "source_probe", "stage": "discovery",
            "status": "partial" if partial else "success", "target_ids": [],
            "counts": {"total": 1, "success": int(not partial), "error": 0, "pending": int(partial)},
            "issue_codes": [], "artifact_paths": [task["output_artifact"]],
            "queue_transitions": [], "route_evidence_path": "",
        }
        validate_locale_probe(payload, task["task_id"], envelope)
        old_envelope = envelope_path.read_bytes()
    except (ConfigurationError, OSError, ValueError):
        return None
    sealed = _seal_recovered_envelope(
        work_dir, relay_path, receipt_path, receipt_bytes, artifact_path, body,
        envelope_path, old_envelope, envelope, issue_code="E_LOCALE_PROBE_REPAIR_CHANGED",
    )
    return {
        "status": "locale_probe_recovered", "worker_role": "source_probe", "dispatch_allowed": False,
        "receipt_path": sealed["receipt_path"], "issue_codes": sealed["issue_codes"],
    }


def _seal_recovered_envelope(
    work_dir: Path, relay_path: Path, receipt_path: Path, receipt_bytes: bytes,
    artifact_path: Path, body: bytes, envelope_path: Path, old_envelope: bytes,
    envelope: dict[str, Any], *, issue_code: str,
) -> dict[str, Any]:
    with exclusive_file_lock(work_dir / ".game-l10n/native/launch.lock", issue_code="E_NATIVE_LAUNCH_BUSY", blocking=True):
        _require_current_data_plane_relay(relay_path)
        require_native_workers_ended(work_dir, relay_path)
        if (receipt_path.read_bytes() != receipt_bytes or artifact_path.read_bytes() != body
                or envelope_path.read_bytes() != old_envelope):
            raise ConfigurationError(issue_code)
        for source, prior in ((envelope_path, old_envelope), (receipt_path, receipt_bytes)):
            archive = source.with_name(f"{source.stem}.before-repair-{hashlib.sha256(prior).hexdigest()}.json")
            _atomic_bytes(archive, prior)
        _atomic_json(envelope_path, envelope)
    return seal_data_plane_task(work_dir, relay_path, worker_ended=True)


def _inventory_constraint_state(
    work_dir: Path,
    task_dir: Path,
    task: dict[str, Any],
    receipt: dict[str, Any],
    attestation: dict[str, Any],
    config: dict[str, Any],
    sdk_activation: dict[str, Any],
) -> dict[str, Any]:
    envelope = (work_dir / str(task["output_envelope"])).resolve()
    if not is_within(envelope, work_dir):
        raise ValueError("inventory envelope binding")
    unsigned = {
        "schema": CONSTRAINT_SCHEMA,
        "task_id": task["task_id"],
        "control_generation": task["control_generation"],
        "task_sha256": artifact_digest(task_dir / "task.json"),
        "relay_sha256": artifact_digest(task_dir / "relay.json"),
        "receipt_binding": stable_hash(receipt),
        "envelope_sha256": artifact_digest(envelope),
        "attestation_binding": stable_hash(attestation),
        "input_binding": inventory_constraint_binding(
            work_dir, attestation, config, sdk_activation, native_runtime(work_dir),
            task["inventory_constraint_baseline"]["evidence_paths"],
        ),
        "constraint_count": task["inventory_constraint_baseline"]["constraint_count"],
    }
    return {**unsigned, "state_fingerprint": stable_hash(unsigned)}


def _inventory_constraint_locator(
    work_dir: Path, task_dir: Path, task_id: str
) -> None:
    _atomic_json(
        work_dir / ".game-l10n/data-plane/inventory_reconciler/inventory-constraint-current.json",
        {
            "schema": "parr0t.inventory-constraint-locator.v1",
            "task_dir": _relative(work_dir, task_dir),
            "task_id": task_id,
        },
    )


def current_inventory_constraint(
    work_dir: Path,
    *,
    config: dict[str, Any],
    sdk_activation: dict[str, Any],
) -> dict[str, Any] | None:
    """Validate an exact core seal without reparsing inventory documents."""
    task_dir = work_dir / ".game-l10n/data-plane/inventory_reconciler"
    try:
        locator_path = task_dir / "inventory-constraint-current.json"
        locator = None
        if locator_path.is_file():
            locator = json.loads(locator_path.read_text(encoding="utf-8"))
            if (
                not isinstance(locator, dict)
                or set(locator) != {"schema", "task_dir", "task_id"}
                or locator["schema"] != "parr0t.inventory-constraint-locator.v1"
            ):
                return None
            task_dir = (work_dir / locator["task_dir"]).resolve()
            if not is_within(task_dir, work_dir):
                return None
        if not (task_dir / "task.json").is_file():
            return None
        task_stub = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
        if not isinstance(task_stub, dict) or re.fullmatch(
            r"[0-9a-f]{64}", str(task_stub.get("task_id", ""))
        ) is None:
            return None
        if locator is not None and locator["task_id"] != task_stub["task_id"]:
            return None
        state_path = task_dir / f"inventory-constraint.{task_stub['task_id']}.json"
        if not state_path.is_file():
            return None
        relay, task = _load_bound_data_plane_relay(work_dir, task_dir / "relay.json")
        if task.get("worker_role") != "inventory_reconciler":
            return None
        safe_receipt = current_data_plane_receipt(work_dir, "inventory_reconciler")
        receipt = json.loads(
            (work_dir / ".game-l10n/data-plane/inventory_reconciler/receipt.json").read_text(
                encoding="utf-8"
            )
        )
        if (
            safe_receipt is None
            or receipt.get("task_id") != task["task_id"]
            or receipt.get("status") == "success"
            or not has_inventory_constraint(receipt.get("issue_codes"))
            or receipt.get("execution", {}).get("complete", True) is not True
        ):
            return None
        attestation = json.loads(
            (work_dir / ".game-l10n/inventory-attestation.json").read_text(encoding="utf-8")
        )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        expected = _inventory_constraint_state(
            work_dir, task_dir, task, receipt, attestation, config, sdk_activation
        )
        if (
            state != expected
            or attestation.get("static_complete") is not False
            or attestation.get("status") != "incomplete"
            or type(state.get("constraint_count")) is not int
            or state["constraint_count"] <= 0
        ):
            return None
        return {
            "status": "pending",
            "next_action": CONSTRAINT_ACTION,
            "issue_codes": [CONSTRAINT_CODE],
            "counts": {"blocking_constraints": state["constraint_count"]},
            "binding": state["state_fingerprint"],
            "job_terminal": False,
            "dispatch_allowed": False,
            "deterministic_repair_allowed": True,
            "repair_actions": list(CONSTRAINT_REPAIR_ACTIONS),
        }
    except (ConfigurationError, OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


def _seal_inventory_constraint(
    work_dir: Path, relay: dict[str, Any], task: dict[str, Any], receipt: dict[str, Any]
) -> None:
    if (
        task.get("worker_role") != "inventory_reconciler"
        or receipt.get("status") == "success"
        or not has_inventory_constraint(receipt.get("issue_codes"))
        or receipt.get("execution", {}).get("complete", True) is not True
        or not isinstance(task.get("inventory_constraint_baseline"), dict)
    ):
        return
    from .workspace import load_config, require_sdk_activation

    try:
        attestation = json.loads(
            (work_dir / ".game-l10n/inventory-attestation.json").read_text(encoding="utf-8")
        )
        if attestation.get("reconciliation", {}).get("progress_state") != "unchanged":
            return
        config = load_config(work_dir)
        activation = require_sdk_activation(work_dir, config)
        current = observe_inventory_constraints(
            work_dir, attestation, config, activation, native_runtime(work_dir)
        )
        if not persistent_inventory_constraint(
            task["inventory_constraint_baseline"], current, attestation
        ):
            return
        task_dir = (work_dir / str(relay["task_path"])).resolve().parent
        state = _inventory_constraint_state(
            work_dir, task_dir, task, receipt, attestation, config, activation
        )
        if state["input_binding"] != current["input_binding"]:
            return
        _atomic_json(task_dir / f"inventory-constraint.{task['task_id']}.json", state)
    except (ConfigurationError, OSError, ValueError, TypeError, AttributeError, KeyError):
        # An unverified claim is an ordinary resumable receipt, never a hold.
        return


def _worker_python_transport(runtime: dict[str, Any] | None) -> tuple[dict[str, str], list[str]]:
    guarded = bool(
        runtime
        and runtime["host"] == "omp"
        and runtime.get("transport", "collaboration") == "collaboration"
        and runtime.get("omp_isolation", "guarded") == "guarded"
    )
    transport = tool_transport_contract(prompt=not guarded)
    binding = {
        "tool": transport["subagent_python_tool"],
        "argv_convention": (
            "python_arguments_without_executable" if guarded else "complete_executable_argv"
        ),
    }
    if guarded:
        binding["tool_device"] = transport["subagent_python_tool_device"]
    return binding, transport["relay_python_argv_prefix"]


def _context_worker_execution(
    work_dir: Path, task_path: Path, runtime: dict[str, Any] | None
) -> dict[str, Any]:
    """Bind worker commands before task hashing, independently of relay lifecycle."""
    transport, prefix = _worker_python_transport(runtime)
    workspace = str(work_dir.resolve())
    task = str(task_path.resolve())
    binding = ["--workspace", workspace, "--task", task]
    return {
        "schema": CONTEXT_WORKER_EXECUTION_SCHEMA,
        **transport,
        "workspace_root": workspace,
        "task_path": task,
        "substitution_policy": "replace_only_whole_declared_placeholder_arguments_never_shell_interpolate",
        "placeholders": {
            "<slot>": "relay_assigned_worker_slot_from_one_to_context_parallelism_max_workers",
            "<claim-path>": "exact_claim_path_returned_for_this_workers_assigned_slot",
            "<result-path>": "workspace_file_containing_the_result_for_that_exact_claim_id",
        },
        "command_argv": {
            "claim-context-chunk": [*prefix, "claim-context-chunk", *binding, "--slot", "<slot>"],
            "commit-context-fragment": [
                *prefix, "commit-context-fragment", *binding,
                "--claim", "<claim-path>", "--result", "<result-path>",
            ],
            "finalize-context-fragments": [*prefix, "finalize-context-fragments", *binding],
        },
        "roles": {
            "slot_worker": ["claim-context-chunk", "commit-context-fragment"],
            "finalizer_only": ["finalize-context-fragments"],
        },
    }


def _inventory_worker_execution(
    work_dir: Path, runtime: dict[str, Any] | None, deterministic_worker: dict | None
) -> dict[str, Any]:
    transport, prefix = _worker_python_transport(runtime)
    workspace = str(work_dir.resolve())
    execution = {
        "schema": INVENTORY_WORKER_EXECUTION_SCHEMA,
        **transport,
        "workspace_root": workspace,
        "core_runtime": core_runtime(),
        "substitution_policy": "replace_only_whole_declared_placeholder_arguments_never_shell_interpolate",
        "placeholders": {
            "<primary-path>": "workspace_path_of_the_staged_primary_inventory",
            "<independent-path>": "workspace_path_of_the_staged_independent_inventory",
        },
        "command_argv": {
            "verify-inventory": [
                *prefix, "verify-inventory", "--workspace", workspace,
                "--primary", "<primary-path>", "--independent", "<independent-path>",
            ],
        },
        "allowed_issue_codes": sorted(ISSUE_CODES_BY_STAGE["inventory"]),
    }
    if deterministic_worker is not None:
        cli = execution["core_runtime"]["cli_argv"]
        execution["deterministic_worker"] = {
            "script_sha256": deterministic_worker["sha256"],
            "argv": [
                *cli[:-1], str(work_dir / "tools" / deterministic_worker["path"]),
                "--workspace", workspace, "--core-entry", cli[-1],
                "--relay", "<relay>", "--task-sha256", "<task-sha256>",
            ],
        }
    return execution


def prepare_data_plane_task(
    work_dir: Path,
    output_dir: Path,
    *,
    worker_role: str,
    config: dict[str, Any],
    sdk_activation: dict[str, Any] | None = None,
    purpose: str = "",
    content_source_grant: Path | None = None,
    content_source_scope: str = "",
    offline_processing: list[dict[str, Any]] | None = None,
    inspection_assets: list[Path] | None = None,
    discard_unverified_receipt: str | None = None,
    deterministic_inventory_worker: bool = False,
    prepare_locale_adapter: bool = False,
    image_preview_index: Path | None = None,
    image_preview_sheets: list[int] | None = None,
    image_preview_ordinals: list[int] | None = None,
    image_review_from: Path | None = None,
) -> dict[str, Any]:
    """Prepare one exact, private discovery, inventory, or context worker task.

    The returned object is parent-safe. Source roots and private task instructions
    exist only inside the task file handed to a fresh isolated relay.
    """

    work_dir = work_dir.resolve()
    require_control_plane_ready(work_dir)
    require_control_plane_entry(work_dir)
    runtime = native_runtime(work_dir)
    isolation = runtime_isolation(work_dir, runtime) if runtime else None
    isolation_fingerprint = stable_hash(isolation) if isolation is not None else None
    control_generation = int(control_plane_state(work_dir).get("generation", 0))
    profile = DATA_PLANE_PROFILES.get(worker_role)
    if profile is None:
        raise ConfigurationError("E_DATA_PLANE_TASK_ROLE: unsupported data-plane role")
    runner = None
    if deterministic_inventory_worker:
        if worker_role != "inventory_reconciler" or not runtime or runtime["host"] != "omp" or runtime.get("omp_isolation") != "prompt":
            raise ConfigurationError("E_DATA_PLANE_TASK_ROLE: deterministic inventory worker requires prompt OMP reconciliation")
        manifest = ((sdk_activation or {}).get("workspace_tools") or {}).get("manifest", [])
        runner = next((entry for entry in manifest if entry["path"] == "reconcile_inventory_worker.py"), None)
        if runner is None:
            raise ConfigurationError("E_DATA_PLANE_TASK_SDK_ACTIVATION: deterministic inventory worker is not activated")
    data_plane_receipt_path(work_dir, worker_role, purpose=purpose)
    if purpose == ENGINE_MEDIA_BINDINGS_PURPOSE and not inspection_assets:
        raise ConfigurationError("E_IMAGE_INSPECTION_ASSET_REQUIRED")
    if (image_preview_index is not None or image_preview_sheets or image_preview_ordinals is not None) and (
        worker_role != "image_ui_inventory" or purpose or inspection_assets is not None
        or image_preview_index is None or not image_preview_sheets
    ):
        raise ConfigurationError("E_WINDOWS_IMAGE_PREVIEW_BINDING_INVALID")
    if purpose == EXTRACTOR_PREPARATION_PURPOSE:
        profile = {**profile, "artifact": EXTRACTOR_ATTESTATION_PATH}
    if purpose or (
        worker_role in {"primary_inventory", "independent_inventory"}
        and preparation_required(work_dir, config)
    ):
        from .workspace import require_sdk_activation
        current_activation = require_sdk_activation(work_dir, config)
        if sdk_activation != current_activation:
            raise ConfigurationError("E_DATA_PLANE_TASK_SDK_ACTIVATION: current activation is required")
        if not purpose:
            require_extractor_preparation(work_dir, config, current_activation)
    if prepare_locale_adapter:
        if purpose != EXTRACTOR_PREPARATION_PURPOSE:
            raise ConfigurationError("E_LOCALE_ADAPTER_PREPARATION_PURPOSE")
        probe = current_locale_probe(work_dir, config, sdk_activation or {})
        if probe is None or probe["inventory_status"] != "applicable":
            raise ConfigurationError("E_LOCALE_ADAPTER_APPLICABILITY_REQUIRED")
        previous = config.get("extractor_requirements") or {}
        requirements = {
            **previous,
            "required_capabilities": sorted(set(previous.get("required_capabilities", ()))
                                            | {"locale-ordered-inventory", "locale-value-roundtrip"}),
            "required_formats": sorted(set(previous.get("required_formats", ())) | {"active-locale-catalog"}),
            "source_metadata": {
                **previous.get("source_metadata", {}),
                "locale_catalog_adapter": {"evidence_fingerprint": probe["evidence_fingerprint"]},
            },
        }
        path = work_dir / ".game-l10n" / "data-plane" / (
            "locale-adapter-requirements-" + stable_hash(requirements) + ".json"
        )
        _atomic_json(path, requirements)
        config = {**config, "extractor_requirements": apply_extractor_requirements(work_dir, path, _digest(path))}
    locale_probe = None
    if worker_role in {"primary_inventory", "independent_inventory"}:
        locale_probe = current_locale_probe(work_dir, config, sdk_activation or {})
        if locale_probe is not None and locale_probe["inventory_status"] not in {"applicable", "unavailable"}:
            return {
                "status": "pending", "worker_role": worker_role, "dispatch_allowed": False,
                "next_action": "verify_locale_applicability", "issue_codes": [locale_probe["issue_code"]],
            }
    output_dir = output_dir.resolve()
    if not is_within(output_dir, work_dir):
        raise ConfigurationError("E_DATA_PLANE_TASK_PATH: output must stay inside the workspace")
    stage_dir = output_dir
    if discard_unverified_receipt is not None and (
        worker_role != "inventory_reconciler"
        or stage_dir != data_plane_receipt_path(work_dir, worker_role).parent
        or re.fullmatch(r"[a-f0-9]{64}", discard_unverified_receipt) is None
    ):
        raise ConfigurationError("E_NATIVE_RECEIPT_DISCARD_INVALID: require the canonical reconciler and exact task ID")
    output_dir = _active_data_plane_dir(stage_dir)
    if worker_role == "inventory_reconciler":
        blocked = current_inventory_constraint(
            work_dir, config=config, sdk_activation=sdk_activation or {}
        )
        if blocked is not None:
            return {**blocked, "worker_role": worker_role}
        if stage_dir != data_plane_receipt_path(work_dir, worker_role).parent:
            raise ConfigurationError("E_DATA_PLANE_TASK_PATH: inventory reconciliation requires its canonical stage directory")

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
    if (image_review_from is not None) != (purpose == IMAGE_COHORT_REVIEW_PURPOSE):
        raise ConfigurationError("E_WEBP_VISUAL_REVIEW_BINDING_INVALID")
    webp_review = None
    if image_review_from is not None:
        from .windows_webp_review import ERROR, _binding

        require_image_localization_requested(work_dir)
        script = str(Path(__file__).with_name("windows_webp_review.py").resolve())
        if (not offline_processing or len(offline_processing) != 1
                or not isinstance(offline_processing[0], dict)
                or not isinstance(offline_processing[0].get("argv"), list)
                or offline_processing[0]["argv"][:1] != [script]):
            raise ConfigurationError(ERROR)
        webp_review = _binding(work_dir, image_review_from)
    image_draft_scope = None
    if worker_role in {"image_ui_inventory", "image_localizer"} and webp_review is None:
        if worker_role == "image_localizer":
            require_image_localization_requested(work_dir)
        else:
            from .image_localization import require_image_inspection_requested

            require_image_inspection_requested(work_dir)
        if purpose != ENGINE_MEDIA_BINDINGS_PURPOSE:
            image_state = public_image_localization_state(work_dir)
            allowed_states = (
                {"scope_required", "scope_incomplete", "inspection_only"}
                if worker_role == "image_ui_inventory" else {"localization_required"}
            )
            if image_state.get("status") not in allowed_states:
                raise ConfigurationError("E_IMAGE_TASK_STATE: image data-plane task is not the next required action")

    artifact_relative = profile["artifact"]
    if worker_role == "context_analyzer":
        artifact_relative = str(
            config.get("files", {}).get("context_pack", artifact_relative)
        )
        if not safe_artifact_path(Path(artifact_relative)):
            raise ConfigurationError(
                "E_DATA_PLANE_TASK_PATH: files.context_pack must use [A-Za-z0-9._-] path parts"
            )
    artifact = (work_dir / artifact_relative).resolve()
    counterpart = (
        (work_dir / profile["counterpart"]).resolve()
        if profile["counterpart"]
        else None
    )
    route_chain = _data_plane_route_chain(config, worker_role, runtime)
    route_index = 0
    worker_route = route_chain[0] if route_chain else ""
    worker_model_family = "native" if route_chain else ""

    attempt = 1
    task_revision = 1
    previous_context_receipt_success = 0
    previous_context_baseline = 0
    previous_context_no_progress = 0
    previous_issue_codes: list[str] = []
    previous_receipt_closes = False
    runtime_repaired = False
    retry_repaired_runtime = False
    replacement: tuple[Path, dict, str] | None = None
    require_prior_terminal_evidence = False
    previous_receipt_bytes: bytes | None = None
    previous_receipt_path = data_plane_receipt_path(work_dir, worker_role, purpose=purpose)
    previous_receipt: dict[str, Any] | None = None
    previous_task_path = output_dir / "task.json"
    previous_source_policy: dict[str, Any] | None = None
    previous_task: dict[str, Any] = {}
    if discard_unverified_receipt is not None and not previous_task_path.is_file():
        raise ConfigurationError("E_NATIVE_RECEIPT_DISCARD_INVALID: no current relay to repair")
    if previous_task_path.is_file():
        try:
            previous_task = json.loads(previous_task_path.read_text(encoding="utf-8"))
            if previous_task.get("purpose", "") != purpose:
                raise ConfigurationError("E_DATA_PLANE_TASK_PURPOSE: output belongs to another purpose")
            if isinstance(previous_task.get("content_source_policy"), dict):
                previous_source_policy = previous_task["content_source_policy"]
            previous_attempt = previous_task.get("attempt", 0)
            valid_previous_attempt = (
                isinstance(previous_attempt, int)
                and not isinstance(previous_attempt, bool)
                and previous_attempt >= 1
            )
            if valid_previous_attempt:
                previous_revision = previous_task.get("task_revision", previous_attempt)
                if type(previous_revision) is not int or previous_revision < 1:
                    raise ConfigurationError("E_DATA_PLANE_TASK_INVALID: prior task revision is invalid")
                task_revision = previous_revision + 1
            if previous_receipt_path.is_file():
                previous_receipt_bytes = previous_receipt_path.read_bytes()
                previous_receipt = json.loads(previous_receipt_bytes.decode("utf-8"))
            if isinstance(previous_receipt, dict) and isinstance(
                previous_receipt.get("issue_codes"), list
            ):
                previous_issue_codes = [
                    code for code in previous_receipt["issue_codes"]
                    if isinstance(code, str)
                ]
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
            receipt_task_id = previous_receipt.get("task_id") if isinstance(previous_receipt, dict) else None
            receipt_in_scope = (
                isinstance(previous_receipt, dict)
                and previous_receipt.get("schema") in {LEGACY_DATA_PLANE_RECEIPT_SCHEMA, DATA_PLANE_RECEIPT_SCHEMA}
                and previous_receipt.get("worker_role") == worker_role
                and previous_receipt.get("stage") == profile["stage"]
                and previous_receipt.get("control_generation") == control_generation
            )
            receipt_matches_previous = receipt_in_scope and receipt_task_id == previous_task.get("task_id")
            unverified_receipt = (
                worker_role == "inventory_reconciler"
                and receipt_matches_previous
                and previous_task.get("control_generation") == control_generation
                and previous_receipt["schema"] == DATA_PLANE_RECEIPT_SCHEMA
                and previous_receipt.get("status") == "error"
                and isinstance(previous_receipt.get("execution"), dict)
                and previous_receipt["execution"].get("complete") is False
                and isinstance(previous_receipt.get("counts"), dict)
                and type(previous_receipt["counts"].get("success")) is int
                and previous_receipt["counts"]["success"] == 0
                and previous_receipt.get("artifact_paths") == []
                and previous_receipt.get("queue_transitions") == []
            )
            if discard_unverified_receipt is not None and (
                not unverified_receipt or discard_unverified_receipt != receipt_task_id
                or previous_receipt_path.resolve() != previous_receipt_path
            ):
                raise ConfigurationError("E_NATIVE_RECEIPT_DISCARD_INVALID: receipt is not the specified incomplete relay receipt")
            receipt_closes_previous = receipt_matches_previous and not unverified_receipt
            previous_receipt_closes = receipt_closes_previous
            if receipt_closes_previous:
                prior_relay = {}
                prior_relay_path = output_dir / "relay.json"
                if prior_relay_path.is_file():
                    require_native_workers_ended(work_dir, prior_relay_path)
                    prior_relay, prior_bound_task = _load_bound_data_plane_relay(work_dir, prior_relay_path)
                    if (purpose == LOCALE_PROBE_PURPOSE and previous_receipt_bytes is not None
                            and previous_issue_codes == ["malformed_response"]
                            and previous_receipt.get("status") == "error"
                            and previous_receipt.get("execution", {}).get("complete") is True):
                        repaired = _recover_locale_envelope(
                            work_dir, prior_relay_path, previous_receipt_path, previous_receipt_bytes,
                            prior_bound_task, sdk_activation or {}, config,
                        )
                        if repaired is not None:
                            return repaired
                if previous_receipt.get("execution", {}).get("complete", True) is not True:
                    return {
                        "status": "pending", "worker_role": worker_role,
                        "next_action": "wait_for_active_data_plane_workers",
                        "dispatch_allowed": False,
                    }
            runtime_blocked = bool(
                receipt_closes_previous and set(previous_receipt.get("issue_codes", [])) & RUNTIME_ISSUES
            )
            prior_isolation = previous_task.get("execution_isolation_fingerprint")
            if "execution_isolation_fingerprint" not in previous_task and runtime_blocked:
                prior_relay, _ = _load_bound_data_plane_relay(work_dir, output_dir / "relay.json")
                prior_proof = prior_relay.get("launch_contract", {}).get("message_isolation")
                prior_isolation = stable_hash(prior_proof) if prior_proof is not None else None
            isolation_repaired = (
                bool(set(previous_issue_codes) & {ISOLATION_ISSUE, "execution_guard_denied"})
                and isolation_fingerprint is not None and prior_isolation != isolation_fingerprint
            )
            runtime_repaired = (
                "execution_runtime_fingerprint" in previous_task
                and previous_task["execution_runtime_fingerprint"] != (runtime or {}).get("fingerprint")
            ) or isolation_repaired
            if receipt_closes_previous and "operational_error" in previous_issue_codes:
                prior_binding = prior_relay.get("launch_contract", {})
                selected_route = previous_task.get("worker_route") or previous_task.get("context_route")
                selected_model = (runtime or {}).get("models", {}).get(selected_route)
                selected_binding_repaired = bool(
                    selected_model and prior_binding.get("model")
                    and (
                        selected_model != prior_binding["model"]
                        or (runtime or {}).get("host") != prior_binding.get("host")
                    )
                )
                retry_repaired_runtime = selected_binding_repaired or isolation_repaired
            if receipt_closes_previous and "operational_error" in previous_issue_codes and not retry_repaired_runtime and not _approved_operational_fallback(previous_task, previous_issue_codes):
                return {
                    "status": "pending", "worker_role": worker_role,
                    "next_action": "repair_native_runtime", "dispatch_allowed": False,
                    "reason": "bound_execution_policy_does_not_authorize_failure",
                    "issue_codes": ["native_route_repair_required"],
                    "repair_actions": ["repair_native_runtime", "prepare_fresh_authorized_task"],
                }
            if route_chain and receipt_closes_previous:
                previous_codes = previous_receipt.get("issue_codes", [])
                previous_index = previous_task.get("data_plane_route_index", 0)
                if retry_repaired_runtime:
                    previous_route = previous_task.get("worker_route")
                    if previous_route not in route_chain:
                        return {
                            "status": "pending", "worker_role": worker_role,
                            "next_action": "repair_native_runtime", "dispatch_allowed": False,
                            "issue_codes": ["native_route_repair_required"],
                            "repair_actions": ["configure_approved_data_plane_route"],
                        }
                    route_index = route_chain.index(previous_route)
                elif "operational_error" in previous_codes:
                    if (
                        not isinstance(previous_index, int)
                        or isinstance(previous_index, bool)
                        or previous_index < 0
                    ):
                        raise ConfigurationError(
                            "E_DATA_PLANE_ROUTE_INVALID: prior route binding is invalid"
                        )
                    route_index = previous_index + 1
                    if route_index >= len(route_chain):
                        return {
                            "status": "pending",
                            "worker_role": worker_role,
                            "reason": "no_configured_route_successor",
                            "next_action": "repair_native_runtime", "dispatch_allowed": False,
                            "issue_codes": ["native_route_repair_required"],
                            "repair_actions": ["repair_native_runtime", "extend_approved_data_plane_routes"],
                        }
                else:
                    route_index = previous_index if (
                        isinstance(previous_index, int)
                        and not isinstance(previous_index, bool)
                        and 0 <= previous_index < len(route_chain)
                    ) else 0
                worker_route = route_chain[route_index]
                worker_model_family = "native"
            if runtime_blocked and "operational_error" not in previous_issue_codes and not runtime_repaired:
                return {
                    "status": "pending", "next_action": "repair_native_runtime",
                    "worker_role": worker_role, "dispatch_allowed": False,
                    "issue_codes": ["native_route_repair_required"],
                    "repair_actions": ["repair_native_runtime", "configure_approved_data_plane_route"],
                }
            if valid_previous_attempt and receipt_closes_previous:
                attempt = previous_attempt if runtime_blocked or "operational_error" in previous_issue_codes else previous_attempt + 1
                if worker_role == "inventory_reconciler" and prior_relay:
                    replacement = (prior_relay_path, prior_relay, "sealed")
            elif valid_previous_attempt:
                if previous_task.get("control_generation") != control_generation:
                    previous_generation = previous_task["control_generation"]
                    if type(previous_generation) is not int or not 0 <= previous_generation < control_generation:
                        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: prior control generation changed")
                    previous_relay_path = output_dir / "relay.json"
                    previous_relay, bound_task = _load_bound_data_plane_relay(
                        work_dir, previous_relay_path, expected_generation=previous_generation,
                    )
                    if bound_task["task_id"] != previous_task["task_id"]:
                        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: prior task binding changed")
                    prior_execution = _safe_execution_summary(previous_receipt.get("execution")) if isinstance(previous_receipt, dict) else None
                    sealed_prior_receipt = (
                        isinstance(previous_receipt, dict)
                        and previous_receipt.get("task_id") == bound_task["task_id"]
                        and previous_receipt.get("worker_role") == worker_role
                        and previous_receipt.get("stage") == profile["stage"]
                        and previous_receipt.get("control_generation") == previous_generation
                        and previous_receipt.get("status") in {"error", "partial", "success"}
                        and (
                            previous_receipt.get("schema") == LEGACY_DATA_PLANE_RECEIPT_SCHEMA
                            or (
                                previous_receipt.get("schema") == DATA_PLANE_RECEIPT_SCHEMA
                                and prior_execution is not None and prior_execution["complete"] is True
                            )
                        )
                    )
                    require_prior_terminal_evidence = not sealed_prior_receipt
                    require_native_workers_ended(
                        work_dir, previous_relay_path, require_terminal_evidence=require_prior_terminal_evidence,
                    )
                    replacement = (previous_relay_path, previous_relay, "rotation")
                    attempt = previous_attempt
                else:
                    previous_relay_path = output_dir / "relay.json"
                    previous_relay, bound_task = _load_bound_data_plane_relay(
                        work_dir, previous_relay_path
                    )
                    if bound_task.get("task_id") == previous_task.get("task_id"):
                        if worker_role == "inventory_reconciler" and bool(
                            bound_task.get("worker_execution", {}).get("deterministic_worker")
                        ) != deterministic_inventory_worker:
                            raise ConfigurationError("E_DATA_PLANE_TASK_ACTIVE: cannot change the bound worker execution")
                        if content_source_grant is not None or content_source_scope:
                            policy = bound_task["content_source_policy"]
                            proposed = content_source_policy(
                                work_dir, task_kind=policy["task_kind"], route=policy["route"],
                                scope_id=content_source_scope, grant_path=content_source_grant,
                            )
                            if proposed != policy:
                                raise ConfigurationError("E_DATA_PLANE_TASK_ACTIVE: end the current bound task before revising content scope")
                        if offline_processing is not None:
                            from .third_party_approval import validated_offline_processing

                            proposed_processing = validated_offline_processing(
                                work_dir, offline_processing, binding_path=previous_task_path,
                            )
                            if proposed_processing != bound_task.get("offline_processing", []):
                                raise ConfigurationError("E_DATA_PLANE_TASK_ACTIVE: end the current bound task before revising offline processing")
                        if inspection_assets is not None:
                            if worker_role != "image_ui_inventory":
                                raise ConfigurationError("E_IMAGE_INSPECTION_SCOPE_INVALID")
                            from .image_localization import bound_inspection_assets

                            proposed_assets = bound_inspection_assets(
                                work_dir, inspection_assets, game_roots=[Path(root) for root in roots],
                            )
                            if proposed_assets != bound_task.get("bound_local_assets", []):
                                raise ConfigurationError("E_DATA_PLANE_TASK_ACTIVE: end the current bound task before revising inspection assets")
                        # An earlier receipt only closes its own attempt, including
                        # a partial result. The obsolete relay needs its own proof
                        # of non-admission before it can be superseded.
                        earlier_execution = (
                            _safe_execution_summary(previous_receipt.get("execution"))
                            if receipt_in_scope and previous_receipt["schema"] == DATA_PLANE_RECEIPT_SCHEMA
                            else None
                        )
                        earlier_sealed_receipt = (
                            isinstance(receipt_task_id, str)
                            and re.fullmatch(r"[a-f0-9]{64}", receipt_task_id)
                            and receipt_task_id != bound_task["task_id"]
                            and earlier_execution is not None and earlier_execution["complete"] is True
                            and previous_receipt.get("status") in {"error", "partial", "success"}
                        )
                        if (
                            worker_role == "inventory_reconciler"
                            and (earlier_sealed_receipt or unverified_receipt
                                 or not os.path.lexists(previous_receipt_path))
                            and _revisable_data_plane_relay(work_dir, previous_relay_path, previous_relay)
                        ):
                            if unverified_receipt and discard_unverified_receipt is None:
                                return {
                                    "status": "pending", "worker_role": worker_role,
                                    "next_action": "discard_unverified_relay_receipt",
                                    "task_id": receipt_task_id, "dispatch_allowed": False,
                                }
                            previous_route = bound_task.get("worker_route")
                            if route_chain and previous_route not in route_chain:
                                raise ConfigurationError("E_DATA_PLANE_ROUTE_INVALID: prior route is no longer configured")
                            if route_chain:
                                route_index = route_chain.index(previous_route)
                                worker_route = previous_route
                            attempt = previous_attempt
                            replacement = (previous_relay_path, previous_relay, "unadmitted")
                        else:
                            if unverified_receipt:
                                if discard_unverified_receipt is not None:
                                    raise ConfigurationError("E_NATIVE_RECEIPT_DISCARD_INVALID: relay is not a stale, unadmitted launch")
                                return {
                                    "status": "pending", "worker_role": worker_role,
                                    "next_action": "wait_for_active_data_plane_workers",
                                    "dispatch_allowed": False,
                                }
                            return {
                                "schema": previous_relay["schema"],
                                "status": "ready",
                                "worker_role": worker_role,
                                "stage": profile["stage"],
                                "relay_path": _relative(work_dir, previous_relay_path),
                                "task_id": previous_task["task_id"],
                                "control_generation": control_generation,
                                **({"candidate_discovery": bound_task["input_image_candidates"]} if bound_task.get("input_image_candidates") else {}),
                            }
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: cannot verify prior data-plane metadata") from None
    if discard_unverified_receipt is not None and replacement is None:
        raise ConfigurationError("E_NATIVE_RECEIPT_DISCARD_INVALID: no eligible relay to supersede")
    sdk_repair = None
    repair_tool = (
        inventory_repair_tool(work_dir, sdk_activation or {})
        if worker_role == "inventory_reconciler" and discard_unverified_receipt is None
        else None
    )
    if repair_tool is not None:
        with exclusive_file_lock(work_dir / ".game-l10n/native/launch.lock",
                                 issue_code="E_NATIVE_LAUNCH_BUSY", blocking=True):
            require_control_plane_ready(work_dir)
            if (_active_data_plane_dir(stage_dir) != output_dir
                    or control_plane_state(work_dir)["generation"] != control_generation
                    or (replacement is None and previous_task_path.exists())):
                raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: reconciliation binding changed")
            if replacement is not None:
                prior_path, prior, kind = replacement
                if kind == "unadmitted":
                    if not _revisable_data_plane_relay(work_dir, prior_path, prior):
                        raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: reconciliation relay was admitted")
                else:
                    require_native_workers_ended(
                        work_dir, prior_path, require_terminal_evidence=require_prior_terminal_evidence,
                    )
            sdk_repair = reconcile_cached_sdk_inventory(work_dir, repair_tool)
        if sdk_repair is not None and sdk_repair["static_complete"]:
            return {"status": "inventory_verified", "worker_role": worker_role,
                    "dispatch_allowed": False, "sdk_reconciliation": sdk_repair}
    if replacement is not None:
        output_dir = stage_dir / "launch-revisions" / os.urandom(16).hex()
        if output_dir.resolve() != output_dir:
            raise ConfigurationError("E_DATA_PLANE_TASK_PATH: launch revision is redirected")
    # Every attempt and control-plane generation owns a distinct response path.
    # Older envelopes remain immutable evidence and can never be resealed as a
    # later attempt merely because their file name was reused.
    revision_suffix = f".revision-{task_revision}" if task_revision > 1 else ""
    envelope = (
        output_dir / f"control-envelope.g{control_generation}.attempt-{attempt}{revision_suffix}.json"
    ).resolve()
    if purpose == LOCALE_PROBE_PURPOSE:
        artifact = envelope.with_name(envelope.name.replace("control-envelope.", "locale-applicability.", 1))
    elif purpose == LOCALE_LOCAL_PACKAGE_PURPOSE:
        artifact = envelope.with_name(envelope.name.replace("control-envelope.", "local-text-patch.", 1)).with_suffix(".zip")
    elif purpose == ENGINE_MEDIA_BINDINGS_PURPOSE:
        artifact = envelope.with_name(envelope.name.replace("control-envelope.", "engine-media-bindings.", 1))
    elif webp_review:
        artifact = envelope.with_name(envelope.name.replace("control-envelope.", "image-review.", 1))
    relay_max_workers = 1
    worker_delegation_allowed = False
    private_payload: dict[str, Any] = {
        "schema": DATA_PLANE_TASK_SCHEMA,
        "worker_role": worker_role,
        "stage": profile["stage"],
        "attempt": attempt,
        "task_revision": task_revision,
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
            "you_are_the_core_bound_native_worker_not_a_relay_or_model_api_controller",
            "perform_bound_semantic_work_with_your_own_current_native_model_inference",
            "no_separate_model_invocation_tool_or_api_is_required_or_allowed",
            "absence_of_an_additional_model_tool_is_not_model_unavailability",
            "never_delegate_or_send_agent_irc_parent_or_sibling_messages",
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
    if purpose:
        private_payload["purpose"] = purpose
    if purpose == ENGINE_MEDIA_BINDINGS_PURPOSE:
        private_payload["engine_media_binding_contract"] = {
            "schema": "parr0t.engine-media-bindings.v1",
            "result_fields": ["schema", "task_id", "references", "unresolved_bindings"],
            "reference_fields": ["root_index", "relative_path", "kind", "evidence_sha256"],
            "kinds": ["binding_metadata", "ui_image", "font"],
            "evidence_sha256": "sha256 of a supplied bound seed proving the exact reference",
            "unresolved_bindings": "nonnegative integer, without source strings or snippets",
            "container_binding_fields": ["root_index", "relative_path", "archive_sha256", "entry", "object_id", "kind"],
            "container_binding_kinds": ["font", "ui_image"],
        }
    if purpose == LOCALE_LOCAL_PACKAGE_PURPOSE:
        from .tooling import _require_pack_input_binding

        require_extractor_preparation(work_dir, config, sdk_activation or {})
        if not config.get("extractor_requirements", {}).get("source_metadata", {}).get("locale_catalog_adapter"):
            raise ConfigurationError("E_LOCALE_PACKAGE_ADAPTER_REQUIRED")
        completion = work_dir / ".game-l10n" / "completion-attestation.json"
        result_path = Path(json.loads(completion.read_bytes())["result_artifact"]["path"])
        runner = work_dir / "pack_local_locale.py"
        if runner.resolve(strict=True) != runner:
            raise ConfigurationError("E_LOCALE_PACKAGE_RUNNER_PATH")
        transport, prefix = _worker_python_transport(runtime)
        private_payload["locale_package"] = {
            **transport,
            "argv": [*prefix[:-1], str(runner), str(output_dir / "task.json"), str(Path(__file__).resolve().parents[1])],
            "runner_sha256": _digest(runner),
            "completion_sha256": _digest(completion),
            "result_path": _relative(work_dir, result_path),
            "result_sha256": _require_pack_input_binding(work_dir, result_path),
        }
    if purpose == LOCALE_PROBE_PURPOSE:
        prior_probe = current_locale_probe(work_dir, config, sdk_activation or {})
        if prior_probe is not None and prior_probe["inventory_status"] == "inconclusive":
            private_payload["previous_locale_checks"] = prior_probe["checks"]
        elif (previous_receipt_closes and previous_task.get("sdk_activation") == sdk_activation
                and previous_task.get("language_binding") == private_payload["language_binding"]
                and previous_task.get("source_roots") == roots
                and isinstance(previous_task.get("previous_locale_checks"), dict)):
            private_payload["previous_locale_checks"] = previous_task["previous_locale_checks"]
        private_payload["locale_probe_contract"] = {
            "schema": LOCALE_PROBE_SCHEMA,
            "checks": list(LOCALE_INVENTORY_CHECKS),
            "optional_checks": ["target_route"],
            "outcomes": ["applicable", "unavailable", "inconclusive"],
            "result_fields": ["schema", "task_id", "outcome", "checks", "evidence_sha256"],
            "check_values": ["verified", "disproved", "unresolved"],
            "envelope_disposition": "success_for_verified_or_disproved_route_partial_for_inconclusive_no_issue_codes",
            "envelope_counts": {
                "success": {"total": 1, "success": 1, "error": 0, "pending": 0},
                "partial": {"total": 1, "success": 0, "error": 0, "pending": 1},
            },
        }
    if purpose == EXTRACTOR_PREPARATION_PURPOSE:
        if ((config.get("extractor_requirements") or {}).get("source_metadata") or {}).get("image_candidate_discovery"):
            private_payload["requirements"].extend([
                "obey_the_exact_image_candidate_discovery_output_contract_in_extractor_requirements",
                "inspect_opaque_sources_only_with_bounded_deterministic_sdk_code_never_whole_root_semantic_reads",
                "decode_only_image_font_candidates_into_inputs_image_assets_preserve_original_source_bytes",
                "do_not_acquire_dependencies_execute_the_game_or_infer_new_processing_authority",
                "functional_checks_must_publish_the_exact_candidate_manifest_and_resolve_every_bound_source",
            ])
        if "locale_catalog_adapter" in (config.get("extractor_requirements") or {}).get("source_metadata", {}):
            private_payload["requirements"].extend([
                "use_the_verified_active_source_catalog_and_reversible_target_slot_only_never_search_for_existing_target_language",
                "build_workspace_owned_locale_inventory_primary_py_and_locale_inventory_independent_py_with_independent_value_discovery",
                "bind_stable_physical_value_IDs_and_preserve_order_duplicates_and_all_keys_outside_value_slots",
                "build_workspace_owned_locale_apply_py_for_value_only_reversible_application_to_workspace_copies",
                "prove_discovery_order_duplicate_retention_key_preservation_value_only_roundtrip_and_original_restoration_with_fixed_synthetic_fixtures",
                "run_the_bound_extractor_preparation_check_and_write_only_the_control_envelope",
            ])
        private_payload["extractor_requirements"] = config.get("extractor_requirements")
        private_payload["preparation_contract"] = {
            "profile_path": "tools/extractor-preparation.json",
            "profile_schema": "parr0t.extractor-preparation-profile.v1",
            "check_command": "game-l10n check-extractor-preparation --workspace <workspace> --relay <relay-path>",
            "relay_path": _relative(work_dir, output_dir / "relay.json"),
        }
    source_policy: dict[str, Any] | None = None

    def _bound_source_policy(task_kind: str, route: str) -> dict[str, Any]:
        previous = (
            validate_content_source_policy(previous_source_policy)
            if previous_source_policy is not None
            else None
        )
        if previous is not None and (
            previous["task_kind"] != task_kind or previous["route"] != route
        ):
            raise ConfigurationError(
                "E_CONTENT_SOURCE_POLICY_IMMUTABLE: retry task scope changed"
            )
        if (
            previous is not None
            and content_source_grant is None
            and not content_source_scope
        ):
            return previous
        proposed = content_source_policy(
            work_dir,
            task_kind=task_kind,
            route=route,
            scope_id=content_source_scope,
            grant_path=content_source_grant,
        )
        return proposed

    if route_chain:
        private_payload.update(
            data_plane_route_chain=route_chain,
            data_plane_route_index=route_index,
            worker_route=worker_route,
        )
    if worker_role in {"primary_inventory", "independent_inventory"}:
        private_payload["inventory_scope_contract"] = TEXT_ELIGIBILITY_CONTRACT
        private_payload["screen_review_skip_allowed"] = screen_review_skip_allowed(work_dir)
        if locale_probe is not None:
            inventory_status = locale_probe["inventory_status"]
            private_payload["locale_probe_binding"] = {
                "locale_probe": inventory_status,
                "route": "locale_data" if inventory_status == "applicable" else "display_extraction",
                "evidence_fingerprint": locale_probe["evidence_fingerprint"],
            }
            adapter = (config.get("extractor_requirements") or {}).get("source_metadata", {}).get("locale_catalog_adapter")
            if adapter is not None:
                # Preparation binds the adapter and its original evidence. Each
                # inventory independently binds the current sealed probe.
                if inventory_status != "applicable":
                    raise ConfigurationError("E_LOCALE_ADAPTER_APPLICABILITY_CHANGED")
                role = "primary" if worker_role == "primary_inventory" else "independent"
                runner = work_dir / "tools" / f"locale_inventory_{role}.py"
                if not runner.is_file() or runner.resolve() != runner:
                    raise ConfigurationError("E_LOCALE_ADAPTER_TOOL_MISSING")
                private_payload["locale_inventory_runner"] = {"path": _relative(work_dir, runner), "sha256": _digest(runner)}
                private_payload["requirements"].append(
                    "use_the_exact_bound_locale_inventory_runner_from_preparation_and_do_not_repeat_the_completed_locale_probe"
                )
    if worker_role == "inventory_reconciler":
        attestation_path = work_dir / ".game-l10n" / "inventory-attestation.json"
        if not attestation_path.is_file():
            raise ConfigurationError(
                "E_DATA_PLANE_TASK_INVENTORY: reconciliation requires an inventory attestation"
            )
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
        reconciliation = attestation.get("reconciliation", {})
        issue_codes = list(attestation.get("issue_codes", []))
        try:
            private_payload["inventory_constraint_baseline"] = observe_inventory_constraints(
                work_dir, attestation, config, sdk_activation or {}, runtime,
                reuse_projection_baseline=True,
            )
        except ActiveControlStorageError as exc:
            # A rejected active row is a repair input, not a verified constraint baseline.
            issue_codes.append(exc.code)
        except (ConfigurationError, OSError, ValueError, TypeError, AttributeError, KeyError):
            # Legacy or stale attestations remain executable, not blocked.
            pass
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
            "issue_codes": issue_codes,
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
        if previous_receipt_closes and retry_repaired_runtime:
            previous_route = previous_task.get("context_route")
            if previous_route not in context_routes:
                return {
                    "status": "pending", "worker_role": worker_role,
                    "next_action": "repair_native_runtime", "dispatch_allowed": False,
                    "issue_codes": ["native_route_repair_required"],
                    "repair_actions": ["configure_approved_context_routes"],
                }
            route_index = context_routes.index(previous_route)
        elif previous_receipt_closes and "operational_error" in previous_issue_codes:
            route_index = int(previous_task.get("context_route_index", 0)) + 1
            if route_index >= len(context_routes):
                return {
                    "status": "pending", "worker_role": worker_role,
                    "next_action": "repair_native_runtime", "dispatch_allowed": False,
                    "issue_codes": ["native_route_repair_required"],
                    "repair_actions": ["repair_native_runtime", "extend_approved_context_routes"],
                }
        route_cycle = (attempt - 1) // len(context_routes)
        worker_route = context_routes[route_index]
        runtime_model = (
            str(runtime["models"].get(worker_route, worker_route))
            if runtime is not None
            else worker_route
        )
        source_policy = _bound_source_policy(
            "localization_context", "data-plane:context_analyzer"
        )
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
        if previous_task.get("context_base_chunk_chars", base_context_chunk_chars) != base_context_chunk_chars:
            context_no_progress_attempts = 0
        failed_route_cycles = context_no_progress_attempts // len(context_routes)
        minimum_chunk_chars = min(MIN_CONTEXT_CHUNK_CHARS, base_context_chunk_chars)
        context_chunk_chars = max(
            minimum_chunk_chars,
            base_context_chunk_chars >> min(failed_route_cycles, base_context_chunk_chars.bit_length()),
        )
        requested_context_workers = int(
            config.get("routing", {}).get(
                "context_max_workers", config.get("routing", {}).get("max_workers", 1),
            )
        )
        if requested_context_workers < 1:
            raise ConfigurationError("E_DATA_PLANE_TASK_ROUTE: context worker count must be positive")
        from .native_runtime import native_worker_ceiling
        configured_context_workers = native_worker_ceiling(work_dir, worker_route, requested_context_workers)
        context_segments = read_segments(primary_path)
        context_preflight = build_context_preflight(context_segments)
        context_fingerprints = context_contract_fingerprints()
        context_preflight_path = (
            work_dir / ".game-l10n" / "context-progress" / "preflight.json"
        )
        _atomic_json(context_preflight_path, context_preflight)
        source_policies = {
            policy["policy_digest"]: validate_content_source_policy(policy)
            for policy in previous_task.get("context_source_policies", [])
        }
        if previous_source_policy is not None:
            source_policies[previous_source_policy["policy_digest"]] = validate_content_source_policy(previous_source_policy)
        source_policies[source_policy["policy_digest"]] = source_policy
        private_payload["context_source_policies"] = list(source_policies.values())
        context_manifest_path, context_manifest = _write_context_manifest(
            work_dir,
            segments=context_segments,
            preflight=context_preflight,
            source_inventory_fingerprint=verified["primary_fingerprint"],
            context_mode=context_mode,
            chunk_chars=context_chunk_chars,
            contract_fingerprints=context_fingerprints,
            localization=_context_localization(work_dir, config),
            content_source_policy=source_policy,
            semantic_route=worker_route,
            runtime_model=runtime_model,
            source_policies=list(source_policies.values()),
        )
        _, pending_context_chunks = _context_progress(work_dir, context_manifest)
        failure_bindings = list(previous_task.get("context_failure_bindings", []))
        if made_progress:
            failure_bindings = []
        elif previous_receipt_closes and previous_task.get("context_attempt_binding"):
            failure_bindings.append(previous_task["context_attempt_binding"])
        attempt_binding = stable_hash({
            "manifest": context_manifest["manifest_id"],
            "route": worker_route, "model": runtime_model,
            "chunk_chars": context_chunk_chars, "runtime": runtime,
            "base_chunk_chars": base_context_chunk_chars, "max_workers": configured_context_workers,
            "pending": [chunk["binding_id"] for chunk in pending_context_chunks],
        })
        if pending_context_chunks and attempt_binding in failure_bindings:
            for alternative_index, alternative in enumerate(context_routes):
                alternative_model = str(runtime["models"].get(alternative, alternative)) if runtime else alternative
                alternative_workers = native_worker_ceiling(work_dir, alternative, requested_context_workers)
                alternative_path, alternative_manifest = _write_context_manifest(
                    work_dir, segments=context_segments, preflight=context_preflight,
                    source_inventory_fingerprint=verified["primary_fingerprint"],
                    context_mode=context_mode, chunk_chars=context_chunk_chars,
                    contract_fingerprints=context_fingerprints,
                    localization=_context_localization(work_dir, config),
                    content_source_policy=source_policy, semantic_route=alternative,
                    runtime_model=alternative_model,
                    source_policies=list(source_policies.values()),
                )
                _, alternative_pending = _context_progress(work_dir, alternative_manifest)
                alternative_binding = stable_hash({
                    "manifest": alternative_manifest["manifest_id"],
                    "route": alternative, "model": alternative_model,
                    "chunk_chars": context_chunk_chars, "runtime": runtime,
                    "base_chunk_chars": base_context_chunk_chars, "max_workers": alternative_workers,
                    "pending": [chunk["binding_id"] for chunk in alternative_pending],
                })
                if alternative_binding not in failure_bindings:
                    route_index, worker_route, runtime_model = alternative_index, alternative, alternative_model
                    context_manifest_path, context_manifest = alternative_path, alternative_manifest
                    pending_context_chunks, attempt_binding = alternative_pending, alternative_binding
                    configured_context_workers = alternative_workers
                    worker_model_family = worker_route.removeprefix("native-") if worker_route.startswith("native-") else "external"
                    break
            else:
                return {
                    "status": "pending", "worker_role": worker_role,
                    "next_action": "repair_context_inputs", "dispatch_allowed": False,
                    "job_terminal": False, "issue_codes": ["context_repair_required"],
                    "repair_actions": ["repair_context_inputs", "extend_approved_context_routes"],
                }
        private_payload["context_attempt_binding"] = attempt_binding
        private_payload["context_failure_bindings"] = list(dict.fromkeys(failure_bindings))
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
                    "semantic_executor": "current_native_worker_inference",
                    "additional_model_invocation_required": False,
                    "semantic_worker_delegation_allowed": False,
                    "outbound_agent_messaging_allowed": False,
                    "claim_schema": CONTEXT_CHUNK_CLAIM_SCHEMA,
                    "result_schema": CONTEXT_CHUNK_RESULT_SCHEMA,
                    "fragment_schema": CONTEXT_FRAGMENT_SCHEMA,
                    "execution_contract": "worker_execution",
                    "content_provenance_required": True,
                    "direct_context_pack_write_forbidden": True,
                    "success_requires_every_manifest_chunk": True,
                },
                "context_recovery": {
                    "resume_private_progress": True,
                    "retry_scope": "smallest_unfinished_context_chunk",
                    "route_selection": "core_round_robin",
                    "chunk_reduction": "halve_only_after_a_complete_no_progress_route_cycle",
                    "progress_resets_chunk_reduction": True,
                    "minimum_chunk_chars": minimum_chunk_chars,
                    "sealed_failure_is_job_terminal": False,
                    "unchanged_progress_limit_applies": True,
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
                        "localization_inputs_digest",
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
                        "dialogue_contexts",
                        "screen_flows",
                        "feature_guides",
                        "knowledge_nodes",
                    ],
                    "evidence_line_ids_required_for": [
                        "dialogue_contexts",
                        "screen_flows",
                        "feature_guides",
                        "knowledge_nodes",
                    ],
                },
            }
        )
    if webp_review:
        private_payload["webp_visual_review"] = webp_review
        private_payload["webp_visual_review_png_root"] = _relative(work_dir, artifact.with_name(artifact.stem + "-pairs"))
        private_payload["bound_local_assets"] = [
            {"path": path, "sha256": digest}
            for row in webp_review["assets"]
            for path, digest in ((row["original"], row["original_sha256"]),
                                 (str(work_dir / row["edited"]), row["edited_sha256"]))
        ]
    elif worker_role == "image_localizer":
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
        from .image_localization import bound_local_assets

        private_payload["bound_local_assets"] = bound_local_assets(
            work_dir, inventory_path, game_roots=[Path(root) for root in roots],
        )
    if worker_role in {"primary_inventory", "independent_inventory", "inventory_reconciler"}:
        private_payload["requirements"].extend(
            [
                "raw_literal_presence_alone_is_not_display_or_completeness_evidence",
                "derive_display_targets_from_format_roles_bindings_and_runtime_reference_evidence",
                "register_only_positive_supported_display_bindings_never_raw_or_possible_only_targets",
                "follow_nested_values_only_with_a_source_bound_path_to_an_exact_display_slot",
                "keep_unclassified_raw_occurrences_out_of_context_translation_and_review",
                "do_not_infer_display_use_from_language_container_membership_or_evidence_hash_alone",
                "include_evidenced_player_visible_system_messages",
                "record_unresolved_actual_display_dependencies_as_pending_not_active_targets",
                "account_raw_occurrences_by_provenance_and_category_separately_from_display_target_counts",
                "keep_private_occurrence_evidence_for_display_bound_uncertain_and_proven_non_display_classifications",
                "store_reusable_method_file_and_type_proofs_once_and_reference_them_by_fingerprint",
                "preserve_every_occurrence_location_and_scope_binding_without_repeating_large_shared_proof_payloads",
                "serialize_large_deterministic_parse_caches_natively_from_files_not_as_model_or_tool_rpc_text_payloads",
                "exclude_only_evidenced_non_display_occurrences_never_blanket_engine_or_module_categories",
                "record_exclusion_reason_codes_counts_and_evidence_fingerprints_in_coverage_exclusions",
                "preserve_control_storage_bytes_and_emit_separate_display_only_projections",
                "attach_only_target_bound_semantic_context_not_whole_command_trees_editor_records_or_parse_caches",
                "use_existing_coverage_static_scope_contract_for_reachability_exclusions",
                "preserve_physical_occurrences_and_source_order_never_deduplicate_by_text_value",
            ]
        )
    if worker_role in {"primary_inventory", "independent_inventory"}:
        private_payload["requirements"].extend(
            [
                "scan_source_once_using_selected_sdk_role_method",
                "probe_the_active_sdk_locale_loader_and_reversible_target_apply_path_before_enumerating_text",
                "when_locale_is_applicable_inventory_only_its_display_values_without_screen_review_or_source_extraction",
                "only_extract_display_text_after_the_sdk_proves_no_applicable_locale_path",
                "do_not_treat_an_inconclusive_locale_probe_as_permission_for_extraction",
                "emit_coverage_text_source_and_per_target_role_for_the_exact_bound_route",
                "skip_only_additional_screen_review_if_the_task_binds_user_approval_never_skip_eligibility",
                "cache_role_local_structural_parse_provenance_and_display_dependency_evidence",
                "construct_inventory_from_cached_single_pass_evidence_without_repeated_source_reads",
                "do_not_read_or_derive_from_counterpart_inventory",
                "independently_discover_source_locations_and_eligibility_never_relabel_a_shared_segment_list",
                "emit_inventory_comparison_key_for_method_specific_ids",
                "report_selected_vs_observed_engine_identity_conflicts_as_sdk_identity_mismatch",
            ]
        )
        if (
            previous_receipt_closes
            and "inventory_display_scope_required" in previous_issue_codes
            and artifact.is_file()
        ):
            private_payload["requirements"].remove(
                "scan_source_once_using_selected_sdk_role_method"
            )
            private_payload["requirements"].extend(
                [
                    "reuse_cached_inventory_artifact_without_rescanning_source",
                    "repair_cached_format_roles_and_actual_display_dependency_evidence_without_promoting_raw_only_rows",
                    "preserve_raw_occurrence_history_and_stable_ids_use_bound_scope_correction_for_retired_targets",
                ]
            )
            private_payload["inventory_scope_repair"] = {
                "failure_code": "inventory_display_scope_required",
                "cached_artifact": _relative(work_dir, artifact),
                "cached_artifact_sha256": _digest(artifact),
            }
    elif worker_role == "inventory_reconciler":
        private_payload["requirements"].extend(
            [
                "reuse_existing_inventory_scans_and_cached_evidence",
                "do_not_repeat_full_source_scans",
                "resolve_only_reported_gap_categories",
                "resolve_any_text_source_route_disagreement_from_cached_loader_evidence_never_merge_routes",
                "for_large_gaps_group_cached_occurrences_by_evidenced_sdk_display_consumer_and_storage_role",
                "apply_each_supported_structural_rule_to_all_matching_pending_occurrences_in_one_local_sdk_pass",
                "preserve_independent_method_proof_for_each_occurrence_and_leave_ambiguous_cases_pending",
                "use_inventory_comparison_key_for_proven_method_specific_aliases",
                "treat_global_id_disagreement_as_identity_namespace_reconciliation_not_content_loss",
                "never_rename_remove_or_reorder_checkpoint_bound_primary_ids",
                "preserve_primary_ids_as_downstream_canonical_identity",
                "repair_method_specific_aliases_on_the_independent_inventory_with_deterministic_evidence",
                "do_not_match_duplicate_source_text_without_occurrence_or_location_evidence",
                "stage_all_evidenced_repairs_before_running_core_verify_inventory_once_to_bind_the_current_pair",
                "use_worker_execution_core_runtime_for_the_exact_core_launcher_never_guess_paths",
                "execute_verify_inventory_using_bound_worker_execution_tool_and_argv",
                "if_execution_aborts_without_pair_verification_leave_the_output_envelope_unwritten",
                "only_use_worker_execution_allowed_issue_codes_never_embed_diagnostics_in_the_envelope",
                "if_inputs_do_not_change_run_the_required_fresh_pair_verification_once_not_repeatedly",
                "incomplete_with_measured_progress_requires_another_bounded_step",
                "do_not_redo_resolved_sdk_projections_only_handle_remaining_exceptions",
                "constraint_unsatisfiable_alone_requests_core_validation_not_terminal_authority",
                "never_relabel_provider_transport_or_malformed_output_as_inventory_constraint",
                "require_fresh_complete_pair_verification_and_bound_history_for_projection_constraints",
                "repair_scope_eligibility_without_removing_or_reordering_canonical_primary_history",
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
                "only_the_fresh_finalizer_writes_the_merged_context_pack_using_exact_context_pack_fields",
                "preserve_evidence_line_ids_for_every_required_domain",
                "on_partial_or_operational_failure_record_resumable_private_progress",
                "continue_unaffected_chunks_when_one_chunk_requires_fallback",
                "relay_assigns_unique_context_worker_slots_from_one_to_max_workers",
                "execute_complete_argv_from_worker_execution_using_its_bound_tool_and_argv_convention",
                "substitute_only_whole_declared_worker_execution_placeholders",
                "do_not_read_relay_command_argv_launch_ledgers_or_lifecycle_state_to_run_worker_commands",
                "slot_workers_use_only_claim_and_commit_commands_for_their_assigned_slot",
                "only_the_fresh_finalizer_uses_the_finalize_command_after_the_worker_wave",
                "on_command_or_semantic_failure_do_not_send_agent_parent_sibling_or_irc_messages",
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
    elif purpose == ENGINE_MEDIA_BINDINGS_PURPOSE:
        private_payload["requirements"].extend([
            "derive_exact_UI_image_atlas_and_font_references_from_bound_local_seed_metadata_only",
            "report_exact_active_runtime_metadata_references_needed_next_as_binding_metadata",
            "for_exact_bound_archive_objects_report_optional_container_bindings_without_decoding_pixels_or_fonts",
            "use_known_engine_layout_only_when_supported_by_the_active_SDK_and_seed_metadata",
            "no_listing_tree_scan_OCR_opening_or_testing_unbound_files_or_target_locale_assets",
            "do_not_translate_or_assert_visual_scope_complete_from_this_binding_probe",
            "write_only_engine_media_binding_contract_at_output_artifact_and_standard_control_envelope",
        ])
    elif worker_role == "image_ui_inventory":
        private_payload["requirements"].extend(
            [
                "user_request_is_recorded_but_does_not_prove_any_image_is_a_target",
                "start_from_engine_display_bindings_or_user_identified_player_visible_assets",
                "do_not_recursively_ocr_all_images",
                "use_ocr_only_on_bounded_ui_candidates_and_confirm_each_text_region_visually",
                "include_requested_player_visible_ui_cg_scenery_or_other_bound_text_assets",
                "account_for_unexamined_and_uncertain_requested_assets_without_claiming_no_targets",
                "preserve_source_asset_paths_hashes_dimensions_regions_and_atlas_evidence",
                "do_not_translate_edit_apply_pack_install_launch_or_delegate",
            ]
        )
    elif webp_review:
        private_payload["webp_visual_review_contract"] = {
            "schema": "parr0t.webp-visual-review.v1",
            "fields": ["schema", "task_id", "draft_task_id", "records"],
            "record_fields": ["ordinal", "status"],
            "statuses": ["approved", "revise", "unresolved"],
        }
        private_payload["requirements"].extend([
            "run_exact_bound_offline_processing_argv_using_parr0t_process_with_python_-I_-B_once_before_review",
            "use_read_to_view_both_full_resolution_private_pngs_for_each_asset_in_webp_visual_review_png_root",
            "compare_source_meaning_korean_accuracy_style_and_fit_for_every_exact_region",
            "mark_approved_only_when_every_region_is_visually_verified_no_clipping_ghosting_or_unreadable_text",
            "mark_revise_for_an_observed_defect_and_unresolved_if_either_png_or_a_region_cannot_be_verified",
            "write_one_record_per_asset_in_bound_order_even_if_unresolved_and_include_report_in_artifact_paths",
            "review_only_never_retranslate_edit_the_draft_approve_an_install_or_claim_full_image_scope",
        ])
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
    elif purpose == LOCALE_PROBE_PURPOSE:
        private_payload["requirements"].extend([
            "run_only_bounded_read_only_locale_applicability_probe_using_activated_sdk_and_locked_languages",
            "bind_active_loader_source_catalog_schema_and_reversible_value_apply_path",
            "prove_value_only_apply_and_reverse_on_synthetic_or_bounded_local_fixture_without_install",
            "verify_locale_applicability_not_existing_target_locale_presence_or_translation_quality",
            "compute_outcome_and_envelope_status_from_the_required_checks_only",
            "omit_target_route_unless_recording_optional_existing_activation_evidence",
            "reuse_prior_verified_inventory_checks_and_test_only_unresolved_inventory_checks_with_new_evidence",
            "do_not_scan_source_literals_or_inspect_screen_images_or_existing_target_language_content",
            "do_not_change_sdk_selection_config_inventories_game_files_or_start_other_stages",
            "write_only_bound_categorical_artifact_and_envelope_no_text_assets_logs_or_details_in_artifact",
            "do_not_confuse_a_missing_test_with_disproof_verify_the_active_SDK_format_contract_and_exact_loader_or_apply_behavior",
            "a_format_or_apply_path_excluded_by_exercised_SDK_behavior_can_disprove_that_exact_route_not_other_unexamined_routes",
            "if_any_required_check_is_disproved_use_unavailable_else_if_unresolved_use_inconclusive_else_use_applicable",
        ])
    elif purpose == LOCALE_LOCAL_PACKAGE_PURPOSE:
        private_payload["requirements"].extend([
            "run_only_the_exact_locale_package_argv_in_the_bound_workspace_using_parr0t_process",
            "this_is_a_deterministic_local_text_package_not_full_image_font_or_runtime_completion",
            "do_not_install_launch_or_forward_source_or_translation_content",
        ])
    elif purpose:
        private_payload["requirements"].extend(
            [
                "implement_missing_extractors_only_inside_external_workspace",
                "inspect_only_bounded_source_format_metadata_never_full_inventory",
                "record_source_metadata_driven_extractor_requirements_without_narrowing_existing_requirements",
                "cover_every_required_source_format_and_extractor_capability",
                "stage_fixed_synthetic_or_bounded_format_fixtures_and_actual_extraction_round_trip_expectations",
                "never_use_self_echo_or_no_throw_checks_as_extractor_proof",
                "reactivate_same_selection_if_bound_workspace_tools_change",
                "run_core_check_extractor_preparation_with_this_exact_relay",
                "on_failure_leave_preparation_pending_never_start_either_inventory",
                "do_not_translate_repair_inventory_pack_install_launch_or_publish",
                "readiness_never_establishes_inventory_static_or_runtime_completion",
            ]
        )
    else:
        private_payload["requirements"].extend(
            [
                "inspect_only_metadata_needed_for_locale_engine_and_version",
                "record_sdk_evaluation_and_selection_inside_workspace",
            ]
        )

    if worker_role == "context_analyzer":
        private_payload["worker_execution"] = _context_worker_execution(
            work_dir, output_dir / "task.json", runtime
        )
    elif worker_role == "inventory_reconciler":
        private_payload["worker_execution"] = _inventory_worker_execution(work_dir, runtime, runner)

    semantic_kind = {
        "context_analyzer": "localization_context",
        "image_localizer": "image_localization",
    }.get(worker_role, worker_role)
    policy_route = (
        f"data-plane:{worker_role}"
        if worker_role in {"context_analyzer", "image_localizer"}
        else "local-inspection"
    )
    if source_policy is None:
        source_policy = _bound_source_policy(semantic_kind, policy_route)
    private_payload["content_source_policy"] = source_policy
    if offline_processing:
        if worker_role not in {"image_ui_inventory", "image_localizer"}:
            raise ConfigurationError("E_DATA_PLANE_TASK_ROLE: offline image processing requires an image task")
        from .third_party_approval import validated_offline_processing

        private_payload["offline_processing"] = validated_offline_processing(work_dir, offline_processing)
        preview_script = str(Path(__file__).with_name("windows_image_preview.py").resolve())
        originals = bool(image_preview_index is not None)
        if originals and (not scoped_image or image_preview_ordinals is None
                          or len(image_preview_ordinals) > 32 or len(offline_processing) != 1
                          or private_payload["offline_processing"][0]["argv"][0] != preview_script
                          or private_payload["offline_processing"][0]["argv"].count("--original-scale") != 1):
            raise ConfigurationError("E_WINDOWS_IMAGE_PREVIEW_BINDING_INVALID")
        if worker_role == "image_ui_inventory" and any(
            binding["argv"][0] == preview_script for binding in private_payload["offline_processing"]
        ):
            private_payload["requirements"].extend([
                "before_examining_candidates_run_bound_windows_image_preview_argv_once_using_parr0t_process",
                "run_exact_python_executable_then_-I_-B_then_the_complete_offline_processing_argv",
                "after_preview_command_use_read_to_view_the_returned_private_png_sheets_and_the_ordinal_index",
                "save_observed_classifications_and_remain_partial_if_a_preview_or_its_original_text_region_was_not_visible",
            ])
            if originals:
                private_payload["requirements"][-2] = (
                    "read_actual_original_scale_pngs_from_the_returned_private_ordinal_index_only_not_just_contact_sheets"
                )
                private_payload["requirements"].extend([
                    "visually_inspect_every_available_original_scale_png_at_its_exact_bound_ordinal",
                    "keep_failed_decode_animated_and_visually_unavailable_originals_unexamined",
                    "no_text_is_a_visual_decision_and_does_not_require_visibility_proof_when_exact_image_contains_no_text",
                ])
        encoder = str(Path(__file__).with_name("bound_webp_encode.py").resolve())
        encoder_args = [binding["argv"] for binding in private_payload["offline_processing"] if binding["argv"][0] == encoder]
        if worker_role == "image_localizer" and encoder_args:
            if any("--retry-task-id" in argv for argv in encoder_args):
                private_payload["requirements"].extend([
                    "run_the_exact_webp_encoder_argv_with_--retry-task-id_first_using_parr0t_process_with_python_-I_-B",
                    "reuse_only_prior_staged_pixels_verified_by_the_bound_processor_never_claim_new_translation",
                    "do_not_overwrite_existing_drafts_or_repeat_semantic_work_if_the_retry_encode_is_blocked",
                ])
            else:
                private_payload["requirements"].extend([
                    "run_the_bound_webp_encoder_argv_with_--prepare-only_to_create_original_scale_pngs",
                    "visually_read_asset-NNNN.source.png_and_render_only_the_bound_text_rectangles",
                    "stage_edited_8_bit_noninterlaced_rgba_png_in_output_image_root_as_asset-NNNN.png",
                    "use_1_based_asset_order_in_input_image_inventory_for_NNNN_and_preserve_original_dimensions",
                    "run_the_bound_webp_encoder_argv_without_--prepare-only_using_parr0t_process_with_python_-I_-B",
                ])
            private_payload["requirements"].extend([
                "use_asset-NNNN.webp_as_output_only_if_exact_decoded_alpha_and_outside_region_pixels_are_verified",
                "retain_required_engine_structure_evidence_and_do_not_claim_independent_visual_review",
            ])
    if source_policy["semantic_content_generation"]:
        private_payload["runtime_model"] = (
            str(runtime["models"].get(worker_route, worker_route))
            if runtime is not None
            else worker_route
        )
    if inspection_assets is not None:
        if worker_role != "image_ui_inventory":
            raise ConfigurationError("E_IMAGE_INSPECTION_SCOPE_INVALID: inspection assets require an inspection task")
        from .image_localization import bound_inspection_assets

        private_payload["bound_local_assets"] = bound_inspection_assets(
            work_dir, inspection_assets, game_roots=[Path(root) for root in roots],
        )
    elif worker_role == "image_ui_inventory":
        discovery, image_inputs = _image_inventory_candidates(
            work_dir, output_dir, [Path(root) for root in roots], artifact, config, sdk_activation,
        )
        candidate_binding = stable_hash({
            "requested_scope": discovery["requested_scope"], "source_roots": discovery["source_roots"],
            "bound_local_assets": discovery["bound_local_assets"], "unexamined_sources": discovery["unexamined_sources"],
        })
        if discovery["unexamined_sources"] and (
            not discovery["bound_local_assets"]
            or (
                previous_receipt_closes and "image_scope_unexamined" in previous_issue_codes
                and previous_task.get("image_candidate_binding") == candidate_binding
            )
        ):
            return _image_candidate_repair(work_dir, output_dir, config, discovery, image_inputs)
        private_payload["image_source_roots"] = discovery["asset_source_roots"]
        private_payload["image_candidate_binding"] = candidate_binding
        private_payload["bound_local_assets"] = discovery["bound_local_assets"]
        private_payload.update(image_inputs)
        private_payload["requirements"].extend([
            "read_the_bound_image_candidate_manifest_and_canonical_metadata_inputs",
            "when_writing_source_root_index_use_image_source_roots_in_exact_order",
            "candidate_file_presence_is_not_player_visibility_or_completeness_evidence",
            "never_read_unexamined_opaque_linked_or_unlisted_source_files",
            "retain_unexamined_sources_in_scope_coverage_until_sdk_evidence_resolves_them",
            "full_requested_scope_is_not_limited_to_previously_confirmed_ui_images",
        ])
        if image_preview_index is not None:
            from .windows_image_preview import _bind_preview_sheets, _select_preview_ordinals

            preview = _select_preview_ordinals(
                _bind_preview_sheets(work_dir, image_preview_index, image_preview_sheets, discovery["bound_local_assets"]),
                image_preview_ordinals,
            )
            private_payload["image_preview"] = preview
            private_payload["requirements"].extend([
                "read_each_exact_png_listed_in_image_preview_sheets_with_the_host_image_attachment_tool",
                "examine_only_those_sheet_ordinals_and_use_their_corresponding_originals_for_full_resolution",
                "retain_all_other_image_candidates_as_unexamined_in_the_full_scope_accounting",
                "write_partial_inventory_evidence_and_return_only_the_bound_envelope",
            ])
    from .native_runtime import active_execution_environment_policy

    private_payload["execution_environment_policy"] = active_execution_environment_policy(work_dir)
    private_payload["execution_runtime_fingerprint"] = (runtime or {}).get("fingerprint")
    private_payload["execution_isolation_fingerprint"] = isolation_fingerprint
    task_id = stable_hash(private_payload)
    task = {**private_payload, "task_id": task_id}
    task_path = output_dir / "task.json"
    if replacement is None and previous_task and previous_task.get("task_id") != task_id:
        if re.fullmatch(r"[a-f0-9]{64}", str(previous_task.get("task_id", ""))) is None:
            raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: prior task identity changed")
        history_dir = output_dir / "history" / str(previous_task["task_id"])
        history_dir.mkdir(parents=True, exist_ok=True)
        for name in ("task.json", "relay.json", "relay-lifecycle.json", "receipt.json"):
            if name == "receipt.json" and (
                not isinstance(previous_receipt, dict) or previous_receipt.get("task_id") != previous_task["task_id"]
            ):
                continue
            previous_path = previous_receipt_path if name == "receipt.json" else output_dir / name
            archived_path = history_dir / name
            if previous_path.is_file() and not archived_path.exists():
                archived_path.write_bytes(previous_path.read_bytes())
    previous_task_bytes = (
        task_path.read_bytes()
        if private_payload.get("offline_processing") and task_path.is_file()
        else None
    )
    _atomic_json(task_path, task)
    if private_payload.get("offline_processing"):
        from .third_party_approval import consume_offline_processing

        try:
            consume_offline_processing(
                work_dir, offline_processing, binding_path=task_path,
            )
        except Exception:
            if previous_task_bytes is None:
                task_path.unlink(missing_ok=True)
            else:
                _atomic_bytes(task_path, previous_task_bytes)
            raise
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
    if replacement is not None:
        # Different relay address: a delayed old host call cannot resolve the new binding.
        previous_relay_path, previous_relay, replacement_kind = replacement
        with exclusive_file_lock(work_dir / ".game-l10n/native/launch.lock", issue_code="E_NATIVE_LAUNCH_BUSY", blocking=True):
            require_control_plane_ready(work_dir)
            _require_current_data_plane_relay(previous_relay_path)
            receipt_unchanged = (
                previous_receipt_path.is_file() and previous_receipt_path.read_bytes() == previous_receipt_bytes
                if previous_receipt_bytes is not None
                else not os.path.lexists(previous_receipt_path)
            )
            if (
                json.loads(previous_relay_path.read_text(encoding="utf-8")) != previous_relay
                or not receipt_unchanged
                or int(control_plane_state(work_dir)["generation"]) != control_generation
            ):
                raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: launch evidence changed during repair")
            if replacement_kind == "unadmitted":
                if not _revisable_data_plane_relay(work_dir, previous_relay_path, previous_relay):
                    raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: launch evidence changed during repair")
            else:
                require_native_workers_ended(
                    work_dir, previous_relay_path, require_terminal_evidence=require_prior_terminal_evidence,
                )
            if discard_unverified_receipt is not None:
                if previous_receipt_bytes is None or previous_receipt_path.resolve() != previous_receipt_path:
                    raise ConfigurationError("E_NATIVE_RECEIPT_DISCARD_INVALID: receipt path changed")
                digest = hashlib.sha256(previous_receipt_bytes).hexdigest()
                discarded = previous_relay_path.with_name(f"discarded-receipt-{digest}.json")
                if os.path.lexists(discarded):
                    raise ConfigurationError("E_NATIVE_RECEIPT_DISCARD_INVALID: receipt or archive path changed")
                # Remove the receipt from the live slot without losing the evidence.
                previous_receipt_path.replace(discarded)
            elif previous_receipt_bytes is not None and receipt_task_id == previous_relay["task_id"]:
                archived = previous_relay_path.with_name("sealed-receipt.json")
                if os.path.lexists(archived):
                    if archived.read_bytes() != previous_receipt_bytes:
                        raise ConfigurationError("E_NATIVE_LAUNCH_INVALID: archived receipt changed")
                else:
                    _atomic_bytes(archived, previous_receipt_bytes)
            _atomic_json(stage_dir / "active-launch-revision.json", {"revision": output_dir.name})
            if worker_role == "inventory_reconciler":
                _inventory_constraint_locator(work_dir, output_dir, task_id)
    elif worker_role == "inventory_reconciler":
        _inventory_constraint_locator(work_dir, output_dir, task_id)
    return {
        "schema": DATA_PLANE_RELAY_SCHEMA,
        "status": "ready",
        "worker_role": worker_role,
        "stage": profile["stage"],
        "relay_path": _relative(work_dir, relay_path),
        "task_id": task_id,
        "control_generation": control_generation,
        **({"sdk_reconciliation": sdk_repair} if sdk_repair is not None else {}),
        **({"candidate_discovery": task["input_image_candidates"]} if task.get("input_image_candidates") else {}),
    }


def _load_bound_data_plane_relay(
    work_dir: Path, relay_path: Path, *, expected_generation: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate immutable relay/task bindings before result collection."""

    require_control_plane_ready(work_dir)
    require_control_plane_entry(work_dir)
    resolved_relay = relay_path.resolve()
    if not is_within(resolved_relay, work_dir) or not resolved_relay.is_file():
        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: relay must be a workspace file")
    _require_current_data_plane_relay(resolved_relay)
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
                set(relay) - {"launch_contract", "command_argv"} != relay_fields
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
    if "command_argv" in relay:
        from .native_runtime import _relay_command_argv
        if relay["command_argv"] != _relay_command_argv(work_dir, resolved_relay, relay):
            raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: lifecycle commands changed")

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
        or task.get("control_generation") != (
            int(control_plane_state(work_dir).get("generation", 0))
            if expected_generation is None else expected_generation
        )
    ):
        raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: task binding is invalid")
    try:
        source_policy = validate_content_source_policy(task["content_source_policy"])
    except (KeyError, ValueError):
        raise ConfigurationError("E_CONTENT_SOURCE_POLICY_IMMUTABLE") from None
    expected_kind = {
        "context_analyzer": "localization_context",
        "image_localizer": "image_localization",
    }.get(task["worker_role"], task["worker_role"])
    expected_policy_route = (
        f"data-plane:{task['worker_role']}"
        if task["worker_role"] in {"context_analyzer", "image_localizer"}
        else "local-inspection"
    )
    if (
        source_policy["task_kind"] != expected_kind
        or source_policy["route"] != expected_policy_route
    ):
        raise ConfigurationError("E_CONTENT_SOURCE_POLICY_IMMUTABLE")
    if "data_plane_route_chain" in task:
        route_chain = task["data_plane_route_chain"]
        route_index = task.get("data_plane_route_index")
        if (
            task.get("worker_role") not in DATA_PLANE_PROFILES
            or not isinstance(route_chain, list)
            or not route_chain
            or not isinstance(route_index, int)
            or isinstance(route_index, bool)
            or not 0 <= route_index < len(route_chain)
            or task.get("worker_route") != route_chain[route_index]
            or (
                relay_schema not in {LEGACY_DATA_PLANE_RELAY_SCHEMA, LEGACY_DATA_PLANE_RELAY_V2_SCHEMA}
                and relay.get("worker_route") != route_chain[route_index]
            )
        ):
            raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: route binding is invalid")

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
            value not in {"pending", "running", "ended", "not_started"}
            for value in worker_states.values()
        )
        or state.get("finalizer_required") is not plan["finalizer_required"]
        or state.get("finalizer_state")
        not in ({"pending", "running", "ended", "not_started"} if plan["finalizer_required"] else {"not_required"})
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
    terminal_unstarted = [slot for slot in required_slots if worker_states[str(slot)] == "not_started"]
    terminal_workers = sum(
        worker_states[str(slot)] == "ended" for slot in required_slots
    )
    finalizer_state = state["finalizer_state"]
    finalizer_required = bool(state["finalizer_required"])
    complete = (
        terminal_workers + len(terminal_unstarted) == len(required_slots)
        and (not finalizer_required or finalizer_state in {"ended", "not_started"})
    )
    return {
        "tracking_required": True,
        "required_workers": len(required_slots),
        "started_workers": len(required_slots) - len(unstarted) - len(terminal_unstarted),
        "terminal_workers": terminal_workers,
        "unstarted_slots": unstarted,
        "unterminated_slots": unterminated,
        "terminal_unstarted_slots": terminal_unstarted,
        "finalizer_skipped": finalizer_state == "not_started",
        "finalizer_required": finalizer_required,
        "finalizer_started": finalizer_state in {"running", "ended"},
        "finalizer_terminal": finalizer_state == "ended",
        "implicit_timeout_allowed": False,
        "complete": complete,
    }



def record_data_plane_failure(
    work_dir: Path,
    relay_path: Path,
    *,
    failure_code: str,
) -> dict[str, Any]:
    """Record an observed operational failure against started, bound workers."""

    if failure_code not in DATA_PLANE_OPERATIONAL_FAILURE_CODES:
        raise ConfigurationError("E_DATA_PLANE_FAILURE_INVALID: unsupported failure code")
    relay, task = _load_bound_data_plane_relay(work_dir, relay_path)
    if relay.get("schema") != DATA_PLANE_RELAY_SCHEMA or "launch_contract" not in relay:
        raise ConfigurationError("E_DATA_PLANE_FAILURE_INVALID: current native launch binding required")
    receipt_path = data_plane_receipt_path(
        work_dir, str(relay["worker_role"]), purpose=task.get("purpose", "")
    )
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            receipt = {}
        if (
            isinstance(receipt, dict)
            and receipt.get("task_id") == task["task_id"]
            and receipt.get("status") == "success"
        ):
            raise ConfigurationError("E_DATA_PLANE_FAILURE_INVALID: task success is already sealed")
    from .native_runtime import _load

    _, contract, ledger = _load(work_dir, relay_path)
    if contract.get("operational_failure_policy") != (
        "record_only_observed_provider_or_guard_errors; terminal_owner_cancellation_required_before_replacement"
    ):
        raise ConfigurationError(
            "E_DATA_PLANE_FAILURE_INVALID: lifecycle contract does not support failure recording"
        )
    workers = []
    for task_name, worker in ledger["workers"].items():
        if task_name not in contract["tasks"] or not isinstance(worker, dict):
            continue
        workers.append(
            {
                "task": task_name,
                "worker_id": worker["worker_id"],
                "model": worker["model"],
                "observed_model": worker["observed_model"],
            }
        )
    if not workers and failure_code != "execution_guard_denied":
        raise ConfigurationError("E_DATA_PLANE_FAILURE_INVALID: provider failure needs an actual bound worker")
    failure_path = _operational_failure_path(relay_path, str(task["task_id"]))
    if failure_path.exists():
        raise ConfigurationError("E_DATA_PLANE_FAILURE_INVALID: operational failure is already recorded")
    owner = {
        "host": contract["host"],
        "relay_role": relay["relay_contract"]["role"],
    }
    failure = {
        "schema": "parr0t.data-plane-operational-failure.v1",
        "task_id": task["task_id"],
        "worker_role": relay["worker_role"],
        "stage": relay["stage"],
        "control_generation": task["control_generation"],
        "failure_code": failure_code,
        "owner": owner,
        "workers": workers,
        "terminal": False,
    }
    _atomic_json(failure_path, failure)
    return {
        "status": "recorded",
        "failure_code": failure_code,
        "task_id": task["task_id"],
        "worker_role": relay["worker_role"],
        "workers": workers,
        "owner": owner,
        "cancellation_required": bool(workers),
        "cancellation_owner": "actual_host_relay_owner",
    }
def record_data_plane_relay_event(
    work_dir: Path, relay_path: Path, *, event: str, slot: int | None = None,
) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    with exclusive_file_lock(work_dir / ".game-l10n/data-plane-relay-events.lock", issue_code="E_DATA_PLANE_RELAY_BUSY", blocking=True):
        return _record_data_plane_relay_event(work_dir, relay_path, event=event, slot=slot)


def _record_data_plane_relay_event(
    work_dir: Path,
    relay_path: Path,
    *,
    event: str,
    slot: int | None = None,
) -> dict[str, Any]:
    """Record categorical relay lifecycle without observing semantic output."""

    relay, _ = _load_bound_data_plane_relay(work_dir, relay_path)
    if event in {"worker-started", "finalizer-started"} and "launch_contract" in relay:
        if ISOLATION_ISSUE in native_launch_summary(work_dir, relay_path)["issue_codes"]:
            raise ValueError(ISOLATION_ISSUE)
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


def _write_data_plane_receipt(
    work_dir: Path, relay: dict[str, Any], path: Path, receipt: dict[str, Any],
) -> None:
    relay_path = (work_dir / relay["task_path"]).with_name("relay.json")
    with exclusive_file_lock(work_dir / ".game-l10n/native/launch.lock", issue_code="E_NATIVE_LAUNCH_BUSY", blocking=True):
        require_control_plane_ready(work_dir)
        _require_current_data_plane_relay(relay_path)
        current = json.loads(relay_path.read_text(encoding="utf-8"))
        if current != relay or receipt["control_generation"] != int(control_plane_state(work_dir)["generation"]):
            raise ConfigurationError("E_DATA_PLANE_RELAY_INVALID: stale relay cannot publish a receipt")
        _atomic_json(path, receipt)


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
        eligibility = task.get("inventory_scope_contract") == TEXT_ELIGIBILITY_CONTRACT
        if result["status"] == "success" or (eligibility and result["status"] == "partial"):
            try:
                validate_inventory_display_scope(
                    inventory,
                    required=eligibility or task.get("inventory_scope_contract") == DISPLAY_SCOPE_SCHEMA,
                    required_text_source=eligibility,
                    screen_review_skip=(task.get("screen_review_skip_allowed") is True) if eligibility else None,
                )
                binding = task.get("locale_probe_binding")
                if binding is not None:
                    text_source = inventory["coverage"]["text_source"]
                    if any(text_source.get(key) != value for key, value in binding.items()):
                        raise ConfigurationError("E_INVENTORY_LOCALE_PROBE_MISMATCH")
            except ConfigurationError as exc:
                raise ConfigurationError(
                    "E_INVENTORY_DISPLAY_SCOPE_REQUIRED"
                ) from exc
    elif task.get("purpose") == LOCALE_PROBE_PURPOSE:
        if result["artifact_paths"] != [expected_artifact]:
            raise ConfigurationError("E_LOCALE_PROBE_RESULT_INVALID")
        from .workspace import load_config, require_sdk_activation
        if require_sdk_activation(work_dir, load_config(work_dir)) != task.get("sdk_activation"):
            raise ConfigurationError("E_LOCALE_PROBE_RESULT_INVALID")
        outcome = validate_locale_probe(
            json.loads(artifact_path.read_text(encoding="utf-8")), str(task["task_id"]), result,
        )
        result["issue_codes"] = [outcome]
        _atomic_bytes(artifact_path.with_suffix(".sha256.txt"), _digest(artifact_path).encode("ascii"))
    elif task.get("purpose") == EXTRACTOR_PREPARATION_PURPOSE:
        if result["status"] == "success":
            from .workspace import load_config, require_sdk_activation
            config = load_config(work_dir)
            require_extractor_preparation(work_dir, config, require_sdk_activation(work_dir, config))
    elif task.get("purpose") == LOCALE_LOCAL_PACKAGE_PURPOSE:
        from .filesystem_package import validate_filesystem_package
        from .tooling import _require_pack_input_binding

        binding = task["locale_package"]
        if (artifact_path != work_dir / expected_artifact
                or result["status"] != "success" or result["artifact_paths"] != [expected_artifact]
                or result["counts"] != {"error": 0, "pending": 0, "success": 1, "total": 1}
                or _digest(work_dir / ".game-l10n" / "completion-attestation.json") != binding["completion_sha256"]
                or _require_pack_input_binding(work_dir, work_dir / binding["result_path"]) != binding["result_sha256"]
                or validate_filesystem_package(artifact_path)["checkpoint_schema"] != "parr0t.local-release-integrity/v1"):
            raise ConfigurationError("E_LOCALE_PACKAGE_BINDING")
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
    elif task.get("webp_visual_review") and result["status"] in {"success", "partial"}:
        from .windows_webp_review import _review_counts

        result["counts"] = _review_counts(work_dir, task, artifact_path)
        result["status"] = "success" if result["counts"]["success"] == result["counts"]["total"] else "partial"
        _atomic_bytes(artifact_path.with_suffix(".sha256.txt"), _digest(artifact_path).encode("ascii"))
    elif task.get("purpose") == ENGINE_MEDIA_BINDINGS_PURPOSE:
        if result["status"] in {"success", "partial"}:
            from .image_localization import _verify_engine_media_binding_probe

            _verify_engine_media_binding_probe(work_dir, artifact_path, task)
            _atomic_bytes(artifact_path.with_suffix(".sha256.txt"), _digest(artifact_path).encode("ascii"))
    elif result["worker_role"] == "image_ui_inventory" and task.get("image_preview"):
        if result["status"] in {"success", "partial"}:
            # A scoped preview cannot attest full player-visible scope on its own.
            inspected = min(result["counts"]["success"], task["image_preview"]["asset_count"])
            total = len(task["bound_local_assets"])
            errors = min(result["counts"]["error"], task["image_preview"]["asset_count"] - inspected)
            result["status"] = "partial"
            result["issue_codes"] = sorted(set(result["issue_codes"]) | {"image_scope_unexamined"})
            result["counts"] = {"total": total, "success": inspected, "error": errors, "pending": total - inspected - errors}
    elif result["worker_role"] == "image_ui_inventory" and result["status"] == "success":
        discovery = None
        if task.get("input_image_candidates"):
            discovery_path = (work_dir / task["input_image_candidates"]).resolve()
            if (
                not is_within(discovery_path, work_dir) or not discovery_path.is_file()
                or _digest(discovery_path) != task.get("input_image_candidates_sha256")
            ):
                raise ConfigurationError("E_IMAGE_CANDIDATE_INPUT_CHANGED")
            discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
        scope_receipt = verify_image_ui_inventory(
            work_dir,
            artifact_path,
            game_roots=[Path(item) for item in task.get("source_roots", [])],
            candidate_discovery=discovery,
        )
        if scope_receipt["status"] == "scope_incomplete":
            result["status"] = "partial"
            result["issue_codes"] = sorted(set(result["issue_codes"]) | {"image_scope_unexamined"})
            pending = max(1, result["counts"].get("pending", 0))
            result["counts"] = {
                **result["counts"], "pending": pending,
                "total": max(result["counts"]["total"], result["counts"].get("success", 0) + result["counts"].get("error", 0) + pending),
            }
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
        "control_generation": task["control_generation"],
        **({"execution": execution} if current_relay else {}),
    }
    receipt_path = data_plane_receipt_path(
        work_dir, str(result["worker_role"]), purpose=task.get("purpose", "")
    )
    _seal_inventory_constraint(work_dir, relay, task, receipt)
    _write_data_plane_receipt(work_dir, relay, receipt_path, receipt)
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
    task: dict[str, Any],
    *,
    issue_code: str,
    execution: dict[str, Any] | None = None,
    additional_issue_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Close an ended worker categorically without interpreting its response."""

    current_relay = relay.get("schema") == DATA_PLANE_RELAY_SCHEMA
    if execution is None:
        execution = _relay_execution_summary(work_dir, relay)
    counts = {"total": 1, "success": 0, "error": 1, "pending": 1}
    if (
        relay.get("worker_role") == "context_analyzer"
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
        "issue_codes": [issue_code, *(additional_issue_codes or [])],
        "artifact_paths": [],
        "queue_transitions": [],
        "control_generation": task["control_generation"],
        **({"execution": execution} if current_relay else {}),
    }
    receipt_path = data_plane_receipt_path(
        work_dir, str(relay["worker_role"]), purpose=task.get("purpose", "")
    )
    _write_data_plane_receipt(work_dir, relay, receipt_path, receipt)
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


def _close_failed_execution(work_dir: Path, relay: dict[str, Any]) -> dict[str, Any]:
    state = _validated_relay_lifecycle(work_dir, relay)
    if state is None:
        return _relay_execution_summary(work_dir, relay)
    if "running" in state["worker_states"].values() or state["finalizer_state"] == "running":
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_EXECUTION_INCOMPLETE: actual started workers must end before replacement"
        )
    if "launch_contract" in relay:
        from .native_runtime import _load_launch_evidence

        relay_path = (work_dir / relay["task_path"]).parent / "relay.json"
        _, contract, ledger = _load_launch_evidence(work_dir, relay_path)
        fingerprint = hashlib.sha256(relay_path.read_bytes()).hexdigest()
        for name in ledger["workers"]:
            if name not in contract["tasks"]:
                continue
            if name == "finalizer":
                pending = state["finalizer_state"] == "pending"
            else:
                slot = name.removeprefix("worker-")
                pending = state["worker_states"].get(slot) == "pending"
            if not pending:
                continue
            key = hashlib.sha256(f"{fingerprint}\0{name}".encode()).hexdigest()
            admission = work_dir / ".game-l10n/native/omp-launch-admissions" / f"{key}.json"
            try:
                record = json.loads(admission.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
            if (
                record.get("schema") != "parr0t.omp-native-admission.v1"
                or record.get("relay_sha256") != fingerprint or record.get("task") != name
                or record.get("state") not in {"ended", "failed"}
            ):
                raise ConfigurationError("E_DATA_PLANE_RELAY_EXECUTION_INCOMPLETE: record the actual admitted worker terminal event")
            if name == "finalizer":
                state["finalizer_state"] = "ended"
            else:
                state["worker_states"][slot] = "ended"
    state["worker_states"] = {
        slot: "not_started" if value == "pending" else value
        for slot, value in state["worker_states"].items()
    }
    if state["finalizer_state"] == "pending":
        state["finalizer_state"] = "not_started"
    state.pop("state_fingerprint")
    state["event_count"] += 1
    _atomic_json(
        work_dir / relay["execution_plan"]["lifecycle_path"], _with_state_fingerprint(state),
    )
    return _relay_execution_summary(work_dir, relay)


def seal_data_plane_task(
    work_dir: Path,
    relay_path: Path,
    *,
    worker_ended: bool = False,
) -> dict[str, Any]:
    """Seal one data-plane task, including a worker ending without valid output."""

    relay, task = _load_bound_data_plane_relay(work_dir, relay_path)
    if "launch_contract" in relay:
        require_native_workers_ended(work_dir, relay_path)
    execution = _relay_execution_summary(work_dir, relay)
    operational_failure_path = _operational_failure_path(
        relay_path, str(task["task_id"])
    )
    if operational_failure_path.is_file():
        if not worker_ended:
            raise ConfigurationError(
                "E_DATA_PLANE_CANCELLATION_REQUIRED: cancel the recorded child through its host owner and seal only after it ends"
            )
        failure = json.loads(operational_failure_path.read_text(encoding="utf-8"))
        expected_failure = {
            "schema": "parr0t.data-plane-operational-failure.v1",
            "task_id": task["task_id"],
            "worker_role": relay["worker_role"],
            "stage": relay["stage"],
            "control_generation": task["control_generation"],
            "owner": {
                "host": relay.get("launch_contract", {}).get("host"),
                "relay_role": relay.get("relay_contract", {}).get("role"),
            },
        }
        if (
            not isinstance(failure, dict)
            or any(failure.get(key) != value for key, value in expected_failure.items())
            or failure.get("failure_code") not in DATA_PLANE_OPERATIONAL_FAILURE_CODES
            or type(failure.get("terminal")) is not bool
        ):
            raise ConfigurationError("E_DATA_PLANE_FAILURE_INVALID: failure binding changed")
        from .native_runtime import _load_launch_evidence

        _, contract, ledger = _load_launch_evidence(work_dir, relay_path)
        if contract.get("operational_failure_policy") != (
            "record_only_observed_provider_or_guard_errors; terminal_owner_cancellation_required_before_replacement"
        ):
            raise ConfigurationError("E_DATA_PLANE_FAILURE_INVALID: lifecycle contract changed")
        expected_workers = [
            {
                "task": name,
                "worker_id": row["worker_id"],
                "model": row["model"],
                "observed_model": row["observed_model"],
            }
            for name, row in ledger["workers"].items()
            if name in contract["tasks"]
        ]
        if not isinstance(failure.get("workers"), list) or any(worker not in expected_workers for worker in failure["workers"]):
            raise ConfigurationError("E_DATA_PLANE_FAILURE_INVALID: worker binding changed")
        execution = _close_failed_execution(work_dir, relay)
        failure["terminal_workers"] = expected_workers
        failure["terminal"] = True
        _atomic_json(operational_failure_path, failure)
        try:
            success = _seal_bound_data_plane_task(work_dir, relay, task)
            if success["status"] == "success":
                return success
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
            pass
        return _seal_ended_data_plane_task(
            work_dir,
            relay,
            task,
            issue_code="operational_error",
            execution=execution,
            additional_issue_codes=[failure["failure_code"]],
        )
    launch = native_launch_evidence(work_dir, relay_path) if "launch_contract" in relay else None
    if worker_ended and not execution["complete"]:
        execution = _close_failed_execution(work_dir, relay)
        if launch is None or not launch["issue_codes"] or launch["issue_codes"] == ["E_NATIVE_WORKER_NOT_STARTED"]:
            return _seal_ended_data_plane_task(
                work_dir, relay, task, issue_code="relay_execution_incomplete", execution=execution,
            )
    if launch is not None:
        if launch["issue_codes"]:
            return _seal_ended_data_plane_task(
                work_dir, relay, task, issue_code=launch["issue_codes"][0], execution=execution,
            )
    if not execution["complete"]:
        raise ConfigurationError(
            "E_DATA_PLANE_RELAY_EXECUTION_INCOMPLETE: required worker slots or finalizer did not finish"
        )
    try:
        return _seal_bound_data_plane_task(work_dir, relay, task)
    except ControlEnvelopeIssueCodeError:
        if not worker_ended:
            raise
        return _seal_ended_data_plane_task(
            work_dir, relay, task, issue_code="malformed_response", execution=execution,
            additional_issue_codes=["invalid_envelope_issue_codes"],
        )
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
    ) as exc:
        if not worker_ended:
            raise
        if str(exc) == "E_INVENTORY_DISPLAY_SCOPE_REQUIRED":
            return _seal_ended_data_plane_task(
                work_dir,
                relay,
                task,
                issue_code="inventory_display_scope_required",
                execution=execution,
            )
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
