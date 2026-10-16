from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .control_plane import (
    LEAK_INCIDENT_CODES,
    ROLE_STAGES,
    WORKER_ROLES,
    acknowledge_control_plane_rotation,
    control_plane_state,
    public_control_plane_state,
    record_control_plane_leak,
    require_control_plane_ready,
    validate_control_envelope,
)
from .errors import ConfigurationError, GameL10nError, ToolExecutionError
from .coverage import (
    COMPLETION_SCHEMA,
    completion_attestation,
    record_runtime_coverage,
    require_inventory_attestation,
    runtime_coverage_state,
    save_completion_attestation,
    save_inventory_attestation,
)
from .context_analysis import ContextAnalyzer, analyze_with_cache
from .corrections import apply_user_corrections
from .failure_queue import (
    FAILURE_QUEUE_SCHEMA,
    held_checkpoint_path,
    load_failure_queue,
    update_failure_queue,
)
from .formats import (
    read_existing_translations,
    read_segments,
    read_translation_results,
    write_results,
)
from .filesystem_package import build_filesystem_package, validate_filesystem_package
from .models import CharacterProfile, ContextMode, ContextPack, GlossaryEntry, ProjectContext
from .instance_release import validate_instance_release
from .native_pass import import_native_shard, merge_native_pass, prepare_native_tasks
from .orchestrator import LocalizationOrchestrator, OrchestratorConfig
from .privacy import is_within, require_external_workdir
from .providers import ProviderRegistry
from .provider_policy import require_direct_provider_pipeline
from .runtime_approval import create_runtime_approval
from .store import stable_hash
from .steam import build_steam_package, inspect_installed_state, validate_steam_package
from .translation_exchange import prepare_translation_exchange, seal_translation_exchange
from .tooling import cached_tool_state, run_cached_tool
from .wolf.cli import register_wolf_parser
from .workspace import (
    initialize_workspace,
    refresh_sdk_inventory,
    load_config,
    prepare_sdk_profile,
    publish_sdk_profile,
    record_sdk_activation,
    record_sdk_evaluation,
    record_sdk_selection,
    require_sdk_activation,
    require_sdk_selection,
    update_repository_policy,
)


STATUS_ACTION_CONTRACTS: dict[str, tuple[str, ...]] = {
    "stop_current_control_plane": ("docs/control-plane-contract.md",),
    "ask_user_for_initial_git_choice": ("skills/request-scope-guard/SKILL.md",),
    "configure_game_root": (),
    "fix_missing_game_root": (),
    "inventory_sdk_branches": (
        "docs/sdk-selection.md",
        "docs/sdk-branch-contract.md",
    ),
    "record_target_identity_evaluate_and_select_sdk": (
        "docs/sdk-selection.md",
        "docs/sdk-branch-contract.md",
    ),
    "activate_selected_sdk_implementation": (
        "docs/sdk-selection.md",
        "docs/sdk-branch-contract.md",
    ),
    "unpack_and_extract_ordered_script": ("docs/coverage-gates.md",),
    "verify_independent_source_inventory": ("docs/coverage-gates.md",),
    "resolve_source_inventory_gaps": ("docs/coverage-gates.md",),
    "analyze_complete_script_context": (
        "docs/coverage-gates.md",
        "docs/provider-protocol.md",
        "docs/control-plane-contract.md",
    ),
    "await_explicit_user_request_to_resume_failure_queue": (
        "docs/coverage-gates.md",
        "docs/fallback-routing.md",
        "docs/control-plane-contract.md",
    ),
    "resolve_blocking_terminal_unresolved": (
        "docs/coverage-gates.md",
        "docs/fallback-routing.md",
        "docs/control-plane-contract.md",
    ),
    "repair_invalid_workspace_state": (
        "docs/coverage-gates.md",
        "docs/control-plane-contract.md",
    ),
    "reconcile_stale_failure_queue": (
        "docs/coverage-gates.md",
        "docs/fallback-routing.md",
        "docs/control-plane-contract.md",
    ),
    "run_primary_translation_review_pipeline": (
        "docs/coverage-gates.md",
        "docs/review-pipeline.md",
        "docs/fallback-routing.md",
        "docs/provider-protocol.md",
        "docs/control-plane-contract.md",
        "docs/execution-budget.md",
    ),
    "resolve_translation_review_gaps": (
        "docs/coverage-gates.md",
        "docs/review-pipeline.md",
        "docs/fallback-routing.md",
        "docs/provider-protocol.md",
        "docs/control-plane-contract.md",
        "docs/execution-budget.md",
    ),
    "rebuild_current_inventory_translation_attestation": (
        "docs/coverage-gates.md",
        "docs/review-pipeline.md",
        "docs/control-plane-contract.md",
    ),
    "validate_and_pack_recoverable_patch": (
        "docs/coverage-gates.md",
        "docs/execution-budget.md",
    ),
    "local_patch_ready": (),
}


def _status_contracts(action: str) -> list[str]:
    return list(STATUS_ACTION_CONTRACTS[action])


