from __future__ import annotations

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
            "temperature": 0.0,
            "thinking": "disabled",
            "reasoning_effort": "none",
            "max_tokens_per_source_char": 4.0,
            "min_output_tokens": 128,
            "max_output_tokens": 8192,
        },
        {
            "name": "openai-sol",
            "type": "openai_responses",
            "model": "gpt-6.1-sol",
            "api_key_env": "OPENAI_API_KEY",
            "reasoning_effort": "low",
            "max_output_tokens": 8192,
        },
    ],
    "routing": {
        "context_analyzers": ["openai-sol", "deepseek"],
        "translators": ["openai-luna", "deepseek"],
        "reviewers": ["openai-sol", "deepseek"],
        "require_review": True,
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
    "repository": {"initial_choice": "pending", "publish_approved": False},
}

AGENTS_TEXT = """# AGENTS.md — Game localization workspace

This directory contains private game data and translation artifacts. It is intentionally separate from the SDK repository.

## Mandatory workflow

1. Before any `git init`, remote creation, branch creation, commit, or push, ask the user whether Git use is allowed for this game/tooling task. Record the answer in `game-l10n.json` under `repository.initial_choice`. If refused, do not initialize Git. Run `game-l10n status --workspace <this-directory>` first; it reports the next safe action without sending data.
2. Never copy source game data, extracted strings, translations, prompts, caches, logs, or credentials into the SDK repository.
3. Run unpack and pack through `game-l10n run-tool`; identical tool/input/environment fingerprints are skipped automatically.
4. Run translation through the orchestrator. The orchestrator only monitors execution/protocol status and fallback routing. It must not replace the independent review agent.
5. Treat model refusals, malformed output, timeouts, and failed integrity checks as route failures. Continue through the configured fallback chain and keep the audit record free of game text.
6. Preserve placeholders, tags, control codes, glossary terms, names, character voice, and scene context.
7. A toolchain may be promoted only after the user explicitly states that the translation/patch succeeded. Then ask separately whether the sanitized SDK profile may be committed/pushed.
8. Engine profiles use branch names `sdk-<engine>-<version>`. Export only reusable files from `tools/`; the privacy scan must pass first.
9. Keep promoted SDK content provenance-free. Never transfer real game/project names, filenames, asset paths, map/event identifiers, dialogue, translations, project-specific exclusions, job metadata, tool paths, counts, hashes, reports, or execution history. Use only public engine-format identifiers and unmistakably synthetic fixtures.
10. Record only recurring engine/SDK problems in the repository `docs/faq.md`, after reducing them to neutral symptom, general cause, diagnosis, resolution, and prevention. Keep one-off or project-specific incidents in this private workspace.
11. Treat translation as a faithful transformation of existing user-provided text, never as new scene generation, continuation, roleplay, or endorsement. Do not censor, soften, intensify, or invent content. A refusal applies only to the individual target segment and must never cancel unrelated segments.
12. With the `openai-deepseek` preset, finish the complete GPT-6 Luna pass first, queue only refused/failed segments for DeepSeek, then finish the GPT-6.1 Sol review pass and queue only refused/failed reviews for DeepSeek. If every permitted route fails, emit the unchanged source with `source_preserved`; never silently claim it was reviewed.
13. Selecting a provider preset and supplying its API-key environment variables authorizes its automatic context, translation, review, and fallback calls. Do not pause for another per-segment API or cost approval. Keep configured game roots read-only; create translated data, rebuilt archives, and installable patches only under this workspace. Never overwrite an installed or source game in place.
14. Distinguish OpenAI API + DeepSeek API from Codex App + DeepSeek API. In App mode, use native GPT-6 Luna translator subagents and a separate native GPT-6.1 Sol reviewer, with only `DEEPSEEK_API_KEY` used for external fallback. The top-level Sol agent remains orchestration-only and follows the SDK's `docs/codex-app-deepseek.md` workflow.
15. Keep a strict data-plane/control-plane boundary. The top-level agent may receive target IDs, categorical status/error codes, counts, queue transitions, artifact paths, and deterministic issue codes only. It must not open dialogue/input/result files or receive source, candidate, translated text, or provider prose refusal explanations. Workers read and write private workspace files and return metadata-only envelopes.
16. A translation or review refusal is a routing event, not a content judgment and not grounds to omit a target or cancel patch creation. Normalize explanations before returning metadata to the parent. Use the configured faithful-transformation prompt and supported deterministic low-creativity settings for DeepSeek; never claim or attempt to disable provider safeguards.
17. The checked-out `main` branch is an engine-neutral control framework. Engine-specific discovery, unpack, apply, pack, verification, FAQ, ignore rules, and fixtures must come from the selected `sdk-<engine>-<version>` branch and must never be copied back into `main`.

## Data boundary

- Private here: `game-data/`, `inputs/`, `translations/`, `batches/`, `audit/`, `.game-l10n/`, `logs/`.
- Paths in `game_roots` are read-only source locations. Never write patched files into them unless the user explicitly authorizes installation after reviewing the packed output.
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
            "require_review": True,
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
            policy["publish_approved"] = False
    if publish_approved is not None:
        if publish_approved and policy.get("initial_choice") != "enabled":
            raise ConfigurationError("Publishing cannot be approved unless Git use was enabled initially")
        policy["publish_approved"] = publish_approved
    (work_dir / "game-l10n.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def prepare_sdk_profile(work_dir: Path, engine: str, version: str, *, user_confirmed_success: bool) -> Path:
    if not user_confirmed_success:
        raise ConfigurationError("The user must explicitly confirm a successful translation/patch before promotion")
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
        "next_step": (
            "Ask the user whether this sanitized profile may be committed/pushed. "
            f"If approved, create branch {profile_name}."
        ),
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
