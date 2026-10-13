from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .privacy import require_external_workdir, scan_tools


CONFIG_TEMPLATE: dict[str, Any] = {
    "provider_preset": "openai-deepseek",
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
        "native_shard_records": 256,
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
1a. Before writing or adapting format tooling, inspect the candidates recorded by `game-l10n sdk-inventory`, evaluate each plausible public contract with `game-l10n sdk-evaluate`, and record exactly one `reuse`, `extend`, or `private-new` decision with `game-l10n sdk-select`. Reuse is the default; extend a partial match instead of reimplementing it. `private-new` is allowed only after every inventoried candidate is incompatible. The CLI blocks extraction, context, translation, and new-profile promotion until this gate is complete.
2. Never copy source game data, extracted strings, translations, prompts, caches, logs, or credentials into the SDK repository.
3. Run unpack and pack through `game-l10n run-tool`; identical tool/input/environment fingerprints are skipped automatically.
4. Run translation through the orchestrator. The orchestrator only monitors execution/protocol status and fallback routing. It must not replace the independent review agent.
5. Treat model refusals, malformed output, timeouts, and failed integrity checks as route failures. Continue through the configured fallback chain and keep the audit record free of game text.
6. Preserve placeholders, tags, control codes, glossary terms, names, character voice and scene state, UI actions/states/conditions/units, and knowledge order/prerequisites. Preserve communicative function, idiomatic target-language phrasing, supported allusions/wordplay, deliberate alterations, and function-appropriate tone/rhythm. Do not approve a mechanically literal result merely because its denotational meaning is correct, and do not invent a reference from weak resemblance.
7. A toolchain may be promoted only after the user explicitly states that the translation/patch succeeded. Keep the sanitized SDK profile local and stop. Do not propose, ask about, preview, stage, or mention commit, push, repository publication, tags, or releases unless the user first gives an explicit publication command.
8. Engine profiles use branch names `sdk-<engine>-<version>`. Export only reusable files from `tools/`; the privacy scan must pass first.
9. Keep promoted SDK content provenance-free. Never transfer real game/project names, filenames, asset paths, map/event identifiers, dialogue, translations, project-specific exclusions, job metadata, tool paths, counts, hashes, reports, or execution history. Use only public engine-format identifiers and unmistakably synthetic fixtures.
10. Record only recurring engine/SDK problems in the repository `docs/faq.md`, after reducing them to neutral symptom, general cause, diagnosis, resolution, and prevention. Keep one-off or project-specific incidents in this private workspace.
11. Treat translation as a faithful transformation of existing user-provided text, never as new scene generation, continuation, roleplay, or endorsement. Analyze the complete ordered inventory first, using `auto`, `narrative`, `functional`, `knowledge`, or `hybrid` context; give each target only the relevant typed evidence. Do not censor, soften, intensify, or invent content. A native worker may receive an exclusive ID shard to reduce startup overhead, but it must load and finalize one target at a time. A refusal applies only to the individual target segment and must never cancel unrelated segments.
12. Follow the configured role chains, not hard-coded provider identities. Finish the complete primary translation pass before translation fallback. Review with compact judges; invoke configured rewriters only for `rewrite` verdicts; confirm corrected candidates; and send only refused/failed judge targets to the next judge. With the recommended OpenAI/DeepSeek preset these roles map to Luna translation, Sol compact judging, and selective DeepSeek/Sol correction, but API, native App, command, local, and compatible providers may be substituted independently. If every permitted route fails, emit the unchanged source with `source_preserved`; never silently claim it was reviewed.
13. Selecting a provider preset and supplying its API-key environment variables authorizes its automatic context, translation, review, and fallback calls. Do not pause for another per-segment API or cost approval. Keep configured game roots read-only; create translated data, rebuilt archives, and installable patches only under this workspace. Never overwrite an installed or source game in place.
14. Distinguish API providers from native Codex App workers. In the recommended App + DeepSeek mode, use native GPT-6 Luna translator subagents and a separate native GPT-6.1 Sol compact judge; use only `DEEPSEEK_API_KEY` for configured translation fallback, selective rewriting, and judge fallback. This is not a core dependency: API, command, compatible, local, and other native providers may fill each role. The top-level agent remains orchestration-only and follows `docs/codex-app-deepseek.md`.
15. Keep a strict data-plane/control-plane boundary. The top-level agent may receive target IDs, categorical status/error codes, counts, queue transitions, artifact paths, and deterministic issue codes only. It must not open dialogue/input/result files or receive source, candidate, translated text, or provider prose refusal explanations. Workers read and write private workspace files and return metadata-only envelopes.
16. A translation or review refusal is a routing event, not a content judgment and not grounds to omit a target or cancel patch creation. Normalize explanations before returning metadata to the parent. Use the configured faithful-transformation prompt and supported deterministic low-creativity settings for DeepSeek; never claim or attempt to disable provider safeguards.
17. The checked-out `main` branch is an engine-neutral control framework. Engine-specific discovery, unpack, apply, pack, verification, FAQ, ignore rules, and fixtures must come from the selected `sdk-<engine>-<version>` branch and must never be copied back into `main`.
18. Follow the repository `docs/execution-budget.md`. Build the smallest artifact that satisfies the active request. Do not add speculative rollback archives, legacy migrations, compatibility layers, exhaustive backups, or unrelated generalization. After the required focused checks pass, stop unless inputs changed or a concrete failure justifies more testing.
19. Handle simple fixes, packaging, documentation, and status inspection in the current agent. Use subagents only for independent bulk translation/review shards or genuinely parallel research. Do not delegate deterministic rechecks or small installer work.
20. For Steam delivery, use `game-l10n steam-package` with a completed payload and private spec. The generated installer owns only receipt-listed files, skips missing Steam libraries, and falls back to a supplied game directory. Do not embed a previous package or copy Steam/game-specific data into the SDK.
21. Building, fixing, packing, or validating a patch does not authorize installing, updating, removing, or launching it. Access to a game folder is read-only discovery permission only. Do not infer runtime permission from a request to inspect logs, test a build, or confirm execution data.
22. Default to handing the recoverable artifact to the user for execution. If the user explicitly asks the agent to act on a real installation, record a separate short-lived one-shot approval for exactly one of `install`, `update`, `remove`, or `launch`. Bind it to the current artifact digest, exact configured game root, and—only for launch—the exact executable. A changed artifact, target, executable, action, expiry, or consumed approval must stop the operation.
23. Installation approval never authorizes launch, and launch approval never authorizes installation, update, removal, cleanup, log deletion, save modification, or another launch. Consume the matching approval immediately before the external effect. Automated lifecycle tests use temporary synthetic targets; only isolated repository tests may create synthetic approval fixtures for those targets.
24. The public per-game instance workflow is dormant until the user first explicitly commands publication. Before that command, do not mention, propose, ask about, preview, or prepare a publication repository, commit/push, tag, GitHub Release, release gate, or upload. Once activated, follow the repository `docs/instance-release.md`, target the player's execution environment, default to Windows unless another platform is named, and derive architecture and prerequisites from the supported game build. Build once from a clean tagged commit and upload the exact verified bytes; bind the source commit, tag, release ID, package/manifest/file hashes, and embedded versions as one release unit. Record every supported original game version/build and make the README, release notes, manifest, asset name, and installer checks agree. Preserve the user's existing README with minimal reviewed edits. Use the visible title `<Game Display Name> Patch <Patch Version> (Game Version <Game Version>)`, or the exact enumerated `Game Versions` form.
25. Player-facing Windows installers follow the repository `docs/installer-contract.md`, not this agent harness. Keep the fixed ZIP layout and the public Install, Restore, and Verify installed-state operations. Use `<game-root>/parr0t-patch-status.txt` as the first patch-ID authority and the matching receipt for owned files. Explicitly declare any original file that must be replaced; preserve it once, verify and reuse that backup on update, and put it back on Restore. Core result codes are fixed; SDK/title-specific failures use a separate `PARR0T-<SCOPE>-E###` registry. Never copy `AGENTS.md`, approval JSON, prompts, task state, logs, or workspace policy into a distribution. An interactive player's menu choice plus final confirmation needs no agent approval; noninteractive mutation still does.
26. For Steam discovery or integration, follow the repository `docs/steam-faq.md` and `docs/wrapper-executable.md`. Use exact App ID metadata and binding checks and ask when the validated game root is ambiguous. Never read or edit `localconfig.vdf`, Steam launch options, or per-account client configuration, and never require, close, kill, or restart Steam. Integrate the Steam Play button only with exact approved, reproducible, signature/hash-verified Parr0t wrapper bytes for the target architecture at explicitly declared main-executable paths; preserve each real original through `replace_files` and do not use `.lnk` or an ad hoc game-workspace binary. Require the target game to be closed for locked replacements; close only the validated game process after separate player confirmation.
27. The control plane must proactively use the repository `skills/request-scope-guard/SKILL.md` whenever the user limits or narrows scope, asks for read-only inspection/diagnosis, or a contemplated action is optional, unrequested, destructive, or externally consequential. Prefer a discovered `$request-scope-guard`; otherwise read and apply the bundled file directly. Re-evaluate after every user message and give workers only the exact authorized targets and side effects. No worker may infer permission to expand scope, clean up data, commit/push, publish, deploy, install, launch, or contact an external system.
28. Do not start context analysis or translation from one extractor's manifest alone. Produce a primary and an independently implemented ordered inventory using the selected SDK, declare user-visible exclusions, and pass `game-l10n verify-inventory`. `manifest_missing=0` describes only result alignment; it must never be reported as zero in-game omissions. Runtime completeness is a separate state and remains `not_run` until metadata from a separately authorized or user-executed runtime observation is recorded.
29. Context analysis is complete only when the CLI records a successful per-part execution attestation and evidence coverage gate. A partial artifact, interrupted worker, capacity error, or file left by a failed route cannot unblock translation. Route operational failures through the configured context fallback; if every route fails, stop.
30. Reusing an existing candidate proves neither quality nor review. When the user requests a complete re-review, run `game-l10n review-existing` so every current target receives a fresh independent semantic verdict under the current context. Do not review only automatically flagged IDs and do not describe identifier, digest, or structure checks as semantic review.
31. Capacity, timeout, rate-limit, quota, authentication, malformed-response, and other operational failures enter the metadata-only failure queue. Do not retry that queue after the turn ends until the user explicitly requests resumption. Extractors mark indispensable targets with `metadata.blocking_on_failure=true`; if any such target remains unresolved, keep all results in the private held checkpoint and emit no new patch artifact.

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
    "incompatible": {
        "engine_mismatch",
        "format_mismatch",
        "version_incompatible",
        "missing_required_capability",
        "uninspectable_contract",
    },
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
) -> dict[str, Any]:
    config = load_config(work_dir)
    selection = config.get("sdk_selection", {})
    candidates = {item["branch"]: item for item in selection.get("candidates", [])}
    if branch not in candidates:
        raise ConfigurationError("SDK branch is not present in the current inventory; refresh it first")
    allowed_reasons = SDK_EVALUATION_REASONS.get(result)
    if allowed_reasons is None:
        raise ConfigurationError("SDK evaluation result must be compatible, partial, or incompatible")
    if reason not in allowed_reasons:
        raise ConfigurationError(f"Reason {reason!r} is not valid for SDK evaluation result {result!r}")
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
    }
    selection["status"] = "pending"
    selection["decision"] = None
    _write_config(work_dir, config)
    return evaluations[branch]