def _read_status_object(
    path: Path, *, label: str, expected_schema: str | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, f"{label}_UNREADABLE"
    if not isinstance(payload, dict):
        return None, f"{label}_NOT_OBJECT"
    if expected_schema is not None and payload.get("schema") != expected_schema:
        return None, f"{label}_SCHEMA_INVALID"
    return payload, None


def _context_state_valid(
    payload: dict[str, Any], context_path: Path, source_segments_digest: str
) -> bool:
    execution = payload.get("execution")
    if payload.get("status") != "success" or not isinstance(execution, dict):
        return False
    total = execution.get("total_parts")
    completed = execution.get("completed_parts")
    parts = execution.get("parts")
    if not context_path.is_file():
        return False
    output_body = context_path.read_bytes()
    return bool(
        execution.get("status") == "success"
        and isinstance(total, int)
        and total > 0
        and completed == total
        and isinstance(parts, list)
        and len(parts) == total
        and all(isinstance(item, dict) and item.get("status") == "success" for item in parts)
        and payload.get("source_segments_digest") == source_segments_digest
        and payload.get("output_sha256") == hashlib.sha256(output_body).hexdigest()
        and payload.get("output_size") == len(output_body)
    )


def _completion_state_valid(payload: dict[str, Any]) -> bool:
    source_inventory = payload.get("source_inventory")
    result_alignment = payload.get("result_alignment")
    target_scope = payload.get("target_scope")
    semantic_review = payload.get("semantic_review")
    result_artifact = payload.get("result_artifact")
    structure_valid = bool(
        isinstance(source_inventory, dict)
        and isinstance(source_inventory.get("fingerprint"), str)
        and source_inventory.get("fingerprint")
        and isinstance(source_inventory.get("static_complete"), bool)
        and isinstance(result_alignment, dict)
        and isinstance(result_alignment.get("complete"), bool)
        and isinstance(target_scope, dict)
        and isinstance(target_scope.get("full_inventory"), bool)
        and isinstance(semantic_review, dict)
        and isinstance(semantic_review.get("complete"), bool)
        and isinstance(semantic_review.get("blocking_unresolved_count"), int)
        and semantic_review.get("blocking_unresolved_count", -1) >= 0
        and isinstance(semantic_review.get("terminal_unresolved_count"), int)
        and semantic_review.get("terminal_unresolved_count", -1) >= 0
        and isinstance(semantic_review.get("failed_count"), int)
        and semantic_review.get("failed_count", -1) >= 0
        and isinstance(payload.get("accounting_complete"), bool)
        and isinstance(payload.get("semantic_completion"), bool)
        and isinstance(payload.get("target_completion"), bool)
        and isinstance(payload.get("static_completion"), bool)
        and isinstance(payload.get("issue_codes"), list)
        and isinstance(result_artifact, dict)
        and isinstance(result_artifact.get("path"), str)
        and bool(result_artifact.get("path"))
        and isinstance(result_artifact.get("sha256"), str)
        and len(result_artifact.get("sha256", "")) == 64
        and isinstance(result_artifact.get("size"), int)
        and result_artifact.get("size", -1) >= 0
    )
    if not structure_valid:
        return False
    accounting_complete = payload["accounting_complete"]
    target_completion = payload["target_completion"]
    static_completion = payload["static_completion"]
    blocking_unresolved = semantic_review["blocking_unresolved_count"]
    terminal_unresolved = semantic_review["terminal_unresolved_count"]
    expected_accounting = bool(
        result_alignment["complete"]
        and semantic_review["complete"]
        and semantic_review["failed_count"] == 0
    )
    if accounting_complete != expected_accounting:
        return False
    if payload["semantic_completion"] != bool(
        accounting_complete and terminal_unresolved == 0
    ):
        return False
    if target_completion != bool(accounting_complete and blocking_unresolved == 0):
        return False
    if static_completion != bool(
        source_inventory["static_complete"]
        and result_alignment["complete"]
        and target_scope["full_inventory"]
        and target_completion
    ):
        return False
    return True


def _result_artifact_binding(work_dir: Path, path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not is_within(resolved, work_dir):
        raise ValueError("Result artifact must stay inside the agent work directory")
    body = resolved.read_bytes()
    return {
        "path": str(resolved),
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
    }


def _completion_result_issue(work_dir: Path, payload: dict[str, Any]) -> str | None:
    artifact = payload.get("result_artifact")
    if not isinstance(artifact, dict):
        return "COMPLETION_RESULT_BINDING_MISSING"
    path_value = artifact.get("path")
    if not isinstance(path_value, str) or not path_value:
        return "COMPLETION_RESULT_PATH_INVALID"
    path = Path(path_value).resolve()
    if not is_within(path, work_dir):
        return "COMPLETION_RESULT_OUTSIDE_WORKSPACE"
    if not path.is_file():
        return "COMPLETION_RESULT_MISSING"
    current = _result_artifact_binding(work_dir, path)
    if current["sha256"] != artifact.get("sha256") or current["size"] != artifact.get(
        "size"
    ):
        return "COMPLETION_RESULT_CHANGED"
    return None


def _sdk_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _path_in_workspace(work_dir: Path, value: str, *, must_exist: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = work_dir / path
    path = path.resolve()
    if not is_within(path, work_dir):
        raise ValueError(f"Data path must stay inside the agent work directory: {path}")
    if must_exist and not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    return path


def _source_path(work_dir: Path, value: str, config: dict[str, Any]) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = work_dir / path
    path = path.resolve()
    allowed_roots = [work_dir, *(Path(item).resolve() for item in config.get("game_roots", []))]
    if not any(is_within(path, root) or path == root for root in allowed_roots):
        raise ValueError(f"Input is outside the workspace and configured read-only game roots: {path}")
    if not path.exists():
        raise ValueError(f"Path does not exist: {path}")
    return path


def _load_dataclass_list(path: Path, cls: type) -> list[Any]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    allowed = {item.name for item in fields(cls)}
    return [cls(**{key: value for key, value in row.items() if key in allowed}) for row in payload]


def _orchestrator(work_dir: Path, context_pack: ContextPack) -> LocalizationOrchestrator:
    config = load_config(work_dir)
    require_direct_provider_pipeline(config, "direct translation/review orchestration")
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    project = ProjectContext(**config["project"])
    routing = config["routing"]
    orchestrator_config = OrchestratorConfig(
        translator_chain=list(routing["translators"]),
        reviewer_chain=list(routing.get("reviewers", [])),
        rewriter_chain=list(routing.get("rewriters", [])),
        require_review=bool(routing.get("require_review", True)),
        review_max_rounds=int(routing.get("review_max_rounds", 2)),
        max_workers=int(routing.get("max_workers", 1)),
        max_length_ratio=float(routing.get("max_length_ratio", 4.0)),
        extra_protected_patterns=list(routing.get("extra_protected_patterns", [])),
        use_style_memory=bool(routing.get("use_style_memory", True)),
        style_anchor_limit=int(routing.get("style_anchor_limit", 6)),
        retry_refusals_once=bool(routing.get("retry_refusals_once", True)),
        grace_retries=int(routing.get("grace_retries", 0)),
        grace_retry_codes=list(
            routing.get(
                "grace_retry_codes",
                [
                    "malformed_response",
                    "provider_error",
                    "rate_limited",
                    "timeout",
                    "integrity_invalid",
                ],
            )
        ),
        defer_fallbacks_until_primary_complete=bool(
            routing.get("defer_fallbacks_until_primary_complete", False)
        ),
        preserve_source_on_failure=bool(routing.get("preserve_source_on_failure", False)),
        review_refusal_policy=str(routing.get("review_refusal_policy", "fallback")),
    )
    file_config = config.get("files", {})
    glossary = _load_dataclass_list(
        _path_in_workspace(work_dir, file_config.get("glossary", "context/glossary.json")), GlossaryEntry
    )
    characters = _load_dataclass_list(
        _path_in_workspace(work_dir, file_config.get("characters", "context/characters.json")), CharacterProfile
    )
    registry = ProviderRegistry.from_config(config, work_dir)
    return LocalizationOrchestrator(
        registry=registry,
        config=orchestrator_config,
        project=project,
        glossary=glossary,
        characters=characters,
        context_pack=context_pack,
        work_dir=work_dir,
    )


def _analyze_context(
    work_dir: Path, segments: list, *, force: bool = False, context_mode: str | None = None
) -> tuple[ContextPack, bool]:
    config = load_config(work_dir)
    require_direct_provider_pipeline(config, "direct context-provider execution")
    routing = config["routing"]
    chain = list(routing.get("context_analyzers") or routing["translators"])
    registry = ProviderRegistry.from_config(config, work_dir)
    analyzer = ContextAnalyzer(
        registry,
        chain,
        max_chunk_chars=int(routing.get("context_chunk_chars", 30000)),
        context_mode=context_mode or str(config.get("project", {}).get("context_mode", "auto")),
    )
    context_relative = config.get("files", {}).get("context_pack", "context/context-pack.json")
    return analyze_with_cache(
        analyzer=analyzer,
        segments=segments,
        output_path=_path_in_workspace(work_dir, context_relative),
        state_path=work_dir / ".game-l10n" / "context-state.json",
        force=force,
    )


def _finalize_translation_results(
    *,
    work_dir: Path,
    target_segments: list,
    inventory_segments: list,
    results: list,
    destination: Path,
    inventory_state: dict[str, Any],
    review_required: bool,
) -> tuple[dict[str, Any], int]:
    queue = update_failure_queue(
        work_dir,
        target_segments,
        results,
        inventory_fingerprint=inventory_state["primary_fingerprint"],
    )
    checkpoint = held_checkpoint_path(work_dir)
    blocking = queue["blocking_count"] > 0
    result_artifact = checkpoint if blocking else destination
    write_results(result_artifact, results)
    if queue["pending_count"] and not blocking:
        write_results(checkpoint, results)
    runtime = runtime_coverage_state(work_dir, inventory_state["primary_fingerprint"])
    completion = completion_attestation(
        target_segments,
        results,
        inventory_segments=inventory_segments,
        inventory=inventory_state,
        runtime=runtime,
        review_required=review_required,
    )
    completion["result_artifact"] = _result_artifact_binding(work_dir, result_artifact)
    completion_path = save_completion_attestation(work_dir, completion)
    failed = sum(item.status == "failed" for item in results)
    source_preserved = sum(item.status == "source_preserved" for item in results)
    if blocking:
        return (
            {
                "status": "held",
                "segments": len(results),
                "failed": failed,
                "source_preserved": source_preserved,
                "failure_queue": {
                    "pending": queue["pending_count"],
                    "operational": queue["operational_count"],
                    "blocking": queue["blocking_count"],
                },
                "artifact_emitted": False,
                "held_checkpoint": str(checkpoint),
                "completion_attestation": str(completion_path),
                "target_completion": completion["target_completion"],
                "static_completion": completion["static_completion"],
                "runtime_completion": completion["runtime_completion"],
                "issue_codes": completion["issue_codes"],
            },
            3,
        )
    static_complete = completion["static_completion"]
    target_complete = completion["target_completion"]
    return (
        {
            "status": (
                "static_complete"
                if static_complete
                else "target_scope_complete"
                if target_complete
                else "incomplete"
            ),
            "segments": len(results),
            "failed": failed,
            "source_preserved": source_preserved,
            "failure_queue": {
                "pending": queue["pending_count"],
                "operational": queue["operational_count"],
                "blocking": queue["blocking_count"],
            },
            "artifact_emitted": True,
            "output": str(destination),
            "resume_checkpoint": str(checkpoint) if queue["pending_count"] else None,
            "completion_attestation": str(completion_path),
            "target_completion": target_complete,
            "static_completion": static_complete,
            "runtime_completion": completion["runtime_completion"],
            "issue_codes": completion["issue_codes"],
        },
        0 if target_complete else 2,
    )
def cmd_init(args: argparse.Namespace) -> int:
    initialize_workspace(
        Path(args.workspace),
        Path(args.sdk_root or _sdk_root()),
        git_choice=args.git_choice,
        game_roots=[Path(item) for item in args.game_root],
        provider_preset=args.provider_preset,
    )
    print(f"Initialized private agent workspace: {Path(args.workspace).resolve()}")
    if args.git_choice == "pending":
        print("ACTION_REQUIRED: ask the user whether Git use is allowed, then run `game-l10n policy`.")
    print(
        "ACTION_REQUIRED: record the target engine/version identity, evaluate and select an SDK, "
        "then run sdk-activate before extraction."
    )
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    publish = None
    if args.publish_approved is not None:
        publish = args.publish_approved == "yes"
    update_repository_policy(work_dir, initial_choice=args.git_choice, publish_approved=publish)
    print("Repository policy updated. This command does not initialize Git or publish anything.")
    return 0


def cmd_translate(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_direct_provider_pipeline(config, "game-l10n translate")
    require_sdk_activation(work_dir, config)
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    destination = _path_in_workspace(work_dir, args.output)
    if destination == source:
        raise ValueError("Translation output must be a separate patch file; the source is read-only")
    segments = read_segments(source)
    context_source = _path_in_workspace(
        work_dir, args.context_input or args.input, must_exist=True
    )
    if destination == context_source:
        raise ValueError("Translation output must not overwrite the verified context inventory")
    inventory_state = require_inventory_attestation(work_dir, context_source)
    inventory_segments = read_segments(context_source)
    context_pack, context_cached = _analyze_context(
        work_dir,
        inventory_segments,
        force=args.force_context,
        context_mode=args.context_mode,
    )
    orchestrator = _orchestrator(work_dir, context_pack)
    results = orchestrator.translate_many(segments, force=args.force)
    summary, exit_code = _finalize_translation_results(
        work_dir=work_dir,
        target_segments=segments,
        inventory_segments=inventory_segments,
        results=results,
        destination=destination,
        inventory_state=inventory_state,
        review_required=bool(config.get("routing", {}).get("require_review", True)),
    )
    summary.update(
        {
            "cached": sum(item.from_cache for item in results),
            "context_cached": context_cached,
            "context_mode": context_pack.context_mode,
        }
    )
    print(json.dumps(summary))
    return exit_code


def cmd_review_existing(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_direct_provider_pipeline(config, "game-l10n review-existing")
    require_sdk_activation(work_dir, config)
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    existing_path = _path_in_workspace(work_dir, args.existing, must_exist=True)
    destination = _path_in_workspace(work_dir, args.output)
    if destination in {source, existing_path}:
        raise ValueError("Full re-review output must be a new artifact")
    segments = read_segments(source)
    context_source = _path_in_workspace(
        work_dir, args.context_input or args.input, must_exist=True
    )
    if destination == context_source:
        raise ValueError("Full re-review output must not overwrite the verified context inventory")
    inventory_state = require_inventory_attestation(work_dir, context_source)
    inventory_segments = read_segments(context_source)
    context_pack, context_cached = _analyze_context(
        work_dir,
        inventory_segments,
        force=args.force_context,
        context_mode=args.context_mode,
    )
    candidates = read_existing_translations(existing_path)
    results = _orchestrator(work_dir, context_pack).review_existing_many(segments, candidates)
    summary, exit_code = _finalize_translation_results(
        work_dir=work_dir,
        target_segments=segments,
        inventory_segments=inventory_segments,
        results=results,
        destination=destination,
        inventory_state=inventory_state,
        review_required=True,
    )
    summary.update(
        {
            "mode": "full_existing_candidate_review",
            "context_cached": context_cached,
            "context_mode": context_pack.context_mode,
        }
    )
    print(json.dumps(summary))
    return exit_code


def cmd_resume_failures(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_direct_provider_pipeline(config, "game-l10n resume-failures")
    require_sdk_activation(work_dir, config)
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    destination = _path_in_workspace(work_dir, args.output)
    context_source = _path_in_workspace(
        work_dir, args.context_input or args.input, must_exist=True
    )
    if destination in {source, context_source}:
        raise ValueError("Resumed output must not overwrite source or context inventory data")
    inventory_state = require_inventory_attestation(work_dir, context_source)
    queue = load_failure_queue(work_dir, inventory_state["primary_fingerprint"])
    retry_ids = {
        item["target_id"]
        for item in queue["entries"]
        if item.get("retryable_on_user_resume") is True
    }
    if not retry_ids:
        raise ValueError("Failure queue has no targets eligible for user-requested resume")
    segments = read_segments(source)
    segment_by_id = {item.id: item for item in segments}
    if not retry_ids <= set(segment_by_id):
        raise ValueError("Failure queue contains targets absent from the current source")
    checkpoint = held_checkpoint_path(work_dir)
    if not checkpoint.is_file():
        raise ValueError("Held result checkpoint is missing")
    previous = {item.segment_id: item for item in read_translation_results(checkpoint)}
    if set(previous) != set(segment_by_id):
        raise ValueError("Held result checkpoint does not match the current target set")
    inventory_segments = read_segments(context_source)
    context_pack, context_cached = _analyze_context(
        work_dir,
        inventory_segments,
        force=args.force_context,
        context_mode=args.context_mode,
    )
    retry_segments = [item for item in segments if item.id in retry_ids]
    retried = _orchestrator(work_dir, context_pack).translate_many(retry_segments, force=True)
    previous.update({item.segment_id: item for item in retried})
    merged = [previous[item.id] for item in segments]
    summary, exit_code = _finalize_translation_results(
        work_dir=work_dir,
        target_segments=segments,
        inventory_segments=inventory_segments,
        results=merged,
        destination=destination,
        inventory_state=inventory_state,
        review_required=bool(config.get("routing", {}).get("require_review", True)),
    )
    summary.update(
        {
            "mode": "failure_queue_resume",
            "retried": len(retried),
            "context_cached": context_cached,
            "context_mode": context_pack.context_mode,
        }
    )
    print(json.dumps(summary))
    return exit_code


def cmd_apply_user_correction(args: argparse.Namespace) -> int:
    if not args.user_requested:
        raise ValueError("Exact correction requires explicit user-supplied or user-approved wording")
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    base_results = (
        _path_in_workspace(work_dir, args.existing, must_exist=True)
        if args.existing
        else None
    )
    checkpoint = _path_in_workspace(work_dir, args.checkpoint, must_exist=True)
    request = _path_in_workspace(work_dir, args.request, must_exist=True)
    destination = _path_in_workspace(work_dir, args.output)
    if destination in {source, checkpoint, request} or (
        base_results is not None and destination == base_results
    ):
        raise ValueError("Correction output must be a new workspace artifact")
    inventory_state = require_inventory_attestation(work_dir, source)
    summary = apply_user_corrections(
        segments=read_segments(source),
        base_results_path=base_results,
        checkpoint_path=checkpoint,
        request_path=request,
        output_path=destination,
        expected_inventory_fingerprint=inventory_state["primary_fingerprint"],
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_verify_inventory(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    require_sdk_activation(work_dir, load_config(work_dir))
    primary = _path_in_workspace(work_dir, args.primary, must_exist=True)
    independent = _path_in_workspace(work_dir, args.independent, must_exist=True)
    if primary == independent:
        raise ValueError("Primary and independent inventory files must differ")
    state = save_inventory_attestation(work_dir, primary, independent)
    print(json.dumps(state))
    return 0 if state["static_complete"] else 1


def cmd_record_runtime_coverage(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    require_sdk_activation(work_dir, load_config(work_dir))
    report = _path_in_workspace(work_dir, args.report, must_exist=True)
    state = record_runtime_coverage(work_dir, report)
    print(json.dumps(state))
    return 0 if state["runtime_complete"] else 1


def cmd_analyze_context(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_direct_provider_pipeline(config, "game-l10n analyze-context")
    require_sdk_activation(work_dir, config)
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    require_inventory_attestation(work_dir, source)
    context_pack, cached = _analyze_context(
        work_dir,
        read_segments(source),
        force=args.force,
        context_mode=args.context_mode,
    )
    config = load_config(work_dir)
    output = _path_in_workspace(
        work_dir, config.get("files", {}).get("context_pack", "context/context-pack.json")
    )
    print(
        json.dumps(
            {
                "cached": cached,
                "gate_status": "success",
                "output": str(output),
                "context_mode": context_pack.context_mode,
                "scenes": len(context_pack.scene_summaries),
                "characters": len(context_pack.character_guides),
                "screens": len(context_pack.screen_flows),
                "features": len(context_pack.feature_guides),
                "knowledge_nodes": len(context_pack.knowledge_nodes),
            }
        )
    )
    return 0


def cmd_run_tool(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    activation = require_sdk_activation(work_dir, config)
    if activation.get("workspace_tools") is None:
        raise ValueError(
            "The selected SDK owns its engine tools; run the adapter command from its activated checkout"
        )
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    command = config.get("tools", {}).get(args.stage)
    if not isinstance(command, list) or not command:
        raise ValueError(f"No command configured for tool stage: {args.stage}")
    result = run_cached_tool(
        stage=args.stage,
        command=[str(item) for item in command],
        input_path=_source_path(work_dir, args.input, config),
        output_path=_path_in_workspace(work_dir, args.output),
        work_dir=work_dir,
        force=args.force,
    )
    print(json.dumps(result))
    return 0


def cmd_import_native_shard(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    require_sdk_activation(work_dir, load_config(work_dir))
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    result = import_native_shard(work_dir, source, args.name)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_validate_control_envelope(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    result = validate_control_envelope(
        work_dir,
        source,
        expected_role=args.expected_role,
        expected_stage=args.expected_stage,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_control_plane_status(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    print(
        json.dumps(
            public_control_plane_state(control_plane_state(work_dir)), ensure_ascii=False
        )
    )
    return 0


def cmd_record_control_plane_leak(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    result = record_control_plane_leak(work_dir, args.incident_code)
    print(json.dumps(public_control_plane_state(result), ensure_ascii=False))
    return 0


def cmd_resume_control_plane(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    if not args.fresh_session_confirmed:
        raise ConfigurationError(
            "E_CONTROL_PLANE_FRESH_SESSION_REQUIRED: only a newly started metadata-only "
            "control plane may acknowledge rotation"
        )
    state = control_plane_state(work_dir)
    result = acknowledge_control_plane_rotation(work_dir, state["incident_id"])
    print(json.dumps(public_control_plane_state(result), ensure_ascii=False))
    return 0


def cmd_prepare_native_pass(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    output = _path_in_workspace(work_dir, args.output)
    execution = config.get("execution", {})
    legacy_default = (
        execution.get("native_shard_records") == 256
        and "native_shard_material_tokens" not in execution
    )
    max_records = (
        args.max_records
        if args.max_records is not None
        else 32
        if legacy_default
        else int(execution.get("native_shard_records", 32))
    )
    max_material_tokens = (
        args.max_material_tokens
        if args.max_material_tokens is not None
        else int(execution.get("native_shard_material_tokens", 12_000))
    )
    result = prepare_native_tasks(
        work_dir,
        source,
        read_segments(source),
        output,
        max_records=max_records,
        max_material_tokens=max_material_tokens,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_merge_native_pass(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    require_sdk_activation(work_dir, load_config(work_dir))
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    destination = _path_in_workspace(work_dir, args.output)
    result = merge_native_pass(work_dir, read_segments(source), destination)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["complete"] else 1


def cmd_prepare_translation_exchange(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    require_sdk_activation(work_dir, load_config(work_dir))
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    result = prepare_translation_exchange(
        work_dir,
        read_segments(source),
        _path_in_workspace(work_dir, args.output),
        exchange_id=args.exchange_id,
        mode=args.mode,
        source_queue=args.source_queue,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_seal_translation_exchange(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    require_sdk_activation(work_dir, load_config(work_dir))
    result = seal_translation_exchange(
        work_dir,
        _path_in_workspace(work_dir, args.bundle, must_exist=True),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "complete" else 1


def cmd_status(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    contract_base = config.get("sdk_root")
    control_state = control_plane_state(work_dir)
    if control_state["status"] == "rotation_required":
        action = "stop_current_control_plane"
        print(
            json.dumps(
                {
                    "workspace": str(work_dir),
                    "control_plane": public_control_plane_state(control_state),
                    "contract_base": contract_base,
                    "next_actions": [action],
                    "required_contracts": _status_contracts(action),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    policy = config.get("repository", {})
    visible_policy = {"initial_choice": policy.get("initial_choice", "pending")}
    if policy.get("publish_approved") is True:
        visible_policy["publish_approved"] = True
    action: str | None = None
    state_issue_codes: list[str] = []
    local_artifact: dict[str, Any] = {"status": "not_checked"}
    if policy.get("initial_choice") == "pending":
        action = "ask_user_for_initial_git_choice"
    game_roots = [Path(item) for item in config.get("game_roots", [])]
    missing_roots = [str(path) for path in game_roots if not path.exists()]
    if action is None and not game_roots:
        action = "configure_game_root"
    elif action is None and missing_roots:
        action = "fix_missing_game_root"
    sdk_selection = config.get("sdk_selection", {})
    sdk_activation: dict[str, Any] = {"status": "not_checked"}
    if action is None and sdk_selection.get("status") != "selected":
        if not sdk_selection.get("inventory_fingerprint"):
            action = "inventory_sdk_branches"
        else:
            action = "record_target_identity_evaluate_and_select_sdk"
    elif action is None:
        try:
            active = require_sdk_activation(work_dir, config)
            sdk_activation = {
                "status": "active",
                "mode": active.get("mode"),
                "target": active.get("target"),
                "runtime_commit": active.get("runtime", {}).get("commit"),
                "workspace_tools_bound": active.get("workspace_tools") is not None,
            }
        except (GameL10nError, OSError, ValueError, json.JSONDecodeError):
            sdk_activation = {"status": "required"}
            action = "activate_selected_sdk_implementation"
    if action is None:
        if not any((work_dir / "inputs").glob("*")):
            action = "unpack_and_extract_ordered_script"
        else:
            inventory_state_path = work_dir / ".game-l10n" / "inventory-attestation.json"
            inventory_verified = False
            inventory_state, inventory_issue = _read_status_object(
                inventory_state_path,
                label="INVENTORY_ATTESTATION",
                expected_schema="parr0t.inventory-attestation.v1",
            )
            if inventory_issue:
                state_issue_codes.append(inventory_issue)
                action = "repair_invalid_workspace_state"
            elif inventory_state is None:
                action = "verify_independent_source_inventory"
            else:
                inventory_verified = bool(
                    inventory_state.get("status") == "static_verified"
                    and inventory_state.get("static_complete") is True
                    and isinstance(inventory_state.get("primary_fingerprint"), str)
                    and inventory_state.get("primary_fingerprint")
                    and isinstance(inventory_state.get("primary_path"), str)
                    and inventory_state.get("primary_path")
                )
                if not inventory_verified:
                    action = "resolve_source_inventory_gaps"
                else:
                    try:
                        inventory_state = require_inventory_attestation(
                            work_dir, Path(inventory_state["primary_path"])
                        )
                    except (GameL10nError, OSError, ValueError, json.JSONDecodeError):
                        inventory_verified = False
                        state_issue_codes.append("INVENTORY_ATTESTATION_STALE")
                        action = "verify_independent_source_inventory"
            if inventory_verified and action is None:
                failure_queue_path = work_dir / ".game-l10n" / "failure-queue.json"
                queue_pending = False
                queue_blocking_terminal = False
                queue, queue_issue = _read_status_object(
                    failure_queue_path,
                    label="FAILURE_QUEUE",
                    expected_schema=FAILURE_QUEUE_SCHEMA,
                )
                if queue_issue:
                    state_issue_codes.append(queue_issue)
                    action = "repair_invalid_workspace_state"
                elif queue is not None:
                    pending_count = queue.get("pending_count")
                    terminal_count = queue.get("terminal_count", 0)
                    if (
                        not isinstance(pending_count, int)
                        or pending_count < 0
                        or not isinstance(terminal_count, int)
                        or terminal_count < 0
                    ):
                        state_issue_codes.append("FAILURE_QUEUE_COUNT_INVALID")
                        action = "repair_invalid_workspace_state"
                    elif (
                        pending_count + terminal_count > 0
                        and queue.get("inventory_fingerprint")
                        != inventory_state.get("primary_fingerprint")
                    ):
                        state_issue_codes.append("FAILURE_QUEUE_STALE")
                        action = "reconcile_stale_failure_queue"
                    elif not isinstance(queue.get("entries"), list) or len(
                        queue["entries"]
                    ) != pending_count:
                        state_issue_codes.append("FAILURE_QUEUE_ENTRIES_INVALID")
                        action = "repair_invalid_workspace_state"
                    elif not isinstance(queue.get("terminal_entries", []), list) or len(
                        queue.get("terminal_entries", [])
                    ) != terminal_count:
                        state_issue_codes.append("FAILURE_QUEUE_TERMINAL_ENTRIES_INVALID")
                        action = "repair_invalid_workspace_state"
                    else:
                        queue_pending = pending_count > 0
                        queue_blocking_terminal = any(
                            isinstance(item, dict) and item.get("blocking") is True
                            for item in queue.get("terminal_entries", [])
                        )
                if action is None and queue_pending:
                    action = "await_explicit_user_request_to_resume_failure_queue"
                elif action is None and queue_blocking_terminal:
                    action = "resolve_blocking_terminal_unresolved"
                elif action is None:
                    context_path = _path_in_workspace(
                        work_dir,
                        config.get("files", {}).get(
                            "context_pack", "context/context-pack.json"
                        ),
                    )
                    context_state_path = work_dir / ".game-l10n" / "context-state.json"
                    context_state, context_issue = _read_status_object(
                        context_state_path, label="CONTEXT_ATTESTATION"
                    )
                    source_segments_digest = stable_hash(
                        [
                            asdict(item)
                            for item in read_segments(Path(inventory_state["primary_path"]))
                        ]
                    )
                    if context_issue:
                        state_issue_codes.append(context_issue)
                        action = "repair_invalid_workspace_state"
                    elif (
                        context_state is None
                        or not _context_state_valid(
                            context_state, context_path, source_segments_digest
                        )
                    ):
                        action = "analyze_complete_script_context"
                    else:
                        completion_path = (
                            work_dir / ".game-l10n" / "completion-attestation.json"
                        )
                        completion, completion_issue = _read_status_object(
                            completion_path,
                            label="COMPLETION_ATTESTATION",
                            expected_schema=COMPLETION_SCHEMA,
                        )
                        if completion_issue:
                            state_issue_codes.append(completion_issue)
                            action = "repair_invalid_workspace_state"
                        elif completion is None:
                            action = "run_primary_translation_review_pipeline"
                        elif not _completion_state_valid(completion):
                            state_issue_codes.append(
                                "COMPLETION_ATTESTATION_STRUCTURE_INVALID"
                            )
                            action = "repair_invalid_workspace_state"
                        elif completion["source_inventory"].get("fingerprint") != (
                            inventory_state.get("primary_fingerprint")
                        ):
                            state_issue_codes.append("COMPLETION_ATTESTATION_STALE")
                            action = "rebuild_current_inventory_translation_attestation"
                        elif result_issue := _completion_result_issue(work_dir, completion):
                            state_issue_codes.append(result_issue)
                            action = "rebuild_current_inventory_translation_attestation"
                        elif completion.get("static_completion") is not True:
                            action = "resolve_translation_review_gaps"
                        else:
                            local_artifact = cached_tool_state(work_dir, "pack")
                            action = (
                                "local_patch_ready"
                                if local_artifact.get("status") == "valid"
                                else "validate_and_pack_recoverable_patch"
                            )
                            issue_code = local_artifact.get("issue_code")
                            if isinstance(issue_code, str):
                                state_issue_codes.append(issue_code)
    action = action or "run_primary_translation_review_pipeline"
    print(
        json.dumps(
            {
                "workspace": str(work_dir),
                "sdk_root": contract_base,
                "game_roots": [str(path) for path in game_roots],
                "missing_game_roots": missing_roots,
                "repository": visible_policy,
                "provider_preset": config.get("provider_preset"),
                "execution_mode": config.get("execution_mode"),
                "native_workflow": config.get("native_workflow"),
                "sdk_selection": sdk_selection,
                "sdk_activation": sdk_activation,
                "execution": config.get("execution", {}),
                "runtime": config.get("runtime", {"default_policy": "deny"}),
                "control_plane": public_control_plane_state(control_state),
                "local_artifact": local_artifact,
                "state_issue_codes": state_issue_codes,
                "contract_base": contract_base,
                "next_actions": [action],
                "required_contracts": _status_contracts(action),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return (
        1
        if action
        in {
            "fix_missing_game_root",
            "repair_invalid_workspace_state",
            "reconcile_stale_failure_queue",
        }
        else 0
    )


def cmd_sdk_inventory(args: argparse.Namespace) -> int:
    selection = refresh_sdk_inventory(
        Path(args.workspace).resolve(),
        refresh_remote=args.refresh_remote,
        remote=args.remote,
    )
    print(json.dumps(selection, ensure_ascii=False, indent=2))
    return 0


def cmd_sdk_evaluate(args: argparse.Namespace) -> int:
    evaluation = record_sdk_evaluation(
        Path(args.workspace).resolve(),
        args.branch,
        args.result,
        args.reason,
        args.evidence_path,
        target_engine=args.target_engine,
        target_version=args.target_version,
    )
    print(json.dumps({"branch": args.branch, **evaluation}, ensure_ascii=False))
    return 0


def cmd_sdk_select(args: argparse.Namespace) -> int:
    decision = record_sdk_selection(
        Path(args.workspace).resolve(),
        args.mode,
        branch=args.branch,
        target_engine=args.target_engine,
        target_version=args.target_version,
    )
    print(json.dumps(decision, ensure_ascii=False))
    return 0


def cmd_sdk_activate(args: argparse.Namespace) -> int:
    state = record_sdk_activation(
        Path(args.workspace).resolve(),
        reference_checkout=(
            Path(args.reference_checkout).resolve() if args.reference_checkout else None
        ),
    )
    print(
        json.dumps(
            {
                "schema": state["schema"],
                "mode": state["mode"],
                "target": state["target"],
                "runtime_commit": state["runtime"]["commit"],
                "reference_commit": (
                    state["reference"]["commit"] if state.get("reference") else None
                ),
                "workspace_tools_bound": state.get("workspace_tools") is not None,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    export_root = prepare_sdk_profile(
        work_dir,
        args.engine,
        args.version,
        user_confirmed_success=args.user_confirmed_success,
    )
    print(f"Sanitized profile prepared: {export_root}")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    if args.push and not args.commit:
        raise ValueError("--push requires --commit")
    branch = publish_sdk_profile(
        Path(args.workspace).resolve(),
        Path(args.sdk_root).resolve(),
        Path(args.profile).resolve(),
        commit=args.commit,
        push=args.push,
    )
    print(json.dumps({"branch": branch, "committed": args.commit, "pushed": args.push}))
    return 0


def cmd_steam_package(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    result = build_steam_package(
        _path_in_workspace(work_dir, args.payload, must_exist=True),
        _path_in_workspace(work_dir, args.spec, must_exist=True),
        _path_in_workspace(work_dir, args.output),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_verify_steam_package(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    result = validate_steam_package(_path_in_workspace(work_dir, args.package, must_exist=True))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_filesystem_package(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    result = build_filesystem_package(
        _path_in_workspace(work_dir, args.payload, must_exist=True),
        _path_in_workspace(work_dir, args.spec, must_exist=True),
        _path_in_workspace(work_dir, args.output),
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_verify_filesystem_package(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    result = validate_filesystem_package(
        _path_in_workspace(work_dir, args.package, must_exist=True)
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_instance_release_gate(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    result = validate_instance_release(
        _path_in_workspace(work_dir, args.instance_root, must_exist=True),
        _path_in_workspace(work_dir, args.package, must_exist=True),
        user_confirmed_success=args.user_confirmed_success,
        user_approved_publication=args.user_approved_publication,
    )
    gate_path = work_dir / ".game-l10n" / "instance-release-gate.json"
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({**result, "gate_path": str(gate_path)}, ensure_ascii=False))
    return 0


def cmd_inspect_installed_state(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    target = _source_path(work_dir, args.target, config)
    configured_roots = {Path(item).resolve() for item in config.get("game_roots", [])}
    if target not in configured_roots:
        raise ValueError(f"Target is not a configured game root: {target}")
    if not target.is_dir():
        raise ValueError(f"Target is not a directory: {target}")
    result = inspect_installed_state(target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_approve_runtime(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    require_sdk_activation(work_dir, config)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    ttl_minutes = args.ttl_minutes
    if ttl_minutes is None:
        ttl_minutes = int(config.get("runtime", {}).get("approval_ttl_minutes", 30))
    approval_path, approval = create_runtime_approval(
        work_dir,
        action=args.action,
        artifact_path=_path_in_workspace(work_dir, args.artifact, must_exist=True),
        target_path=Path(args.target),
        executable_path=Path(args.executable) if args.executable else None,
        ttl_minutes=ttl_minutes,
        user_confirmed=args.user_confirmed,
    )
    print(
        json.dumps(
            {
                "approval": str(approval_path),
                "action": approval["action"],
                "expires_at_utc": approval["expires_at_utc"],
                "artifact_sha256": approval["artifact"]["sha256"],
                "status": "recorded_not_executed",
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    sdk_root = Path(config.get("sdk_root") or _sdk_root()).resolve()
    require_external_workdir(work_dir, sdk_root)
    issues: list[str] = []
    for provider in config.get("providers", []):
        if provider.get("model") == "CHANGE_ME":
            issues.append(f"provider {provider.get('name')}: model is still CHANGE_ME")
        env_name = provider.get("api_key_env")
        if env_name and not os.environ.get(env_name):
            issues.append(f"provider {provider.get('name')}: environment variable {env_name} is not set")
        if provider.get("type") == "command":
            executable = (provider.get("command") or [""])[0]
            if executable and not shutil.which(executable):
                issues.append(f"provider {provider.get('name')}: command not found: {executable}")
    policy = config.get("repository", {})
    if policy.get("initial_choice") == "pending":
        issues.append("repository: initial Git choice is still pending")
    print(json.dumps({"ok": not issues, "issues": issues}, ensure_ascii=False, indent=2))
    return 1 if issues else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="game-l10n", description="Parr0t game localization agent framework")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-workspace", help="Create a private workspace outside the SDK repository")
    init.add_argument("workspace")
    init.add_argument("--sdk-root")
    init.add_argument(
        "--game-root",
        action="append",
        default=[],
        help="Read-only source game folder; may be provided more than once",
    )
    init.add_argument("--git-choice", choices=["pending", "enabled", "disabled"], default="pending")
    init.add_argument(
        "--provider-preset",
        choices=["openai-deepseek", "codex-app-deepseek", "custom"],
        default="codex-app-deepseek",
        help=(
            "OpenAI/DeepSeek APIs, locked Codex App native workers with DeepSeek fallback, "
            "or a manually configured provider chain"
        ),
    )
    init.set_defaults(func=cmd_init)

    policy = sub.add_parser("policy", help="Record explicit user Git/publish choices")
    policy.add_argument("--workspace", required=True)
    policy.add_argument("--git-choice", choices=["pending", "enabled", "disabled"])
    policy.add_argument("--publish-approved", choices=["yes", "no"])
    policy.set_defaults(func=cmd_policy)

    translate = sub.add_parser("translate", help="Translate and independently review segment data")
    translate.add_argument("--workspace", required=True)
    translate.add_argument("--input", required=True)
    translate.add_argument(
        "--context-input",
        help="Complete ordered source inventory used for context analysis; defaults to --input",
    )
    translate.add_argument(
        "--context-mode",
        choices=[mode.value for mode in ContextMode],
        help="Override project.context_mode for this run",
    )
    translate.add_argument("--output", required=True)
    translate.add_argument("--force", action="store_true")
    translate.add_argument("--force-context", action="store_true")
    translate.set_defaults(func=cmd_translate)

    review_existing = sub.add_parser(
        "review-existing",
        help="Independently re-review every reused candidate against the current full context",
    )
    review_existing.add_argument("--workspace", required=True)
    review_existing.add_argument("--input", required=True, help="Current ordered source targets")
    review_existing.add_argument("--existing", required=True, help="Prior result artifact")
    review_existing.add_argument(
        "--context-input",
        help="Verified complete ordered inventory; defaults to --input",
    )
    review_existing.add_argument(
        "--context-mode",
        choices=[mode.value for mode in ContextMode],
        help="Override project.context_mode for this run",
    )
    review_existing.add_argument("--output", required=True)
    review_existing.add_argument("--force-context", action="store_true")
    review_existing.set_defaults(func=cmd_review_existing)

    resume = sub.add_parser(
        "resume-failures",
        help="Retry the metadata-only failure queue after a new explicit user request",
    )
    resume.add_argument("--workspace", required=True)
    resume.add_argument("--input", required=True, help="Current ordered source targets")
    resume.add_argument(
        "--context-input",
        help="Verified complete ordered inventory; defaults to --input",
    )
    resume.add_argument(
        "--context-mode",
        choices=[mode.value for mode in ContextMode],
        help="Override project.context_mode for this run",
    )
    resume.add_argument("--output", required=True)
    resume.add_argument("--force-context", action="store_true")
    resume.add_argument(
        "--user-requested",
        action="store_true",
        required=True,
        help="Assert that the user explicitly requested resuming the saved failure queue",
    )
    resume.set_defaults(func=cmd_resume_failures)

    correction = sub.add_parser(
        "apply-user-correction",
        help="Apply exact user-specified target wording without model calls or broad re-review",
    )
    correction.add_argument("--workspace", required=True)
    correction.add_argument("--input", required=True, help="Verified current source inventory")
    correction.add_argument(
        "--existing",
        help="Released full result artifact; omit to materialize candidates from the checkpoint",
    )
    correction.add_argument("--checkpoint", required=True, help="Matching release checkpoint")
    correction.add_argument("--request", required=True, help="Private exact-correction request JSON")
    correction.add_argument("--output", required=True, help="New corrected full result artifact")
    correction.add_argument(
        "--user-requested",
        action="store_true",
        required=True,
        help="Assert that the user explicitly supplied or approved the exact replacement wording",
    )
    correction.set_defaults(func=cmd_apply_user_correction)

    inventory = sub.add_parser(
        "verify-inventory",
        help="Cross-check two independently produced complete source inventories",
    )
    inventory.add_argument("--workspace", required=True)
    inventory.add_argument("--primary", required=True)
    inventory.add_argument("--independent", required=True)
    inventory.set_defaults(func=cmd_verify_inventory)

    runtime_coverage = sub.add_parser(
        "record-runtime-coverage",
        help="Validate metadata-only runtime observations without launching the game",
    )
    runtime_coverage.add_argument("--workspace", required=True)
    runtime_coverage.add_argument("--report", required=True)
    runtime_coverage.set_defaults(func=cmd_record_runtime_coverage)

    context = sub.add_parser("analyze-context", help="Analyze complete source context before translation")
    context.add_argument("--workspace", required=True)
    context.add_argument("--input", required=True)
    context.add_argument(
        "--context-mode",
        choices=[mode.value for mode in ContextMode],
        help="Override project.context_mode for this run",
    )
    context.add_argument("--force", action="store_true")
    context.set_defaults(func=cmd_analyze_context)

    tool = sub.add_parser("run-tool", help="Run cached unpack/pack tooling")
    tool.add_argument("stage", choices=["unpack", "pack"])
    tool.add_argument("--workspace", required=True)
    tool.add_argument("--input", required=True)
    tool.add_argument("--output", required=True)
    tool.add_argument("--force", action="store_true")
    tool.set_defaults(func=cmd_run_tool)

    native_prepare = sub.add_parser(
        "prepare-native-pass",
        help="Create cached exclusive target-ID task files for native workers",
    )
    native_prepare.add_argument("--workspace", required=True)
    native_prepare.add_argument("--input", required=True, help="Ordered source segment file")
    native_prepare.add_argument("--output", required=True, help="Private task directory")
    native_prepare.add_argument("--max-records", type=int)
    native_prepare.add_argument(
        "--max-material-tokens",
        type=int,
        help="Estimated private input-material cap per fresh native worker task",
    )
    native_prepare.set_defaults(func=cmd_prepare_native_pass)

    native_import = sub.add_parser(
        "import-native-shard",
        help="Import one exclusive native-agent result shard into the private workspace",
    )
    native_import.add_argument("--workspace", required=True)
    native_import.add_argument("--input", required=True)
    native_import.add_argument("--name", required=True)
    native_import.set_defaults(func=cmd_import_native_shard)

    native_merge = sub.add_parser(
        "merge-native-pass",
        help="Merge exclusive native-agent shards in stable source order",
    )
    native_merge.add_argument("--workspace", required=True)
    native_merge.add_argument("--input", required=True, help="Ordered source segment file")
    native_merge.add_argument("--output", required=True)
    native_merge.set_defaults(func=cmd_merge_native_pass)

    exchange_prepare = sub.add_parser(
        "prepare-translation-exchange",
        help="Create the fixed private manual/fallback translation exchange bundle",
    )
    exchange_prepare.add_argument("--workspace", required=True)
    exchange_prepare.add_argument("--input", required=True, help="Ordered source segment file")
    exchange_prepare.add_argument("--output", required=True, help="New private exchange directory")
    exchange_prepare.add_argument("--exchange-id", required=True)
    exchange_prepare.add_argument("--mode", choices=["manual", "fallback"], required=True)
    exchange_prepare.add_argument(
        "--source-queue",
        choices=["primary_pending", "translation_fallback_pending"],
        required=True,
    )
    exchange_prepare.set_defaults(func=cmd_prepare_translation_exchange)

    exchange_seal = sub.add_parser(
        "seal-translation-exchange",
        help="Validate a fixed exchange and emit only its metadata-only receipt and control envelope",
    )
    exchange_seal.add_argument("--workspace", required=True)
    exchange_seal.add_argument("--bundle", required=True)
    exchange_seal.set_defaults(func=cmd_seal_translation_exchange)

    control_validate = sub.add_parser(
        "validate-control-envelope",
        help="Validate and sanitize a data-plane worker result before the parent uses it",
    )
    control_validate.add_argument("--workspace", required=True)
    control_validate.add_argument("--input", required=True)
    control_validate.add_argument("--expected-role", choices=sorted(WORKER_ROLES))
    control_validate.add_argument(
        "--expected-stage",
        choices=sorted({stage for stages in ROLE_STAGES.values() for stage in stages}),
    )
    control_validate.set_defaults(func=cmd_validate_control_envelope)

    control_status = sub.add_parser(
        "control-plane-status",
        help="Report the text-free control-plane generation and rotation state",
    )
    control_status.add_argument("--workspace", required=True)
    control_status.set_defaults(func=cmd_control_plane_status)

    control_leak = sub.add_parser(
        "record-control-plane-leak",
        help="Quarantine a top-level agent after data-plane or semantic content crossed the boundary",
    )
    control_leak.add_argument("--workspace", required=True)
    control_leak.add_argument("--incident-code", choices=sorted(LEAK_INCIDENT_CODES), required=True)
    control_leak.set_defaults(func=cmd_record_control_plane_leak)

    control_resume = sub.add_parser(
        "resume-control-plane",
        help="Acknowledge a recorded incident from a fresh metadata-only control plane",
    )
    control_resume.add_argument("--workspace", required=True)
    control_resume.add_argument(
        "--fresh-session-confirmed",
        action="store_true",
        help="Assert this is a newly started metadata-only control plane",
    )
    control_resume.set_defaults(func=cmd_resume_control_plane)

    sdk_inventory = sub.add_parser(
        "sdk-inventory",
        help="Refresh the read-only inventory of existing sdk-* branches",
    )
    sdk_inventory.add_argument("--workspace", required=True)
    sdk_inventory.add_argument(
        "--refresh-remote",
        action="store_true",
        help="Fetch only sdk-* tracking refs before inventorying them",
    )
    sdk_inventory.add_argument("--remote", default="origin")
    sdk_inventory.set_defaults(func=cmd_sdk_inventory)

    sdk_evaluate = sub.add_parser(
        "sdk-evaluate",
        help="Record a compatibility evaluation for one inventoried SDK branch",
    )
    sdk_evaluate.add_argument("--workspace", required=True)
    sdk_evaluate.add_argument("--branch", required=True)
    sdk_evaluate.add_argument("--target-engine", required=True)
    sdk_evaluate.add_argument(
        "--target-version",
        required=True,
        help="Target SDK compatibility line used in sdk-<engine>-<version>",
    )
    sdk_evaluate.add_argument(
        "--result",
        choices=["compatible", "partial", "reference", "incompatible"],
        required=True,
    )
    sdk_evaluate.add_argument(
        "--reason",
        choices=[
            "public_contract_match",
            "reusable_with_extension",
            "reusable_for_new_identity",
            "engine_mismatch",
            "format_mismatch",
            "version_incompatible",
            "missing_required_capability",
            "uninspectable_contract",
        ],
        required=True,
    )
    sdk_evaluate.add_argument(
        "--evidence-path",
        action="append",
        default=[],
        help="Inspected path from the candidate's public_contract_paths; repeat when needed",
    )
    sdk_evaluate.set_defaults(func=cmd_sdk_evaluate)

    sdk_select = sub.add_parser(
        "sdk-select",
        help="Select reuse, extension, derived new identity, or a private new adapter after evaluation",
    )
    sdk_select.add_argument("--workspace", required=True)
    sdk_select.add_argument(
        "--mode", choices=["reuse", "extend", "derive-new", "private-new"], required=True
    )
    sdk_select.add_argument("--branch")
    sdk_select.add_argument("--target-engine", required=True)
    sdk_select.add_argument(
        "--target-version",
        required=True,
        help="Target SDK compatibility line used in sdk-<engine>-<version>",
    )
    sdk_select.set_defaults(func=cmd_sdk_select)

    sdk_activate = sub.add_parser(
        "sdk-activate",
        help="Bind the selected SDK decision to the implementation that will execute it",
    )
    sdk_activate.add_argument("--workspace", required=True)
    sdk_activate.add_argument(
        "--reference-checkout",
        help="Exact clean reference checkout required only for derive-new",
    )
    sdk_activate.set_defaults(func=cmd_sdk_activate)

    status = sub.add_parser("status", help="Report the next safe action for a Codex orchestrator")
    status.add_argument("--workspace", required=True)
    status.set_defaults(func=cmd_status)

    promote = sub.add_parser("promote-sdk", help="Prepare a game-data-free engine SDK profile")
    promote.add_argument("--workspace", required=True)
    promote.add_argument("--engine", required=True)
    promote.add_argument("--version", required=True)
    promote.add_argument("--user-confirmed-success", action="store_true", required=True)
    promote.set_defaults(func=cmd_promote)

    publish = sub.add_parser("publish-sdk", help="Create an approved sdk-<engine>-<version> branch")
    publish.add_argument("--workspace", required=True)
    publish.add_argument("--sdk-root", required=True)
    publish.add_argument("--profile", required=True)
    publish.add_argument("--commit", action="store_true")
    publish.add_argument("--push", action="store_true")
    publish.set_defaults(func=cmd_publish)

    steam_package = sub.add_parser(
        "steam-package",
        help="Build a minimal Steam-aware install package from a private workspace payload",
    )
    steam_package.add_argument("--workspace", required=True)
    steam_package.add_argument("--payload", required=True, help="Patch payload directory inside the workspace")
    steam_package.add_argument("--spec", required=True, help="Steam package JSON spec inside the workspace")
    steam_package.add_argument("--output", required=True, help="New ZIP path inside the workspace")
    steam_package.set_defaults(func=cmd_steam_package)

    verify_steam = sub.add_parser(
        "verify-steam-package",
        help="Verify a Parr0t Steam package without installing it",
    )
    verify_steam.add_argument("--workspace", required=True)
    verify_steam.add_argument("--package", required=True)
    verify_steam.set_defaults(func=cmd_verify_steam_package)

    filesystem_package = sub.add_parser(
        "filesystem-package",
        help="Build a launcher-neutral package that asks for an explicit game folder",
    )
    filesystem_package.add_argument("--workspace", required=True)
    filesystem_package.add_argument("--payload", required=True)
    filesystem_package.add_argument("--spec", required=True)
    filesystem_package.add_argument("--output", required=True)
    filesystem_package.set_defaults(func=cmd_filesystem_package)

    verify_filesystem = sub.add_parser(
        "verify-filesystem-package",
        help="Verify a launcher-neutral Parr0t package without installing it",
    )
    verify_filesystem.add_argument("--workspace", required=True)
    verify_filesystem.add_argument("--package", required=True)
    verify_filesystem.set_defaults(func=cmd_verify_filesystem_package)

    instance_release = sub.add_parser(
        "instance-release-gate",
        help="Validate a committed per-game instance before any push, tag, release, or upload",
    )
    instance_release.add_argument("--workspace", required=True)
    instance_release.add_argument(
        "--instance-root", required=True, help="Committed instance repository inside the workspace"
    )
    instance_release.add_argument(
        "--package", required=True, help="Exact verified Release asset outside the instance repository"
    )
    instance_release.add_argument("--user-confirmed-success", action="store_true", required=True)
    instance_release.add_argument("--user-approved-publication", action="store_true", required=True)
    instance_release.set_defaults(func=cmd_instance_release_gate)

    inspect_installed = sub.add_parser(
        "inspect-installed-state",
        help="Classify a configured game root from Parr0t receipt hashes without changing it",
    )
    inspect_installed.add_argument("--workspace", required=True)
    inspect_installed.add_argument("--target", required=True, help="Exact configured game root")
    inspect_installed.set_defaults(func=cmd_inspect_installed_state)

    approve_runtime = sub.add_parser(
        "approve-runtime",
        help="Record one exact user-approved real install, update, remove, or launch action",
    )
    approve_runtime.add_argument("--workspace", required=True)
    approve_runtime.add_argument(
        "--action", choices=["install", "update", "remove", "launch"], required=True
    )
    approve_runtime.add_argument(
        "--artifact", required=True, help="Exact patch artifact inside the workspace"
    )
    approve_runtime.add_argument("--target", required=True, help="Exact configured game root")
    approve_runtime.add_argument(
        "--executable", help="Exact executable inside the game root; required only for launch"
    )
    approve_runtime.add_argument("--ttl-minutes", type=int)
    approve_runtime.add_argument(
        "--user-confirmed",
        action="store_true",
        required=True,
        help="Assert that the user explicitly requested this exact external action",
    )
    approve_runtime.set_defaults(func=cmd_approve_runtime)

    doctor = sub.add_parser("doctor", help="Check workspace configuration without sending game data")
    doctor.add_argument("--workspace", required=True)
    doctor.set_defaults(func=cmd_doctor)
    register_wolf_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        recovery_commands = {
            cmd_init,
            cmd_status,
            cmd_doctor,
            cmd_control_plane_status,
            cmd_record_control_plane_leak,
            cmd_resume_control_plane,
        }
        if args.func not in recovery_commands:
            workspace = getattr(args, "workspace", None)
            if workspace is not None:
                require_control_plane_ready(Path(workspace).resolve())
        return int(args.func(args))
    except ToolExecutionError:
        print("error: E_TOOL_EXECUTION_FAILED", file=sys.stderr)
        return 2
    except (GameL10nError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
