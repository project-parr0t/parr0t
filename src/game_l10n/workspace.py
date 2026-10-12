from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .privacy import require_external_workdir, scan_tools


CONFIG_TEMPLATE: dict[str, Any] = {
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
    ],
    "routing": {
        "context_analyzers": ["translator-primary"],
        "translators": ["translator-primary"],
        "reviewers": ["reviewer-primary"],
        "require_review": True,
        "max_workers": 1,
        "retry_refusals_once": True,
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
8. Engine profiles use branch names `sdk-<engine>-<version>` (for example `sdk-unity-2023.1`). Export only reusable files from `tools/`; the privacy scan must pass first.
9. Keep promoted SDK content provenance-free. Never transfer real game/project names, filenames, asset paths, map/event identifiers, dialogue, translations, project-specific exclusions, job metadata, tool paths, counts, hashes, reports, or execution history. Use only public engine-format identifiers and unmistakably synthetic fixtures.
10. Record only recurring engine/SDK problems in the repository `docs/faq.md`, after reducing them to neutral symptom, general cause, diagnosis, resolution, and prevention. Keep one-off or project-specific incidents in this private workspace.

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


def initialize_workspace(
    work_dir: Path,
    sdk_root: Path,
    *,
    git_choice: str = "pending",
    game_roots: list[Path] | None = None,
) -> None:
    work_dir = work_dir.resolve()
    sdk_root = sdk_root.resolve()
    require_external_workdir(work_dir, sdk_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    config = json.loads(json.dumps(CONFIG_TEMPLATE))
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
