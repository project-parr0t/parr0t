from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .control_plane import CONTROL_STATE_PATH, initial_control_plane_state
from .errors import ConfigurationError
from .privacy import require_external_workdir, scan_tools
from .provider_policy import (
    CODEX_APP_EXTERNAL_ROLES,
    CODEX_APP_EXECUTION_MODE,
    CODEX_APP_PRESET,
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
        "translators": ["openai-luna", "deepseek"],
        "reviewers": ["openai-sol", "deepseek"],
        "rewriters": ["deepseek", "openai-sol"],
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
        "native_shard_records": 32,
        "native_shard_material_tokens": 12000,
        "native_worker_history": "none",
        "native_worker_reuse": False,
    },
    "control_plane": {
        "envelope_schema": "parr0t.control-envelope.v1",
        "metadata_only_for_all_data_plane_stages": True,
        "reject_free_form_worker_returns": True,
        "rotate_after_leak": True,
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

This directory contains private game data and translation artifacts. It is intentionally separate from the SDK repository.

## Mandatory workflow

1. Before any `git init`, remote creation, branch creation, commit, or push, ask the user whether Git use is allowed for this game/tooling task. Record the answer in `game-l10n.json` under `repository.initial_choice`. If refused, do not initialize Git. Run `game-l10n status --workspace <this-directory>` first; it reports the next safe action without sending data.
1a. Before writing or adapting format tooling, derive the target engine/version identity from the inspected runtime, inspect the candidates recorded by `game-l10n sdk-inventory`, and bind every `sdk-evaluate` and `sdk-select` call to that target. Keep implementation reuse separate from compatibility ownership: reuse or extend only the exact target branch; mark useful code from a different identity as `reference` and select `derive-new`. Use `private-new` only when every candidate is incompatible and none is a reusable reference. The CLI blocks extraction, context, translation, and new-profile promotion until this gate is complete.
2. Never copy source game data, extracted strings, translations, prompts, caches, logs, or credentials into the SDK repository.
3. Run unpack and pack through `game-l10n run-tool`; identical tool/input/environment fingerprints are skipped automatically.
4. Run translation through the orchestrator. The orchestrator only monitors execution/protocol status and fallback routing. It must not replace the independent review agent.
5. Treat model refusals, malformed output, timeouts, and failed integrity checks as route failures. Continue through the configured fallback chain and keep the audit record free of game text.
6. Preserve placeholders, tags, control codes, glossary terms, names, character voice and scene state, UI actions/states/conditions/units, and knowledge order/prerequisites. Preserve communicative function, idiomatic target-language phrasing, supported allusions/wordplay, deliberate alterations, and function-appropriate tone/rhythm. Do not approve a mechanically literal result merely because its denotational meaning is correct, and do not invent a reference from weak resemblance.
7. A toolchain may be promoted only after the user explicitly states that the translation/patch succeeded, and the promotion engine/version must equal the target identity recorded before implementation. A reference SDK is a code source, not the promotion destination. Keep the sanitized SDK profile local and stop. Do not propose, ask about, preview, stage, or mention commit, push, repository publication, tags, or releases unless the user first gives an explicit publication command.
8. Engine profiles use branch names `sdk-<engine>-<version>` and originate from `main`. For `derive-new`, port only the verified neutral capability and never merge the reference SDK branch wholesale. Export only reusable files from `tools/`; the privacy scan must pass first.
9. Keep promoted SDK content provenance-free. Never transfer real game/project names, filenames, asset paths, map/event identifiers, dialogue, translations, project-specific exclusions, job metadata, tool paths, counts, hashes, reports, or execution history. Use only public engine-format identifiers and unmistakably synthetic fixtures.
10. Follow the repository `docs/engine-faq-contract.md`. Classify each material engine problem as `automated_fix`, `engine_runbook`, `private_only`, or `unresolved`. Fix deterministic conditions in SDK code and regression tests. Add an engine runbook entry only when safe continuation requires external tooling, user material, runtime evidence, authorization, or a decision the SDK cannot infer; keep title-specific incidents private and unresolved causes in the failure queue. Entry count is not a goal.
11. Treat translation as a faithful transformation of existing user-provided text, never as new scene generation, continuation, roleplay, or endorsement. Analyze the complete ordered inventory first, using `auto`, `narrative`, `functional`, `knowledge`, or `hybrid` context; give each target only the relevant typed evidence. Do not censor, soften, intensify, or invent content. A native worker may receive an exclusive ID shard to reduce startup overhead, but it must load and finalize one target at a time. A refusal applies only to the individual target segment and must never cancel unrelated segments.
12. Follow the configured role chains, not hard-coded provider identities. Finish the complete primary translation pass before translation fallback. Review with compact judges; invoke configured rewriters only for `rewrite` verdicts; confirm corrected candidates; and send only refused/failed judge targets to the next judge. Provider-neutral substitution is allowed only when the user explicitly selected `custom`; never turn a locked preset into `custom` as a workaround. If every permitted route fails, emit the unchanged source with `source_preserved`; never silently claim it was reviewed.
13. A selected provider preset authorizes only the providers and stage roles declared by that preset. API-key presence never selects a mode, expands a role, promotes fallback to primary, or authorizes a different external service. Once an authorized fallback stage becomes eligible, do not pause for another per-segment approval. Keep configured game roots read-only; create translated data, rebuilt archives, and installable patches only under this workspace. Never overwrite an installed or source game in place.
14. Distinguish API providers from native Codex App workers. In `codex-app-deepseek`, use native GPT-6 Luna translator subagents and a separate native GPT-6.1 Sol compact judge; `DEEPSEEK_API_KEY` unlocks only post-barrier translation fallback, selective rewriting, and judge fallback. Never run `codex.exe`, `codex exec`, a second Codex session, or a command provider to imitate a native worker, and never request external-transfer approval as a workaround. Other provider combinations remain available only through a user-selected preset such as `openai-deepseek` or `custom`. The top-level agent remains orchestration-only and follows `docs/codex-app-deepseek.md`.
15. Keep a strict data-plane/control-plane boundary. The top-level agent may receive target IDs, categorical status/error codes, counts, queue transitions, artifact paths, and deterministic issue codes only. It must not open dialogue/input/result files or receive source, candidate, translated text, or provider prose refusal explanations. Workers read and write private workspace files and return metadata-only envelopes.
15a. Apply that boundary to primary and independent inventory, context analysis, translation, review, rewriting, application, and validation—not only native translation. Extraction and context workers may inspect private artifacts but may return only a `parr0t.control-envelope.v1` file. Their final chat response is the workspace-relative envelope path only. Validate it with `game-l10n validate-control-envelope` before using any result. Do not accept worker prose, pasted command output, summaries of content, semantic safety labels, anticipated provider-policy judgments, or detailed review categories such as meaning, omission, voice, tone, or terminology.
15b. Inventory workers classify structure, player visibility, bindings, and order only; context workers produce evidence without deciding translation eligibility. Neither role may exclude user-visible text because of subject matter or suppress a route before an actual configured model/provider invocation. Only the model/provider that processed its assigned context chunk, translation target, or review target may return categorical `provider_refusal`. The parent must not inspect private content to confirm or reinterpret it.
15c. If the parent receives source/candidate text, prose refusal reasons, content classification, or performs a semantic judgment itself, record the fixed incident code with `game-l10n record-control-plane-leak`, stop using that top-level agent, and preserve only metadata checkpoints. Continue in a fresh control plane and acknowledge the matching incident with `game-l10n resume-control-plane`; never ask the contaminated agent to summarize or sanitize what it saw.
16. A translation or review refusal is a routing event, not a content judgment and not grounds to omit a target or cancel patch creation. Normalize explanations before returning metadata to the parent. Use the configured faithful-transformation prompt and supported deterministic low-creativity settings for DeepSeek; never claim or attempt to disable provider safeguards.
17. The checked-out `main` branch is an engine-neutral control framework. Engine-specific discovery, unpack, apply, pack, verification, `docs/engine-faq.md`, ignore rules, and fixtures must come from the selected `sdk-<engine>-<version>` branch and must never be copied back into `main`. Inspect only matching runbook triggers when the SDK protocol reaches a condition it cannot resolve.
18. Follow the repository `docs/execution-budget.md`. Build the smallest artifact that satisfies the active request. Do not add speculative rollback archives, legacy migrations, compatibility layers, exhaustive backups, or unrelated generalization. After the required focused checks pass, stop unless inputs changed or a concrete failure justifies more testing.
19. Handle simple fixes, packaging, documentation, and status inspection in the current agent. Use subagents only for independent bulk translation/review shards or genuinely parallel research. Do not delegate deterministic rechecks or small installer work.
20. For Steam delivery, use `game-l10n steam-package` with a completed payload and private spec. The generated installer owns only receipt-listed files, skips missing Steam libraries, and falls back to a supplied game directory. Do not embed a previous package or copy Steam/game-specific data into the SDK.
21. Building, fixing, packing, or validating a patch does not authorize installing, updating, removing, or launching it. Access to a game folder is read-only discovery permission only. Do not infer runtime permission from a request to inspect logs, test a build, or confirm execution data.
22. Default to handing the recoverable artifact to the user for execution. If the user explicitly asks the agent to act on a real installation, record a separate short-lived one-shot approval for exactly one of `install`, `update`, `remove`, or `launch`. Bind it to the current artifact digest, exact configured game root, and—only for launch—the exact executable. A changed artifact, target, executable, action, expiry, or consumed approval must stop the operation.
23. Installation approval never authorizes launch, and launch approval never authorizes installation, update, removal, cleanup, log deletion, save modification, or another launch. Consume the matching approval immediately before the external effect. Automated lifecycle tests use temporary synthetic targets; only isolated repository tests may create synthetic approval fixtures for those targets.
24. The public per-game instance workflow is dormant until the user first explicitly commands publication. Before that command, do not mention, propose, ask about, preview, or prepare a publication repository, commit/push, tag, GitHub Release, release gate, or upload. Once activated, follow the repository `docs/instance-release.md`, target the player's execution environment, default to Windows unless another platform is named, and derive architecture and prerequisites from the supported game build. Keep deployable archives, installers, generated payloads, game data, copied SDKs, and duplicate translation exports out of the instance branch; publish verified distributable bytes only as GitHub Release assets. The branch normally contains only the preserved README/legal notices, canonical checkpoint plus `parr0t-instance-release.json`, and strictly necessary minimal title/engine build glue. When a translation source repository, current read-only game folder, and instance/checkpoint repository are supplied together, validate and use each for its separate role before any model call. Build once from clean referenced commits and upload the exact verified bytes; bind the instance, translation-source, and SDK commits, tag, release ID, package/manifest/file hashes, and embedded versions as one release unit. Record every supported original game version/build and make the README, release notes, manifest, asset name, and installer checks agree. Preserve the user's existing README with minimal reviewed edits. Use the visible title `<Game Display Name> Patch <Patch Version> (Game Version <Game Version>)`, or the exact enumerated `Game Versions` form. A validated package is not publication readiness: after committing the lean tree locally and before any push, tag, `gh release create`, or upload, run `game-l10n instance-release-gate` and require `ready` for the exact commit and exact external asset. Never replace it with a generic GitHub Release workflow or repair the contract only after publication.
25. Player-facing Windows installers follow the repository `docs/installer-contract.md`, not this agent harness. Keep the fixed ZIP layout and the public Install, Restore, and Verify installed-state operations. Use `<game-root>/parr0t-patch-status.txt` as the first patch-ID authority and the matching receipt for owned files. Explicitly declare any original file that must be replaced; preserve it once, verify and reuse that backup on update, and put it back on Restore. Core result codes are fixed; SDK/title-specific failures use a separate `PARR0T-<SCOPE>-E###` registry. Never copy `AGENTS.md`, approval JSON, prompts, task state, logs, or workspace policy into a distribution. An interactive player's menu choice plus final confirmation needs no agent approval; noninteractive mutation still does.
26. For Steam discovery or integration, follow the repository `docs/steam-faq.md` and `docs/wrapper-executable.md`. Use exact App ID metadata and binding checks and ask when the validated game root is ambiguous. Never read or edit `localconfig.vdf`, Steam launch options, or per-account client configuration, and never require, close, kill, or restart Steam. Integrate the Steam Play button only with exact approved, reproducible, signature/hash-verified Parr0t wrapper bytes for the target architecture at explicitly declared main-executable paths; preserve each real original through `replace_files` and do not use `.lnk` or an ad hoc game-workspace binary. Require the target game to be closed for locked replacements; close only the validated game process after separate player confirmation.
27. The control plane must proactively use the repository `skills/request-scope-guard/SKILL.md` whenever the user limits or narrows scope, asks for read-only inspection/diagnosis, or a contemplated action is optional, unrequested, destructive, or externally consequential. Prefer a discovered `$request-scope-guard`; otherwise read and apply the bundled file directly. Re-evaluate after every user message and give workers only the exact authorized targets and side effects. No worker may infer permission to expand scope, clean up data, commit/push, publish, deploy, install, launch, or contact an external system.
28. Do not start context analysis or translation from one extractor's manifest alone. Produce a primary and an independently implemented ordered inventory using the selected SDK, declare user-visible exclusions, and pass `game-l10n verify-inventory`. `manifest_missing=0` describes only result alignment; it must never be reported as zero in-game omissions. Runtime completeness is a separate state and remains `not_run` until metadata from a separately authorized or user-executed runtime observation is recorded.
29. Context analysis is complete only when the CLI records a successful per-part execution attestation and evidence coverage gate. A partial artifact, interrupted worker, capacity error, or file left by a failed route cannot unblock translation. Route operational failures through the configured context fallback; if every route fails, stop.
30. Reusing an existing candidate proves neither quality nor review. When the user requests a complete re-review, run `game-l10n review-existing` so every current target receives a fresh independent semantic verdict under the current context. Do not review only automatically flagged IDs and do not describe identifier, digest, or structure checks as semantic review.
31. Capacity, timeout, rate-limit, quota, authentication, malformed-response, and other operational failures enter the metadata-only failure queue. Do not retry that queue after the turn ends until the user explicitly requests resumption. Extractors mark indispensable targets with `metadata.blocking_on_failure=true`; if any such target remains unresolved, keep all results in the private held checkpoint and emit no new patch artifact.
32. Every distributable package includes `parr0t-release-checkpoint.json` as a machine-readable continuation point. When a prior release and a game folder are supplied, validate that checkpoint first and re-extract the current inventories. A new game build invalidates direct installation compatibility, not every translation: reuse a reviewed target only when its source, location, dependency, target-scoped context/review, and compatible SDK-adapter bindings still match. A user-requested correction invalidates only the named target IDs and their declared reverse dependencies unless the user requests a complete review or the global review policy changed. Implement a verified missing-locale fallback to a base locale, source text, or a nonempty placeholder when the selected SDK can do so safely; otherwise record the fallback as unsupported with a reason and never claim it exists.
33. Follow the repository `docs/segment-metadata.md` for UI composition. When the selected SDK can prove the call-site structure, emit `ui_slot_role`, semantic state, related IDs, render pattern, fixed target affixes, and hard limits. Translate and review only the slot payload; never duplicate surrounding labels or confuse a semantic enabled/disabled state with a literal source token. Before assuming an installed patch is a new game build or repeating broad extraction, run the read-only `game-l10n inspect-installed-state --workspace <this-directory> --target <configured-game-root>`. Treat `possible_game_update` as an advisory receipt-owned-file mismatch, not proof of cause, and stop before mutation if preserved original backups are damaged.
34. When the user supplies or approves exact replacement wording for a typo or small fix, follow `docs/minimal-corrections.md` and run `game-l10n apply-user-correction` against the matching verified inventory and release checkpoint; reuse the full result artifact when available, or materialize released candidates from the checkpoint. Keep the correction in the current agent with zero model/context calls, preserve every unnamed result, repack only the owning payload once, and verify the package once. Do not delegate, reopen discovery, inspect or hash the whole game, re-review unchanged targets, or create one-off correction scripts. A reverse dependency or ambiguous mapping expands only to the specifically affected IDs.
35. Native bulk translation and review use `prepare-native-pass` v2. Run every task in a fresh native worker with no parent conversation history (`fork_turns="none"` or the host equivalent), one task per worker, no follow-up task reuse, and a metadata-only final envelope. Pass only workspace/contract/source/context/task artifact paths in the startup message; never paste source, candidate, inventory, code, or tool output into the conversation. Keep the default 32-ID and 12,000 estimated-material-token task bounds unless a smaller bound is needed. Normalize null speaker, scene, previous, and next values to empty strings before prompting.
36. Treat `.game-l10n/provider-mode.json` as the immutable execution-mode contract. Do not edit, replace, delete, or recreate it to make a different provider path pass. If `game-l10n` reports `E_PROVIDER_MODE_DRIFT` or `E_NATIVE_WORKFLOW_REQUIRED`, stop before reading source text or constructing a prompt and restore the selected mode; do not build a job-local bridge around the guard.

## Data boundary

- Private here: `game-data/`, `inputs/`, `translations/`, `batches/`, `audit/`, `.game-l10n/`, `logs/`.
- Paths in `game_roots` are read-only source locations. Never write patched files into them unless the user explicitly authorizes installation after reviewing the packed output.
- Real install/update/remove/launch operations are denied by default. When explicitly authorized, record the exact action with `game-l10n approve-runtime`; do not create that record merely to unblock a test.
- Reusable SDK only: orchestration source, documentation, tests, and sanitized engine-specific unpack/pack tooling.
- API keys are environment variables only. Never place keys in configuration or logs.
"""

GITIGNORE_TEXT = """# Private game and localization data
game-data/
inputs/
translations/
localized/
batches/
audit/
wolf-json/
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


def inventory_sdk_branches(sdk_root: Path) -> dict[str, Any]:
    """Return an engine-branch inventory without checking out or changing any ref."""

    sdk_root = sdk_root.resolve()
    if not (sdk_root / ".git").exists():
        candidates: list[dict[str, Any]] = []
    else:
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
                }
            )

    fingerprint_payload = json.dumps(
        candidates, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return {
        "source": "local_and_remote_tracking_refs",
        "candidates": candidates,
        "fingerprint": hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
    }


def refresh_sdk_inventory(
    work_dir: Path, *, refresh_remote: bool = False, remote: str = "origin"
) -> dict[str, Any]:
    config = load_config(work_dir)
    sdk_root = Path(config.get("sdk_root") or "").resolve()
    if refresh_remote:
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
    inventory = inventory_sdk_branches(sdk_root)
    previous = config.get("sdk_selection", {})
    candidate_commits = {item["branch"]: item["commit"] for item in inventory["candidates"]}
    evaluations = {
        branch: evaluation
        for branch, evaluation in previous.get("evaluations", {}).items()
        if candidate_commits.get(branch) == evaluation.get("commit")
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


def _write_config(work_dir: Path, config: dict[str, Any]) -> None:
    (work_dir / "game-l10n.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
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
            "translators": ["native-luna", "deepseek"],
            "reviewers": ["native-sol", "deepseek"],
            "rewriters": ["deepseek"],
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
    selection = require_sdk_selection(load_config(work_dir))
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
    if push:
        git("push", "-u", "origin", branch)
    return branch
