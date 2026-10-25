from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

from .control_plane import (
    CONTROL_ENTRY_PATH,
    CONTROL_STATE_PATH,
    initial_control_plane_entry_state,
    initial_control_plane_state,
)
from .errors import ConfigurationError
from .language_policy import (
    LANGUAGE_POLICY_PATH,
    LANGUAGE_POLICY_SCHEMA,
    require_target_language_policy,
)
from .privacy import require_external_workdir, scan_tools
from .provider_policy import (
    CODEX_APP_EXTERNAL_ROLES,
    CODEX_APP_EXECUTION_MODE,
    CODEX_APP_PRESET,
    CONTROL_OBSERVATION_POLICY,
    MODE_LOCK_PATH,
    provider_mode_lock,
    validate_provider_policy,
)


CONFIG_TEMPLATE: dict[str, Any] = {
    "provider_preset": "openai-deepseek",
    "execution_mode": "api",
    "provider_policy": {
        "selected_preset": "openai-deepseek",
        "key_presence_selects_mode": False,
    },
    "project": {
        "game_title": "CHANGE_ME",
        "source_language": "ja",
        "target_language": "ko",
        "genre": "",
        "world": "",
        "style_guide": "",
        "age_rating": "",
        "content_notes": [],
        "context_mode": "auto",
    },
    "providers": [
        {
            "name": "openai-luna",
            "type": "openai_responses",
            "model": "gpt-6-luna",
            "api_key_env": "OPENAI_API_KEY",
            "reasoning_effort": "none",
            "max_output_tokens": 2048,
            "task_max_output_tokens": {
                "game_translation_review_judge": 256
            },
        },
        {
            "name": "deepseek",
            "type": "compatible_chat",
            "model": "deepseek-flash",
            "base_url": "https://api.deepseek.com",
            "api_key_env": "DEEPSEEK_API_KEY",
            "response_format": "json_object",
            "thinking": "disabled",
            "reasoning_effort": "none",
            "max_tokens_per_source_char": 4.0,
            "min_output_tokens": 128,
            "max_output_tokens": 8192,
            "review_verdict_format": "plain",
            "task_max_output_tokens": {"game_translation_review_judge": 24},
            "task_max_input_chars": {
                "game_translation": 24000,
                "game_translation_review_judge": 24000,
                "game_translation_review_rewrite": 24000
            },
        },
        {
            "name": "openai-sol",
            "type": "openai_responses",
            "model": "gpt-6.1-sol",
            "api_key_env": "OPENAI_API_KEY",
            "reasoning_effort": "low",
            "max_output_tokens": 8192,
            "task_max_output_tokens": {
                "game_translation_review_judge": 512,
                "game_translation_review_rewrite": 2048,
            },
        },
    ],
    "routing": {
        "context_analyzers": ["openai-sol", "deepseek"],
        "translators": ["openai-luna", "openai-sol", "deepseek"],
        "translator_retries": {
            "openai-luna": 5,
            "openai-sol": 3,
        },
        "review_triagers": ["openai-luna"],
        "reviewers": ["openai-sol", "deepseek"],
        "rewriters": ["deepseek", "openai-sol"],
        "strong_review_sample_percent": 5.0,
        "strong_review_high_risk_categories": [
            "allusion",
            "critical_logic",
            "parody",
            "quest_title",
            "script_sensitive",
            "wordplay"
        ],
        "require_review": True,
        "review_max_rounds": 2,
        "max_workers": 8,
        "retry_refusals_once": False,
        "grace_retries": 3,
        "grace_retry_codes": [
            "malformed_response",
            "provider_error",
            "rate_limited",
            "timeout",
            "integrity_invalid"
        ],
        "defer_fallbacks_until_primary_complete": True,
        "preserve_source_on_failure": True,
        "review_refusal_policy": "fallback",
    },
    "files": {
        "glossary": "context/glossary.json",
        "characters": "context/characters.json",
        "context_pack": "context/context-pack.json"
    },
    "tools": {
        "unpack": ["python", "tools/unpack.py", "{input}", "{output}"],
        "pack": ["python", "tools/pack.py", "{input}", "{output}"],
    },
    "repository": {"initial_choice": "pending"},
    "sdk_selection": {
        "status": "pending",
        "inventory_fingerprint": "",
        "candidates": [],
        "evaluations": {},
        "decision": None,
    },
    "execution": {
        "scope_mode": "minimum_requested_artifact",
        "validation_profile": "proportional",
        "same_failure_limit": 2,
        "reuse_unchanged_checks": True,
        "delegate_simple_tasks": False,
        "native_shard_policy_version": 5,
        "native_shard_records": 64,
        "native_shard_min_records": 8,
        "native_shard_material_tokens": 8000,
        "native_review_shard_records": 2048,
        "native_review_shard_min_records": 64,
        "native_review_shard_material_tokens": 262144,
        "large_inventory_threshold_records": 10000,
        "startup_full_inventory_scan_limit": 2,
        "startup_per_record_artifacts_allowed": False,
        "startup_attestation_fast_path": True,
        "native_worker_history": "none",
        "native_worker_reuse": False,
        "native_task_payload_schema": "parr0t.native-task-payload.v1",
        "native_full_source_access": "validation_only",
        "native_agent_max_depth": 1,
        "model_controller_allowed": False,
        "worker_delegation_allowed": False,
        "control_turn_budget_policy_version": 2,
        "control_turn_budget_user_approved": False,
        "max_root_model_calls_per_wave": None,
        "max_root_model_calls_per_turn": None,
        "max_root_input_tokens_per_turn": None,
        "max_root_progress_messages_per_wave": 1,
        "max_worker_model_calls_per_task": 16,
        "max_semantic_model_calls_per_task": 1,
        "per_record_semantic_call_loop_allowed": False,
        "detail_contract_schema": "parr0t.detail-contract.v1",
        "free_form_execution_contract_allowed": False,
        "bulk_budget_gate_records": 4096,
        "bulk_max_elapsed_minutes": None,
        "bulk_max_input_tokens": None,
        "bulk_max_provider_cost_usd": None,
        "bulk_provider_cost_unmetered": False,
        "bulk_budget_user_approved": False,
        "budget_exhaustion_action": "continue_until_terminal_or_actionable_blocker",
        "contract_reload_policy": "digest_change_or_new_control_generation_only",
        "residual_direct_records": 32,
        "job_local_controller_allowed": False,
        "automatic_capacity_restart_allowed": False,
        "max_unchanged_progress_observations": 1,
        "max_control_actions_without_progress": 2,
        "aggregate_once_per_wave": True,
        "progress_report_mode": "wave_boundary_or_blocker_only",
    },
    "control_plane": {
        "envelope_schema": "parr0t.control-envelope.v2",
        "translation_exchange_schema": "parr0t.translation-exchange/v1",
        "metadata_only_for_all_data_plane_stages": True,
        "reject_free_form_worker_returns": True,
        "reject_ad_hoc_exchange_formats": True,
        "rotate_after_leak": True,
        **CONTROL_OBSERVATION_POLICY,
    },
    "image_localization": {
        "default_policy": "disabled",
        "explicit_user_request_required": True,
        "definite_player_visible_ui_binding_required": True,
        "recursive_ocr_allowed": False,
    },
    "coverage": {
        "require_independent_inventory": True,
        "separate_static_and_runtime_completion": True,
        "runtime_execution_requires_user_approval": True,
    },
    "failure_queue": {
        "resume_only_after_user_request": True,
        "hold_patch_when_blocking_target_fails": True,
    },
    "release_checkpoint": {
        "required_for_distribution": True,
        "reuse_verified_unchanged_targets": True,
        "selective_user_corrections": True,
        "locale_fallback": "best_effort_declared",
    },
    "instance_release": {
        "dormant_until_user_publication_command": True,
        "package_validation_is_not_release_readiness": True,
        "gate_required_before_external_publication": True,
        "metadata_file": "parr0t-instance-release.json",
        "asset_location": "github_release_only",
    },
    "runtime": {
        "default_policy": "deny",
        "approval_ttl_minutes": 30,
        "separate_install_and_launch": True,
        "user_executes_by_default": True,
    },
}