def record_sdk_selection(
    work_dir: Path, mode: str, branch: str | None = None
) -> dict[str, Any]:
    config = load_config(work_dir)
    selection = config.get("sdk_selection", {})
    if not selection.get("inventory_fingerprint"):
        raise ConfigurationError("SDK inventory is missing; run sdk-inventory first")
    candidates = {item["branch"]: item for item in selection.get("candidates", [])}
    evaluations = selection.get("evaluations", {})

    if mode in {"reuse", "extend"}:
        if not branch or branch not in candidates:
            raise ConfigurationError("reuse and extend decisions require an inventoried SDK branch")
        evaluation = evaluations.get(branch, {})
        allowed_results = {"compatible"} if mode == "reuse" else {"compatible", "partial"}
        if evaluation.get("result") not in allowed_results:
            raise ConfigurationError(f"SDK branch must be evaluated before selecting mode {mode}")
    elif mode == "private-new":
        if branch:
            raise ConfigurationError("private-new does not accept a branch")
        unevaluated = sorted(set(candidates) - set(evaluations))
        reusable = sorted(
            name
            for name, evaluation in evaluations.items()
            if evaluation.get("result") != "incompatible"
        )
        uninspectable = sorted(
            name
            for name, evaluation in evaluations.items()
            if evaluation.get("reason") == "uninspectable_contract"
        )
        if unevaluated:
            raise ConfigurationError(
                "Every SDK candidate must be evaluated before private-new: " + ", ".join(unevaluated)
            )
        if reusable:
            raise ConfigurationError(
                "A compatible or extensible SDK exists; reuse or extend it instead: " + ", ".join(reusable)
            )
        if uninspectable:
            raise ConfigurationError(
                "Uninspectable SDK branches block private-new until access is resolved: "
                + ", ".join(uninspectable)
            )
    else:
        raise ConfigurationError("SDK selection mode must be reuse, extend, or private-new")

    decision = {
        "mode": mode,
        "branch": branch,
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
            "SDK selection is incomplete; inventory, evaluate, and select an existing branch or private-new first"
        )
    if decision.get("inventory_fingerprint") != selection.get("inventory_fingerprint"):
        raise ConfigurationError("SDK inventory changed after selection; refresh and select again")
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
    provider_preset: str = "openai-deepseek",
) -> None:
    work_dir = work_dir.resolve()
    sdk_root = sdk_root.resolve()
    require_external_workdir(work_dir, sdk_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(json.dumps(CONFIG_TEMPLATE))
    if provider_preset not in {"openai-deepseek", "custom"}:
        raise ConfigurationError("provider_preset must be openai-deepseek or custom")
    config["provider_preset"] = provider_preset
    if provider_preset == "custom":
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
    files = {
        "game-l10n.json": json.dumps(config, ensure_ascii=False, indent=2) + "\n",
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
    return json.loads(path.read_text(encoding="utf-8"))


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
    if selection.get("mode") != "private-new":
        raise ConfigurationError(
            "An existing SDK branch was selected; reuse or extend that branch instead of creating a duplicate profile"
        )
    safe_engine = "-".join(part for part in engine.lower().replace("_", "-").split("-") if part.isalnum())
    safe_version = "-".join(part for part in version.lower().replace("_", "-").split("-") if part.replace(".", "").isalnum())
    if not safe_engine or not safe_version:
        raise ConfigurationError("engine and version must form a safe profile name")
    profile_name = f"sdk-{safe_engine}-{safe_version}"
    tool_root = work_dir / "tools"
    manifest = scan_tools(tool_root)
    export_root = work_dir / "sdk-export" / profile_name
    if export_root.exists():
        shutil.rmtree(export_root)
    shutil.copytree(tool_root, export_root / "tools")
    profile = {
        "profile": profile_name,
        "engine": engine,
        "version": version,
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
    branch = profile_data["profile"]
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