AGENTS_TEXT = """# AGENTS.md — Game localization workspace

This private workspace contains game data. Resolve Parr0t from `game-l10n.json`; keep game roots read-only and generated artifacts here.

## First action

1. Before listing, opening, searching, or reading any file inside a configured game root, read the repository `AGENTS.md` and `docs/agent-usage.md`.
2. Run `game-l10n status --workspace <this-directory>` and perform only its single `next_actions` entry.
3. When status requires `establish_text_free_control_plane`, read every returned contract, then run `game-l10n enter-control-plane --workspace <this-directory> --contracts-read --source-unopened`. If the parent already received or inspected game content, record a leak and stop instead.
4. Resolve later `required_contracts` against `contract_base`; reuse unchanged checkpoints. Stop local work only at `local_patch_ready`, which requires a current completion attestation and its unchanged packed artifact.

## Core contract

- Apply the repository `skills/request-scope-guard/SKILL.md` after every scope-changing user message and before optional or external effects. Git, provider, installation, launch, and publication permissions are separate; never infer one from access or another approval.
- Do not inspect images for text or run OCR unless the user requests image translation. Then record `image-localization-policy`, read `docs/image-localization.md`, and process only definitively bound player-visible UI text; never scan all images.
- Select and activate the exact engine/version SDK before data-plane work. A different SDK may be a bound `reference` for `derive-new`; generated pass-through tools are not an adapter.
- Require two independent complete ordered inventories, typed context analysis, primary translation, translation fallback for failures only, complete low-cost triage, selective strong review for flags/risk/sample, flagged-only rewrite, review fallback for failures only, deterministic validation, then packing. Do not shrink scope or reorder barriers.
- The top-level agent is text-free. It may observe only core CLI metadata, validated `parr0t.control-envelope.v2`/receipt paths, and direct-relay categorical lifecycle. Never inspect session transcripts/logs, agent trees, nested-worker status/output, or semantic-worker logs. Usage comes only from core ledgers/receipts. Run `game-l10n validate-control-envelope`; forbidden detail requires rotation.
- Existing-locale discovery, inventory, gap reconciliation, and context analysis use only the relay from `prepare-data-plane-task`; seal it with `seal-data-plane-task`. The parent never assigns a data worker directly, opens game content, calls a direct native-mode provider, or requests an exception. Retry or repair non-success through the next status action even when a partial artifact exists.
- If private text or semantic/refusal prose reaches the parent, run `game-l10n record-control-plane-leak` and stop. The contaminated agent cannot resume. A fresh control plane uses only the canonical receipt with `--fresh-session-confirmed --contracts-read --source-unopened`; prose handoffs are invalid.
- Normalize every refusal to `fallback_required` / “폴백 처리 필요”. It is a routing event, never an exclusion, batch cancellation, or content decision. `source_preserved` requires route evidence that every configured route was attempted and exhausted.
- Use the locked target language (default: Korean). Another language requires `target-language-policy --user-requested`. Preserve relevant context, meaning, function, natural tone, voice, terminology, allusions, placeholders, markup, control codes, conditions, units, and order.
- Provider roles are fixed by the selected preset. An API key does not select or alter a mode. Fresh native workers use no parent history and return metadata only. Manual/fallback handoff uses only `parr0t.translation-exchange/v1`.
- Send external fallback requests only through the configured provider adapter. Do not create a job-local API loop, embed a context pack inside segment metadata, or resend an unchanged successful target. The adapter enforces focused per-target input budgets; `budget_exhausted` queues that exact target without a network call or scope reduction.
- Build and validate recoverable output here. Never mutate or launch the real game during automated validation. Real install, update, remove/restore, and launch each require their own explicit one-shot authorization.
- Retryable failures stay in the metadata-only failure queue. Terminal source-preserved outcomes are recorded separately and are not retried as pending work; `metadata.blocking_on_failure=true` holds the entire patch.
- Keep publication dormant and unmentioned until the user requests it. SDK promotion also requires confirmed patch success and the original exact target identity. Its separate engine-only commit must pass `game-l10n verify-sdk-promotion`; never mix a `main`-owned path into that commit.
- Native orchestration uses one disposable isolated relay per authorized wave. Give it only the `native-pass-step` artifact; it cannot inspect payloads, plan, retry, split, route, or forward raw responses. Wait once on that direct relay, then step. Reload prose only when `status.contract_set.digest` or the control generation changes.
- V7 tasks embed bound segments; never open the full source. Review starts at 256 records/65,536 estimated tokens or less (hard ceilings: 2,048/262,144). One semantic call ends the worker; core locks successes and emits smaller failed-only manifests.
- Use proportional checks and stop after the required layer passes. For large inventory, scan once per method and continue bounded cached gap work while the core gap score improves; incomplete inventory is not terminal. Once a semantic pass leaves no more than `execution.residual_direct_records`, use one fresh worker on one cached residual task; do not create a job-local scheduler or supplemental evidence pack. A native capacity failure enters resumable state and must not auto-restart the root turn. Aggregate once per wave, report progress only at a wave boundary or blocker, and stop only after the configured unchanged limit.
- Only user-set turn limits recorded in the manifest apply; never infer them.

## Status routes

- SDK choice: `docs/sdk-selection.md`; SDK promotion: `docs/sdk-branch-contract.md` and `docs/engine-faq-contract.md`.
- Inventory/review: `docs/coverage-gates.md` and `docs/review-pipeline.md`.
- Control plane/native workers: `docs/control-plane-contract.md`, `docs/execution-budget.md`, and `docs/detail-contract.md`.
- Manual/API handoff: `docs/translation-exchange.md`.
- Existing release or game update: `docs/release-checkpoint.md`; exact approved wording fix: `docs/minimal-corrections.md`; composed UI: `docs/segment-metadata.md`; requested UI images: `docs/image-localization.md`.
- Runtime mutation: `docs/runtime-approval.md` only after the exact user request.
- Windows package: `docs/installer-contract.md` plus `docs/filesystem-packaging.md` or `docs/steam-packaging.md`.
- Steam integration: `docs/steam-faq.md` and `docs/wrapper-executable.md` only when requested.
- Per-game publication: `docs/instance-release.md` only after the user commands publication.

For launcher-neutral packages, `-GameRoot` accepts absolute and startup-current-directory-relative paths; when omitted, the validated startup current directory is tried before prompting. Do not interact with a store client.

## Data boundary

- Private here: `game-data/`, `inputs/`, `translations/`, `batches/`, `audit/`, `.game-l10n/`, and `logs/`.
- Never copy private data, text, translations, prompts, logs, credentials, paths, counts, hashes, or job provenance into the SDK repository or a distribution.
- API keys remain in environment variables. Final parent reports contain metadata only.
"""

GITIGNORE_TEXT = """# Private game and localization data
game-data/
inputs/
translations/
localized/
batches/
audit/
build/
logs/
.game-l10n/
sdk-export/
.env
*.key
*.pem
"""

PASS_THROUGH_TOOL = '''from __future__ import annotations

import shutil
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {Path(sys.argv[0]).name} INPUT OUTPUT", file=sys.stderr)
        return 2
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    if not source.exists():
        print(f"input does not exist: {source}", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

SDK_ACTIVATION_PATH = ".game-l10n/sdk-activation.json"
SDK_ACTIVATION_SCHEMA = "parr0t.sdk-activation.v2"

PORTABLE_RUNTIME_IGNORED_DIRS = {
    ".game-l10n",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "venv",
}
PORTABLE_RUNTIME_IGNORED_SUFFIXES = {".pyc", ".pyo"}


SDK_EVALUATION_REASONS = {
    "compatible": {"public_contract_match"},
    "partial": {"reusable_with_extension"},
    "reference": {"reusable_for_new_identity"},
    "incompatible": {
        "engine_mismatch",
        "format_mismatch",
        "version_incompatible",
        "missing_required_capability",
        "uninspectable_contract",
    },
}


def _sdk_target(engine: str, version: str) -> dict[str, str]:
    safe_engine = "-".join(
        part for part in engine.lower().replace("_", "-").split("-") if part.isalnum()
    )
    safe_version = "-".join(
        part
        for part in version.lower().replace("_", "-").split("-")
        if part.replace(".", "").isalnum()
    )
    if not safe_engine or not safe_version:
        raise ConfigurationError("target engine and version must form a safe SDK branch name")
    return {
        "engine": safe_engine,
        "version": safe_version,
        "branch": f"sdk-{safe_engine}-{safe_version}",
    }


def _run_git(sdk_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=sdk_root,
        text=True,
        encoding="utf-8",
        capture_output=True,
        shell=False,
    )
    if completed.returncode != 0:
        raise ConfigurationError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout


def _content_fingerprint(entries: list[dict[str, Any]]) -> str:
    return _json_fingerprint(sorted(entries, key=lambda item: item["path"]))


def _git_tree_content_binding(sdk_root: Path, ref: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=sdk_root,
        capture_output=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ConfigurationError(f"Unable to fingerprint SDK tree {ref}: {error}")
    entries: list[dict[str, Any]] = []
    with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if member.isfile():
                source = archive.extractfile(member)
                if source is None:
                    raise ConfigurationError(
                        f"Unable to read SDK tree member while fingerprinting: {member.name}"
                    )
                data = source.read()
                kind = "file"
            elif member.issym():
                data = member.linkname.encode("utf-8")
                kind = "symlink"
            else:
                continue
            entries.append(
                {
                    "path": member.name.replace("\\", "/"),
                    "kind": kind,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return {
        "content_fingerprint": _content_fingerprint(entries),
        "file_count": len(entries),
    }


def _portable_runtime_content_binding(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ConfigurationError(f"SDK runtime directory does not exist: {root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(
            part in PORTABLE_RUNTIME_IGNORED_DIRS or part.endswith(".egg-info")
            for part in relative.parts
        ):
            continue
        if path.suffix.lower() in PORTABLE_RUNTIME_IGNORED_SUFFIXES:
            continue
        if path.is_symlink():
            data = os.readlink(path).encode("utf-8")
            kind = "symlink"
        elif path.is_file():
            data = path.read_bytes()
            kind = "file"
        else:
            continue
        entries.append(
            {
                "path": relative.as_posix(),
                "kind": kind,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    if not entries:
        raise ConfigurationError(f"SDK runtime directory contains no bindable files: {root}")
    return {
        "content_fingerprint": _content_fingerprint(entries),
        "file_count": len(entries),
    }


def _validate_portable_sdk_identity(branch: str, commit: str) -> None:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    if not branch.startswith("sdk-") or any(
        character not in allowed for character in branch
    ):
        raise ConfigurationError("Portable SDK branch must be a safe sdk-* branch name")
    if len(commit) not in {40, 64} or any(
        character not in "0123456789abcdefABCDEF" for character in commit
    ):
        raise ConfigurationError("Portable SDK commit must be a full hexadecimal commit ID")


def _portable_public_contract_paths(root: Path) -> list[str]:
    prefixes = ("docs/", "profiles/", "src/game_l10n/", "tests/")
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix().startswith(prefixes)
    )


def _looks_like_portable_parr0t_runtime(root: Path) -> bool:
    return (root / "AGENTS.md").is_file() and (
        (root / "src" / "game_l10n" / "workspace.py").is_file()
        or (root / "pyproject.toml").is_file()
    )


def inventory_sdk_branches(
    sdk_root: Path,
    *,
    portable_branch: str | None = None,
    portable_commit: str | None = None,
) -> dict[str, Any]:
    """Return an engine-branch inventory without checking out or changing any ref."""

    sdk_root = sdk_root.resolve()
    if not (sdk_root / ".git").exists():
        if bool(portable_branch) != bool(portable_commit):
            raise ConfigurationError(
                "Portable SDK inventory requires both branch and full commit ID"
            )
        if portable_branch and portable_commit:
            _validate_portable_sdk_identity(portable_branch, portable_commit)
            binding = _portable_runtime_content_binding(sdk_root)
            candidates: list[dict[str, Any]] = [
                {
                    "branch": portable_branch,
                    "commit": portable_commit.lower(),
                    "inspect_ref": f"portable:{portable_branch}",
                    "refs": [
                        {
                            "name": f"portable:{portable_branch}",
                            "commit": portable_commit.lower(),
                        }
                    ],
                    "public_contract_paths": _portable_public_contract_paths(sdk_root),
                    **binding,
                    "binding_kind": "content",
                }
            ]
            source = "portable_sdk_snapshot"
        else:
            candidates = []
            source = (
                "portable_unidentified"
                if _looks_like_portable_parr0t_runtime(sdk_root)
                else "filesystem_without_sdk_refs"
            )
    else:
        if portable_branch or portable_commit:
            raise ConfigurationError(
                "Portable SDK identity options cannot be combined with a Git checkout"
            )
        output = _run_git(
            sdk_root,
            "for-each-ref",
            "--format=%(refname)%09%(objectname)",
            "refs/heads/sdk-*",
            "refs/remotes/*/sdk-*",
        )
        grouped: dict[str, list[tuple[str, str]]] = {}
        for line in output.splitlines():
            if not line.strip():
                continue
            ref, commit = line.split("\t", 1)
            if ref.startswith("refs/heads/"):
                branch = ref.removeprefix("refs/heads/")
            else:
                remainder = ref.removeprefix("refs/remotes/")
                _, separator, branch = remainder.partition("/")
                if not separator:
                    continue
            if not branch.startswith("sdk-") or branch.endswith("/HEAD"):
                continue
            grouped.setdefault(branch, []).append((ref, commit))

        candidates = []
        for branch in sorted(grouped):
            refs = sorted(
                grouped[branch],
                key=lambda item: (
                    0
                    if item[0].startswith("refs/remotes/origin/")
                    else 1
                    if item[0].startswith("refs/remotes/")
                    else 2,
                    item[0],
                ),
            )
            selected_ref, commit = refs[0]
            binding = _git_tree_content_binding(sdk_root, selected_ref)
            changed = _run_git(
                sdk_root, "diff", "--name-only", f"main...{selected_ref}"
            ).splitlines()
            public_paths = sorted(
                path
                for path in changed
                if path.startswith(("docs/", "profiles/", "src/game_l10n/", "tests/"))
            )
            candidates.append(
                {
                    "branch": branch,
                    "commit": commit,
                    "inspect_ref": selected_ref,
                    "refs": [{"name": ref, "commit": ref_commit} for ref, ref_commit in refs],
                    "public_contract_paths": public_paths,
                    **binding,
                    "binding_kind": "git",
                }
            )
        source = "local_and_remote_tracking_refs"

    fingerprint_payload = json.dumps(
        candidates, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return {
        "source": source,
        "candidates": candidates,
        "fingerprint": hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
    }


def refresh_sdk_inventory(
    work_dir: Path,
    *,
    refresh_remote: bool = False,
    remote: str = "origin",
    runtime_root: Path | None = None,
    portable_branch: str | None = None,
    portable_commit: str | None = None,
) -> dict[str, Any]:
    config = load_config(work_dir)
    sdk_root = (runtime_root or Path(config.get("sdk_root") or "")).resolve()
    if runtime_root is not None:
        config["sdk_root"] = str(sdk_root)
    if refresh_remote:
        if not (sdk_root / ".git").exists():
            raise ConfigurationError(
                "Remote refresh requires a Git checkout, but SDK activation does not. "
                "Register an authenticated portable snapshot with --runtime-root, "
                "--portable-branch, and --portable-commit instead."
            )
        allowed_remote_characters = (
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
        )
        if not remote or any(character not in allowed_remote_characters for character in remote):
            raise ConfigurationError("Remote name contains unsupported characters")
        _run_git(
            sdk_root,
            "fetch",
            "--prune",
            remote,
            f"+refs/heads/sdk-*:refs/remotes/{remote}/sdk-*",
        )
    inventory = inventory_sdk_branches(
        sdk_root,
        portable_branch=portable_branch,
        portable_commit=portable_commit,
    )
    previous = config.get("sdk_selection", {})
    candidate_commits = {
        item["branch"]: item["commit"] for item in inventory["candidates"]
    }
    evaluations = {
        branch: evaluation
        for branch, evaluation in previous.get("evaluations", {}).items()
        if candidate_commits.get(branch) == evaluation.get("commit")
        and next(
            (
                candidate.get("content_fingerprint")
                for candidate in inventory["candidates"]
                if candidate["branch"] == branch
            ),
            None,
        )
        == evaluation.get("content_fingerprint")
    }
    config["sdk_selection"] = {
        "status": "pending",
        "inventory_source": inventory["source"],
        "inventory_fingerprint": inventory["fingerprint"],
        "candidates": inventory["candidates"],
        "evaluations": evaluations,
        "decision": None,
    }
    _write_config(work_dir, config)
    return config["sdk_selection"]


def record_sdk_evaluation(
    work_dir: Path,
    branch: str,
    result: str,
    reason: str,
    evidence_paths: list[str] | None = None,
    *,
    target_engine: str,
    target_version: str,
) -> dict[str, Any]:
    config = load_config(work_dir)
    selection = config.get("sdk_selection", {})
    candidates = {item["branch"]: item for item in selection.get("candidates", [])}
    if branch not in candidates:
        raise ConfigurationError("SDK branch is not present in the current inventory; refresh it first")
    target = _sdk_target(target_engine, target_version)
    allowed_reasons = SDK_EVALUATION_REASONS.get(result)
    if allowed_reasons is None:
        raise ConfigurationError(
            "SDK evaluation result must be compatible, partial, reference, or incompatible"
        )
    if reason not in allowed_reasons:
        raise ConfigurationError(f"Reason {reason!r} is not valid for SDK evaluation result {result!r}")
    if branch == target["branch"] and result == "reference":
        raise ConfigurationError(
            "A target-identity SDK cannot be reference-only; evaluate it as compatible, partial, or incompatible"
        )
    if branch != target["branch"] and result in {"compatible", "partial"}:
        raise ConfigurationError(
            "A different SDK identity cannot be reused or extended for this target; record reference or incompatible"
        )
    evidence = sorted(set(evidence_paths or []))
    available_paths = set(candidates[branch].get("public_contract_paths", []))
    unknown_evidence = sorted(set(evidence) - available_paths)
    if unknown_evidence:
        raise ConfigurationError(
            "SDK evaluation evidence is not present in the candidate contract: "
            + ", ".join(unknown_evidence)
        )
    if reason != "uninspectable_contract" and not evidence:
        raise ConfigurationError(
            "SDK evaluation requires at least one inspected public contract path"
        )
    evaluations = selection.setdefault("evaluations", {})
    evaluations[branch] = {
        "result": result,
        "reason": reason,
        "commit": candidates[branch]["commit"],
        "content_fingerprint": candidates[branch].get("content_fingerprint"),
        "evidence_paths": evidence,
        "target": target,
    }
    selection["status"] = "pending"
    selection["decision"] = None
    _write_config(work_dir, config)
    return evaluations[branch]


def record_sdk_selection(
    work_dir: Path,
    mode: str,
    branch: str | None = None,
    *,
    target_engine: str,
    target_version: str,
) -> dict[str, Any]:
    config = load_config(work_dir)
    selection = config.get("sdk_selection", {})
    if not selection.get("inventory_fingerprint"):
        raise ConfigurationError("SDK inventory is missing; run sdk-inventory first")
    candidates = {item["branch"]: item for item in selection.get("candidates", [])}
    evaluations = selection.get("evaluations", {})
    target = _sdk_target(target_engine, target_version)

    def evaluation_for_target(name: str) -> dict[str, Any]:
        evaluation = evaluations.get(name, {})
        return evaluation if evaluation.get("target") == target else {}

    if mode in {"reuse", "extend"}:
        if not branch or branch not in candidates:
            raise ConfigurationError("reuse and extend decisions require an inventoried SDK branch")
        if branch != target["branch"]:
            raise ConfigurationError(
                "reuse and extend require the SDK branch whose engine/version identity matches the target"
            )
        evaluation = evaluation_for_target(branch)
        allowed_results = {"compatible"} if mode == "reuse" else {"compatible", "partial"}
        if evaluation.get("result") not in allowed_results:
            raise ConfigurationError(f"SDK branch must be evaluated before selecting mode {mode}")
    elif mode == "derive-new":
        if not branch or branch not in candidates:
            raise ConfigurationError("derive-new requires an inventoried reference SDK branch")
        if target["branch"] in candidates:
            raise ConfigurationError(
                "The target SDK branch already exists; evaluate and reuse or extend that branch"
            )
        if branch == target["branch"]:
            raise ConfigurationError("derive-new requires a different reference SDK identity")
        evaluation = evaluation_for_target(branch)
        if evaluation.get("result") != "reference":
            raise ConfigurationError(
                "derive-new requires a reference evaluation for the selected source branch"
            )
    elif mode == "private-new":
        if branch:
            raise ConfigurationError("private-new does not accept a branch")
        if selection.get("inventory_source") == "portable_unidentified":
            raise ConfigurationError(
                "A portable Parr0t runtime is present but has no SDK identity. Register the "
                "authenticated SDK snapshot before selecting private-new."
            )
        if target["branch"] in candidates:
            raise ConfigurationError(
                "The target SDK branch already exists; evaluate and reuse or extend that branch"
            )
        unevaluated = sorted(
            name for name in candidates if not evaluation_for_target(name)
        )
        reusable = sorted(
            name
            for name in candidates
            if (evaluation := evaluation_for_target(name))
            if evaluation.get("result") != "incompatible"
        )
        uninspectable = sorted(
            name
            for name in candidates
            if (evaluation := evaluation_for_target(name))
            if evaluation.get("reason") == "uninspectable_contract"
        )
        if unevaluated:
            raise ConfigurationError(
                "Every SDK candidate must be evaluated before private-new: " + ", ".join(unevaluated)
            )
        if reusable:
            raise ConfigurationError(
                "A reusable SDK reference exists; select reuse, extend, or derive-new instead: "
                + ", ".join(reusable)
            )
        if uninspectable:
            raise ConfigurationError(
                "Uninspectable SDK branches block private-new until access is resolved: "
                + ", ".join(uninspectable)
            )
    else:
        raise ConfigurationError(
            "SDK selection mode must be reuse, extend, derive-new, or private-new"
        )

    decision = {
        "mode": mode,
        "branch": branch,
        "target": target,
        "inventory_fingerprint": selection["inventory_fingerprint"],
    }
    selection["status"] = "selected"
    selection["decision"] = decision
    _write_config(work_dir, config)
    return decision


def require_sdk_selection(config: dict[str, Any]) -> dict[str, Any]:
    selection = config.get("sdk_selection", {})
    decision = selection.get("decision")
    if selection.get("status") != "selected" or not isinstance(decision, dict):
        raise ConfigurationError(
            "SDK selection is incomplete; inventory, evaluate, and select reuse, extend, derive-new, or private-new first"
        )
    if decision.get("inventory_fingerprint") != selection.get("inventory_fingerprint"):
        raise ConfigurationError("SDK inventory changed after selection; refresh and select again")
    target = decision.get("target")
    if not isinstance(target, dict) or target.get("branch") != _sdk_target(
        str(target.get("engine", "")), str(target.get("version", ""))
    )["branch"]:
        raise ConfigurationError(
            "SDK selection has no valid target identity; evaluate and select again"
        )
    return decision


def _json_fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _selected_candidate(config: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any] | None:
    branch = decision.get("branch")
    if not branch:
        return None
    for candidate in config.get("sdk_selection", {}).get("candidates", []):
        if candidate.get("branch") == branch:
            return candidate
    raise ConfigurationError("Selected SDK branch is no longer present in the recorded inventory")


def _workspace_tool_binding(work_dir: Path) -> dict[str, Any]:
    tool_root = work_dir / "tools"
    manifest = scan_tools(tool_root)
    defaults = {
        "pack.py": PASS_THROUGH_TOOL.encode("utf-8"),
        "unpack.py": PASS_THROUGH_TOOL.encode("utf-8"),
    }
    unchanged = [
        name
        for name, content in defaults.items()
        if (tool_root / name).is_file() and (tool_root / name).read_bytes() == content
    ]
    return {
        "manifest": manifest,
        "fingerprint": _json_fingerprint(manifest),
        "default_pass_through_tools": sorted(unchanged),
    }


def _sdk_runtime_binding(checkout: Path) -> dict[str, Any]:
    checkout = checkout.resolve()
    if not checkout.is_dir():
        raise ConfigurationError(f"SDK runtime directory does not exist: {checkout}")
    if not (checkout / ".git").exists():
        return {
            "kind": "content",
            "path": str(checkout),
            "commit": None,
            "dirty": False,
            **_portable_runtime_content_binding(checkout),
        }
    status = _run_git(checkout, "status", "--porcelain").strip()
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=checkout,
        capture_output=True,
        check=False,
        shell=False,
    )
    if diff.returncode != 0:
        raise ConfigurationError("Unable to fingerprint the SDK runtime checkout")
    untracked = _run_git(checkout, "ls-files", "--others", "--exclude-standard").splitlines()
    dirty = hashlib.sha256(diff.stdout)
    for relative in sorted(item for item in untracked if item):
        path = checkout / relative
        dirty.update(relative.encode("utf-8"))
        if path.is_file():
            dirty.update(hashlib.sha256(path.read_bytes()).digest())
    return {
        "kind": "git",
        "path": str(checkout),
        "commit": _run_git(checkout, "rev-parse", "HEAD").strip(),
        "dirty": bool(status),
        "worktree_fingerprint": dirty.hexdigest(),
        **_git_tree_content_binding(checkout, "HEAD"),
    }


def record_sdk_activation(
    work_dir: Path,
    *,
    runtime_root: Path | None = None,
    reference_checkout: Path | None = None,
) -> dict[str, Any]:
    """Bind a selected SDK decision to the implementation that will actually execute it."""

    work_dir = work_dir.resolve()
    config = load_config(work_dir)
    decision = require_sdk_selection(config)
    candidate = _selected_candidate(config, decision)
    mode = decision["mode"]
    module_root = Path(__file__).resolve().parents[2]
    configured_root = Path(config.get("sdk_root") or module_root)
    if runtime_root is None:
        runtime_root = (
            configured_root
            if mode in {"reuse", "extend"} and not (module_root / ".git").exists()
            else module_root
        )
    runtime_root = runtime_root.resolve()
    runtime = _sdk_runtime_binding(runtime_root)
    tools: dict[str, Any] | None = None
    reference: dict[str, Any] | None = None

    if mode in {"reuse", "extend"}:
        if candidate is None:
            raise ConfigurationError("Selected SDK activation has no candidate commit")
        expected = str(candidate.get("commit", ""))
        if mode == "reuse":
            if runtime.get("content_fingerprint") != candidate.get("content_fingerprint"):
                raise ConfigurationError(
                    "Reuse activation runtime does not match the inventoried SDK content"
                )
            if runtime["kind"] == "git" and (
                runtime["commit"] != expected or runtime["dirty"]
            ):
                raise ConfigurationError(
                    "Reuse activation must run from the clean exact inventoried SDK commit"
                )
        if mode == "extend":
            if runtime["kind"] == "git":
                try:
                    _run_git(
                        runtime_root,
                        "merge-base",
                        "--is-ancestor",
                        expected,
                        runtime["commit"],
                    )
                except ConfigurationError as exc:
                    raise ConfigurationError(
                        "Extended SDK runtime must descend from the inventoried same-identity commit"
                    ) from exc
            elif runtime.get("content_fingerprint") != candidate.get("content_fingerprint"):
                raise ConfigurationError(
                    "A modified portable SDK cannot prove extension ancestry. Use the exact "
                    "portable snapshot for reuse or a Git worktree for an extension."
                )
    else:
        tools = _workspace_tool_binding(work_dir)
        if tools["default_pass_through_tools"]:
            raise ConfigurationError(
                "A new SDK adapter is not active while generated pass-through tools remain: "
                + ", ".join(tools["default_pass_through_tools"])
            )
        if mode == "derive-new":
            if candidate is None or reference_checkout is None:
                raise ConfigurationError(
                    "derive-new activation requires the selected reference SDK checkout"
                )
            reference = _sdk_runtime_binding(reference_checkout)
            if reference["dirty"]:
                raise ConfigurationError(
                    "derive-new requires a clean checkout of the selected reference SDK"
                )
            if reference.get("content_fingerprint") != candidate.get("content_fingerprint"):
                raise ConfigurationError(
                    "Reference runtime does not match the inventoried SDK content"
                )
            if (
                reference["kind"] == "git"
                and reference["commit"] != candidate.get("commit")
            ):
                raise ConfigurationError(
                    "Reference checkout does not match the inventoried SDK commit "
                    f"({reference['commit']} != {candidate.get('commit')})"
                )
        elif reference_checkout is not None:
            raise ConfigurationError("private-new activation does not accept a reference checkout")

    selection_binding = {
        "decision": decision,
        "candidate_commit": candidate.get("commit") if candidate else None,
        "candidate_content_fingerprint": (
            candidate.get("content_fingerprint") if candidate else None
        ),
    }
    state = {
        "schema": SDK_ACTIVATION_SCHEMA,
        "selection_fingerprint": _json_fingerprint(selection_binding),
        "mode": mode,
        "target": decision["target"],
        "runtime": runtime,
        "candidate_commit": candidate.get("commit") if candidate else None,
        "reference": reference,
        "workspace_tools": tools,
    }
    path = work_dir / SDK_ACTIVATION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return state


def require_sdk_activation(
    work_dir: Path,
    config: dict[str, Any] | None = None,
    *,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    work_dir = work_dir.resolve()
    config = config or load_config(work_dir)
    decision = require_sdk_selection(config)
    candidate = _selected_candidate(config, decision)
    selection_binding = {
        "decision": decision,
        "candidate_commit": candidate.get("commit") if candidate else None,
        "candidate_content_fingerprint": (
            candidate.get("content_fingerprint") if candidate else None
        ),
    }
    path = work_dir / SDK_ACTIVATION_PATH
    if not path.is_file():
        raise ConfigurationError(
            "Selected SDK implementation is not active; run sdk-activate from the intended runtime"
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("SDK activation record is unreadable; activate it again") from exc
    if (
        state.get("schema") != SDK_ACTIVATION_SCHEMA
        or state.get("selection_fingerprint") != _json_fingerprint(selection_binding)
        or state.get("mode") != decision.get("mode")
        or state.get("target") != decision.get("target")
    ):
        raise ConfigurationError("SDK activation is stale for the current selection")

    expected_runtime = state.get("runtime")
    if not isinstance(expected_runtime, dict) or not expected_runtime.get("path"):
        raise ConfigurationError("SDK activation record has no runtime binding")
    if runtime_root is None:
        runtime_root = (
            Path(expected_runtime["path"])
            if expected_runtime.get("kind") == "content"
            else Path(__file__).resolve().parents[2]
        )
    current_runtime = _sdk_runtime_binding(runtime_root.resolve())
    if expected_runtime != current_runtime:
        raise ConfigurationError(
            "This command is not running from the SDK implementation recorded by sdk-activate"
        )

    tools = state.get("workspace_tools")
    if tools is not None:
        current_tools = _workspace_tool_binding(work_dir)
        if current_tools != tools or current_tools["default_pass_through_tools"]:
            raise ConfigurationError("Workspace SDK adapter changed after activation")

    reference = state.get("reference")
    if reference is not None and _sdk_runtime_binding(Path(reference["path"])) != reference:
        raise ConfigurationError("Selected SDK reference checkout changed after activation")
    return state


def _write_config(work_dir: Path, config: dict[str, Any]) -> None:
    (work_dir / "game-l10n.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _apply_execution_defaults(config: dict[str, Any]) -> None:
    execution = config.setdefault("execution", {})
    if not isinstance(execution, dict):
        raise ConfigurationError("Workspace execution policy must be an object")
    try:
        policy_version = int(execution.get("native_shard_policy_version", 1))
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("native_shard_policy_version must be an integer") from exc
    if policy_version < 2 and (
        (
            execution.get("native_shard_records") == 256
            and "native_shard_material_tokens" not in execution
        )
        or (
            execution.get("native_shard_records") == 32
            and execution.get("native_shard_material_tokens") == 12_000
        )
    ):
        execution["native_shard_records"] = 64
        execution["native_shard_material_tokens"] = 24_000
    if (
        policy_version < 5
        and execution.get("native_shard_material_tokens") == 24_000
    ):
        execution["native_shard_material_tokens"] = 8_000
    try:
        control_budget_policy_version = int(
            execution.get("control_turn_budget_policy_version", 1)
        )
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            "control_turn_budget_policy_version must be an integer"
        ) from exc
    if control_budget_policy_version < 2:
        # Older workspaces carried framework-selected 12/64/5M ceilings. They
        # were not evidence of a user request and must not survive migration.
        execution["control_turn_budget_user_approved"] = False
        execution["max_root_model_calls_per_wave"] = None
        execution["max_root_model_calls_per_turn"] = None
        execution["max_root_input_tokens_per_turn"] = None
        execution["budget_exhaustion_action"] = (
            "continue_until_terminal_or_actionable_blocker"
        )
    for key, value in CONFIG_TEMPLATE["execution"].items():
        execution.setdefault(key, value)
    execution["native_shard_policy_version"] = 5
    execution["control_turn_budget_policy_version"] = 2
    control_turn_budget_user_approved = execution.get(
        "control_turn_budget_user_approved", False
    )
    if not isinstance(control_turn_budget_user_approved, bool):
        raise ConfigurationError("control_turn_budget_user_approved must be a boolean")
    if not control_turn_budget_user_approved:
        execution["max_root_model_calls_per_wave"] = None
        execution["max_root_model_calls_per_turn"] = None
        execution["max_root_input_tokens_per_turn"] = None
        execution["budget_exhaustion_action"] = (
            "continue_until_terminal_or_actionable_blocker"
        )
    else:
        limits = (
            execution.get("max_root_model_calls_per_wave"),
            execution.get("max_root_model_calls_per_turn"),
            execution.get("max_root_input_tokens_per_turn"),
        )
        if not any(value is not None for value in limits):
            raise ConfigurationError(
                "A user-approved control-turn budget requires an explicit limit"
            )
        if any(
            value is not None
            and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            )
            for value in limits
        ):
            raise ConfigurationError(
                "Control-turn budget limits must be positive integers"
            )
        execution["budget_exhaustion_action"] = "checkpoint_and_return_control"


def _apply_review_routing_defaults(config: dict[str, Any]) -> None:
    routing = config.setdefault("routing", {})
    if not isinstance(routing, dict):
        raise ConfigurationError("Workspace routing policy must be an object")
    preset = str(config.get("provider_preset", ""))
    if preset == CODEX_APP_PRESET:
        routing.setdefault("review_triagers", ["native-luna"])
    elif preset == "openai-deepseek":
        routing.setdefault("review_triagers", ["openai-luna"])
    else:
        routing.setdefault("review_triagers", [])
    routing.setdefault("strong_review_sample_percent", 5.0)
    routing.setdefault(
        "strong_review_high_risk_categories",
        [
            "allusion",
            "critical_logic",
            "parody",
            "quest_title",
            "script_sensitive",
            "wordplay",
        ],
    )


def _apply_translation_retry_defaults(config: dict[str, Any]) -> None:
    """Upgrade only the two known preset topologies; never rewrite custom routing."""

    routing = config.setdefault("routing", {})
    if not isinstance(routing, dict):
        raise ConfigurationError("Workspace routing policy must be an object")
    preset = str(config.get("provider_preset", ""))
    translators = routing.get("translators")
    if preset == CODEX_APP_PRESET:
        if translators == ["native-luna", "deepseek"]:
            routing["translators"] = ["native-luna", "native-sol", "deepseek"]
        routing.setdefault(
            "translator_retries", {"native-luna": 5, "native-sol": 3}
        )
    elif preset == "openai-deepseek":
        if translators == ["openai-luna", "deepseek"]:
            routing["translators"] = ["openai-luna", "openai-sol", "deepseek"]
        routing.setdefault(
            "translator_retries", {"openai-luna": 5, "openai-sol": 3}
        )


def _apply_control_plane_defaults(config: dict[str, Any]) -> None:
    """Migrate the fixed observation boundary without weakening explicit drift."""

    control_plane = config.setdefault("control_plane", {})
    if not isinstance(control_plane, dict):
        raise ConfigurationError("Workspace control_plane policy must be an object")
    for key, value in CONTROL_OBSERVATION_POLICY.items():
        control_plane.setdefault(key, json.loads(json.dumps(value)))


def _apply_image_localization_defaults(config: dict[str, Any]) -> None:
    policy = config.setdefault("image_localization", {})
    if not isinstance(policy, dict):
        raise ConfigurationError("Workspace image_localization policy must be an object")
    for key, value in CONFIG_TEMPLATE["image_localization"].items():
        policy.setdefault(key, value)
    if (
        policy.get("default_policy") != "disabled"
        or policy.get("explicit_user_request_required") is not True
        or policy.get("definite_player_visible_ui_binding_required") is not True
        or policy.get("recursive_ocr_allowed") is not False
    ):
        raise ConfigurationError(
            "Workspace image-localization defaults cannot enable implicit image inspection or OCR"
        )


def initialize_workspace(
    work_dir: Path,
    sdk_root: Path,
    *,
    git_choice: str = "pending",
    game_roots: list[Path] | None = None,
    provider_preset: str = CODEX_APP_PRESET,
) -> None:
    work_dir = work_dir.resolve()
    sdk_root = sdk_root.resolve()
    require_external_workdir(work_dir, sdk_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(json.dumps(CONFIG_TEMPLATE))
    if provider_preset not in {"openai-deepseek", CODEX_APP_PRESET, "custom"}:
        raise ConfigurationError(
            "provider_preset must be openai-deepseek, codex-app-deepseek, or custom"
        )
    config["provider_preset"] = provider_preset
    config["provider_policy"] = {
        "selected_preset": provider_preset,
        "key_presence_selects_mode": False,
    }
    if provider_preset == CODEX_APP_PRESET:
        config["execution_mode"] = CODEX_APP_EXECUTION_MODE
        deepseek = next(
            item for item in config["providers"] if item["name"] == "deepseek"
        )
        deepseek["allowed_roles"] = sorted(CODEX_APP_EXTERNAL_ROLES)
        config["providers"] = [deepseek]
        config["native_workflow"] = {
            "required": True,
            "launcher": "collaboration",
            "child_model_cli_allowed": False,
            "external_provider_primary_allowed": False,
            "external_provider_roles": sorted(CODEX_APP_EXTERNAL_ROLES),
        }
        config["routing"] = {
            "context_analyzers": ["native-sol"],
            "translators": ["native-luna", "native-sol", "deepseek"],
            "translator_retries": {
                "native-luna": 5,
                "native-sol": 3,
            },
            "review_triagers": ["native-luna"],
            "reviewers": ["native-sol", "deepseek"],
            "rewriters": ["deepseek"],
            "strong_review_sample_percent": 5.0,
            "strong_review_high_risk_categories": [
                "allusion",
                "critical_logic",
                "parody",
                "quest_title",
                "script_sensitive",
                "wordplay",
            ],
            "require_review": True,
            "review_max_rounds": 2,
            "max_workers": 8,
            "retry_refusals_once": False,
            "grace_retries": 3,
            "grace_retry_codes": [
                "malformed_response",
                "provider_error",
                "rate_limited",
                "timeout",
                "integrity_invalid",
            ],
            "defer_fallbacks_until_primary_complete": True,
            "preserve_source_on_failure": True,
            "review_refusal_policy": "fallback",
        }
    elif provider_preset == "custom":
        config["execution_mode"] = "custom_provider"
        config["providers"] = [
            {
                "name": "translator-primary",
                "type": "openai_responses",
                "model": "CHANGE_ME",
                "api_key_env": "OPENAI_API_KEY",
            },
            {
                "name": "reviewer-primary",
                "type": "openai_responses",
                "model": "CHANGE_ME",
                "api_key_env": "OPENAI_API_KEY",
            },
        ]
        config["routing"] = {
            "context_analyzers": ["translator-primary"],
            "translators": ["translator-primary"],
            "translator_retries": {},
            "review_triagers": [],
            "reviewers": ["reviewer-primary"],
            "rewriters": ["reviewer-primary"],
            "require_review": True,
            "review_max_rounds": 2,
            "max_workers": 1,
            "retry_refusals_once": True,
            "grace_retries": 0,
            "defer_fallbacks_until_primary_complete": False,
            "preserve_source_on_failure": False,
            "review_refusal_policy": "fallback",
        }
    else:
        config["execution_mode"] = "api"
    config["sdk_root"] = str(sdk_root)
    config["game_roots"] = [str(path.resolve()) for path in (game_roots or [])]
    config["repository"]["initial_choice"] = git_choice
    inventory = inventory_sdk_branches(sdk_root)
    config["sdk_selection"] = {
        "status": "pending",
        "inventory_source": inventory["source"],
        "inventory_fingerprint": inventory["fingerprint"],
        "candidates": inventory["candidates"],
        "evaluations": {},
        "decision": None,
    }
    validate_provider_policy(config)
    files = {
        "game-l10n.json": json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        MODE_LOCK_PATH: json.dumps(
            provider_mode_lock(config), ensure_ascii=False, indent=2
        ) + "\n",
        CONTROL_STATE_PATH: json.dumps(
            initial_control_plane_state(), ensure_ascii=False, indent=2
        ) + "\n",
        CONTROL_ENTRY_PATH: json.dumps(
            initial_control_plane_entry_state(), ensure_ascii=False, indent=2
        ) + "\n",
        LANGUAGE_POLICY_PATH: json.dumps(
            {
                "schema": LANGUAGE_POLICY_SCHEMA,
                "target_language": "ko",
                "revision": 0,
                "authorization": "default_korean",
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        "AGENTS.md": AGENTS_TEXT,
        ".gitignore": GITIGNORE_TEXT,
        "context/glossary.json": "[]\n",
        "context/characters.json": "[]\n",
        "tools/unpack.py": PASS_THROUGH_TOOL,
        "tools/pack.py": PASS_THROUGH_TOOL,
    }
    for relative, content in files.items():
        path = work_dir / relative
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for directory in ("game-data", "inputs", "translations", "logs"):
        (work_dir / directory).mkdir(exist_ok=True)


def load_config(work_dir: Path) -> dict[str, Any]:
    path = work_dir / "game-l10n.json"
    if not path.is_file():
        raise ConfigurationError(f"Workspace config not found: {path}")
    config = json.loads(path.read_text(encoding="utf-8"))
    _apply_execution_defaults(config)
    _apply_review_routing_defaults(config)
    _apply_translation_retry_defaults(config)
    _apply_control_plane_defaults(config)
    _apply_image_localization_defaults(config)
    require_target_language_policy(work_dir, config)
    lock_path = work_dir / MODE_LOCK_PATH
    mode_lock = (
        json.loads(lock_path.read_text(encoding="utf-8"))
        if lock_path.is_file()
        else None
    )
    validate_provider_policy(
        config,
        mode_lock=mode_lock,
        require_mode_lock=config.get("provider_preset") == CODEX_APP_PRESET,
    )
    return config


def update_repository_policy(work_dir: Path, *, initial_choice: str | None = None, publish_approved: bool | None = None) -> None:
    config = load_config(work_dir)
    policy = config.setdefault("repository", {})
    if initial_choice is not None:
        if initial_choice not in {"enabled", "disabled", "pending"}:
            raise ConfigurationError("initial_choice must be enabled, disabled, or pending")
        policy["initial_choice"] = initial_choice
        if initial_choice != "enabled":
            policy.pop("publish_approved", None)
    if publish_approved is not None:
        if publish_approved and policy.get("initial_choice") != "enabled":
            raise ConfigurationError("Publishing cannot be approved unless Git use was enabled initially")
        policy["publish_approved"] = publish_approved
    _write_config(work_dir, config)


def prepare_sdk_profile(work_dir: Path, engine: str, version: str, *, user_confirmed_success: bool) -> Path:
    if not user_confirmed_success:
        raise ConfigurationError("The user must explicitly confirm a successful translation/patch before promotion")
    config = load_config(work_dir)
    selection = require_sdk_selection(config)
    require_sdk_activation(work_dir, config)
    if selection.get("mode") not in {"derive-new", "private-new"}:
        raise ConfigurationError(
            "An existing SDK branch was selected; reuse or extend that branch instead of creating a duplicate profile"
        )
    target = _sdk_target(engine, version)
    if target != selection.get("target"):
        raise ConfigurationError(
            "Promotion engine/version must match the target identity recorded by sdk-select"
        )
    profile_name = target["branch"]
    tool_root = work_dir / "tools"
    manifest = scan_tools(tool_root)
    export_root = work_dir / "sdk-export" / profile_name
    if export_root.exists():
        shutil.rmtree(export_root)
    shutil.copytree(tool_root, export_root / "tools")
    profile = {
        "profile": profile_name,
        "engine": target["engine"],
        "version": target["version"],
        "selection_mode": selection["mode"],
        "reference_branch": selection.get("branch") if selection["mode"] == "derive-new" else None,
        "contains_game_data": False,
        "files": manifest,
    }
    (export_root / "profile.json").write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return export_root


def publish_sdk_profile(
    work_dir: Path,
    sdk_root: Path,
    profile_root: Path,
    *,
    commit: bool = False,
    push: bool = False,
) -> str:
    """Create an engine SDK branch from main after both recorded user approvals.

    The caller must separately record the initial Git choice and the post-success publish approval.
    This function never initializes a repository and never stages files outside the sanitized profile.
    """
    config = load_config(work_dir)
    policy = config.get("repository", {})
    if policy.get("initial_choice") != "enabled":
        raise ConfigurationError("Git use was not enabled at the beginning of the task")
    if policy.get("publish_approved") is not True:
        raise ConfigurationError("The user has not approved publishing after confirming success")
    sdk_root = sdk_root.resolve()
    require_external_workdir(work_dir.resolve(), sdk_root)
    if not (sdk_root / ".git").exists():
        raise ConfigurationError("SDK repository is not initialized; this command never runs git init")
    profile_data = json.loads((profile_root / "profile.json").read_text(encoding="utf-8"))
    selection = require_sdk_selection(config)
    require_sdk_activation(work_dir, config)
    target = selection["target"]
    expected_reference = (
        selection.get("branch") if selection.get("mode") == "derive-new" else None
    )
    if selection.get("mode") not in {"derive-new", "private-new"}:
        raise ConfigurationError(
            "The recorded selection does not authorize creating a new SDK identity"
        )
    if (
        profile_data.get("profile") != target["branch"]
        or profile_data.get("engine") != target["engine"]
        or profile_data.get("version") != target["version"]
        or profile_data.get("selection_mode") != selection.get("mode")
        or profile_data.get("reference_branch") != expected_reference
    ):
        raise ConfigurationError(
            "SDK profile does not match the recorded target identity and selection provenance"
        )
    branch = target["branch"]
    scan_tools(profile_root / "tools")

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args], cwd=sdk_root, text=True, encoding="utf-8", capture_output=True, shell=False
        )
        if completed.returncode != 0:
            raise ConfigurationError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
        return completed.stdout.strip()

    if git("status", "--porcelain"):
        raise ConfigurationError("SDK repository must be clean before publishing a profile")
    git("rev-parse", "--verify", "main")
    git("switch", "main")
    git("switch", "-c", branch)
    destination = sdk_root / "profiles" / branch
    if destination.exists():
        raise ConfigurationError(f"Profile destination already exists: {destination}")
    shutil.copytree(profile_root, destination)
    if commit or push:
        git("add", "--", str(destination.relative_to(sdk_root)))
        git("commit", "-m", f"Add {branch} game localization toolkit")
        verify_sdk_promotion_commit(sdk_root, branch, "HEAD")
    if push:
        git("push", "-u", "origin", branch)
    return branch


def verify_sdk_promotion_commit(
    sdk_root: Path,
    branch: str,
    commit: str = "HEAD",
    *,
    working_tree: bool = False,
    planned_paths: list[str] | None = None,
    expected_main: str | None = None,
) -> dict[str, Any]:
    """Check SDK path ownership before implementation or on a final commit.

    Preflight is read-only and does not attest tests, privacy, or publication approval.
    Pass its base hash as expected_main on subsequent checks to detect base drift.
    """

    sdk_root = sdk_root.resolve()
    if not (sdk_root / ".git").exists():
        raise ConfigurationError("E_SDK_PROMOTION_REPOSITORY: SDK repository is unavailable")
    if (
        not branch.startswith("sdk-")
        or not branch.removeprefix("sdk-")
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in branch)
    ):
        raise ConfigurationError("E_SDK_PROMOTION_BRANCH: target must be a safe sdk-* branch")
    if (
        not commit
        or len(commit) > 128
        or commit.startswith("-")
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._/"
            for character in commit
        )
    ):
        raise ConfigurationError("E_SDK_PROMOTION_COMMIT: commit reference is unsafe")

    if planned_paths and not working_tree:
        raise ConfigurationError("E_SDK_PROMOTION_PATH: planned paths require working-tree preflight")
    if expected_main is not None and (
        len(expected_main) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in expected_main)
    ):
        raise ConfigurationError("E_SDK_PROMOTION_BASE: expected main must be a full commit hash")

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=sdk_root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            shell=False,
        )
        if completed.returncode != 0:
            raise ConfigurationError(
                f"E_SDK_PROMOTION_GIT: git {' '.join(args)} failed"
            )
        return completed.stdout.rstrip("\r\n")

    def is_ancestor(ancestor: str, descendant: str) -> bool:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=sdk_root,
            text=True,
            encoding="utf-8",
            capture_output=True,
            shell=False,
        )
        if completed.returncode not in {0, 1}:
            raise ConfigurationError("E_SDK_PROMOTION_GIT: ancestry check failed")
        return completed.returncode == 0

    base = git("rev-parse", "--verify", "main^{commit}")
    if expected_main is not None and base != expected_main:
        raise ConfigurationError("E_SDK_PROMOTION_BASE: main changed since preflight; reconcile explicitly")
    git("rev-parse", "--verify", f"{branch}^{{commit}}")
    if working_tree:
        if commit != "HEAD":
            raise ConfigurationError("E_SDK_PROMOTION_COMMIT: preflight checks HEAD and the working tree")
        if git("symbolic-ref", "--quiet", "--short", "HEAD") != branch:
            raise ConfigurationError("E_SDK_PROMOTION_BRANCH: preflight requires the target branch checkout")
        resolved = git("rev-parse", "--verify", "HEAD^{commit}")
        if not is_ancestor(base, resolved):
            raise ConfigurationError("E_SDK_PROMOTION_BASE: merge the intended main revision before preflight")
        paths = set()
        # Include committed, staged and unstaged changes separately: a worktree
        # reversal must not conceal a staged or already committed core edit.
        for revisions in ((base, "HEAD"), ("--cached", base), (base,)):
            paths.update(git("diff", "--name-only", "--no-renames", "-z", *revisions, "--").split("\0"))
        paths.update(git("ls-files", "--others", "--exclude-standard", "-z").split("\0"))
        for path in planned_paths or []:
            normalized = path.replace("\\", "/")
            if (
                not normalized
                or normalized.startswith("/")
                or ":" in normalized
                or "\0" in normalized
                or any(part in {"", ".", ".."} for part in normalized.split("/"))
            ):
                raise ConfigurationError("E_SDK_PROMOTION_PATH: planned paths must be repository-relative files")
            paths.add(normalized)
        changed_paths = sorted(paths - {""})
    else:
        resolved = git("rev-parse", "--verify", f"{commit}^{{commit}}")
        if not is_ancestor(resolved, branch):
            raise ConfigurationError(
                "E_SDK_PROMOTION_COMMIT: commit is not reachable from the target SDK branch"
            )
        ancestry = git("rev-list", "--parents", "-n", "1", resolved).split()
        if len(ancestry) != 2:
            raise ConfigurationError(
                "E_SDK_PROMOTION_COMMIT: promotion must be one non-merge engine commit"
            )
        if not is_ancestor(base, ancestry[1]):
            raise ConfigurationError(
                "E_SDK_PROMOTION_BASE: merge the intended main revision before promotion"
            )
        changed_paths = sorted(set(git(
            "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-z", "-r", resolved,
        ).split("\0")) - {""})
    if not changed_paths and not working_tree:
        raise ConfigurationError("E_SDK_PROMOTION_COMMIT: promotion commit is empty")
    main_paths = set(git("ls-tree", "-r", "--name-only", "-z", base).split("\0")) - {""}
    core_paths = sorted(set(changed_paths) & main_paths)
    if core_paths:
        raise ConfigurationError(
            "E_SDK_PROMOTION_CORE_PATH: promotion changes main-owned paths: "
            + ", ".join(core_paths)
        )
    return {
        "schema": "parr0t.sdk-promotion-scope/v1",
        "status": "scope_ready" if working_tree else "ready",
        "mode": "working-tree" if working_tree else "commit",
        "branch": branch,
        "commit": resolved,
        "base": base,
        "changed_paths": changed_paths,
        "main_owned_paths": [],
    }
