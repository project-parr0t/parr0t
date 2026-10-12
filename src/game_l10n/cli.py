from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

from .errors import GameL10nError
from .context_analysis import ContextAnalyzer, analyze_with_cache
from .formats import read_segments, write_results
from .models import CharacterProfile, ContextPack, GlossaryEntry, ProjectContext
from .native_pass import import_native_shard, merge_native_pass
from .orchestrator import LocalizationOrchestrator, OrchestratorConfig
from .privacy import is_within, require_external_workdir
from .providers import ProviderRegistry
from .tooling import run_cached_tool
from .workspace import (
    initialize_workspace,
    load_config,
    prepare_sdk_profile,
    publish_sdk_profile,
    update_repository_policy,
)


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
    work_dir: Path, segments: list, *, force: bool = False
) -> tuple[ContextPack, bool]:
    config = load_config(work_dir)
    routing = config["routing"]
    chain = list(routing.get("context_analyzers") or routing["translators"])
    registry = ProviderRegistry.from_config(config, work_dir)
    analyzer = ContextAnalyzer(
        registry,
        chain,
        max_chunk_chars=int(routing.get("context_chunk_chars", 30000)),
    )
    context_relative = config.get("files", {}).get("context_pack", "context/context-pack.json")
    return analyze_with_cache(
        analyzer=analyzer,
        segments=segments,
        output_path=_path_in_workspace(work_dir, context_relative),
        state_path=work_dir / ".game-l10n" / "context-state.json",
        force=force,
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
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    destination = _path_in_workspace(work_dir, args.output)
    if destination == source:
        raise ValueError("Translation output must be a separate patch file; the source is read-only")
    segments = read_segments(source)
    context_source = _path_in_workspace(
        work_dir, args.context_input or args.input, must_exist=True
    )
    context_pack, context_cached = _analyze_context(
        work_dir, read_segments(context_source), force=args.force_context
    )
    orchestrator = _orchestrator(work_dir, context_pack)
    results = orchestrator.translate_many(segments, force=args.force)
    write_results(destination, results)
    failed = sum(item.status == "failed" for item in results)
    source_preserved = sum(item.status == "source_preserved" for item in results)
    cached = sum(item.from_cache for item in results)
    print(
        json.dumps(
            {
                "segments": len(results),
                "failed": failed,
                "source_preserved": source_preserved,
                "cached": cached,
                "context_cached": context_cached,
                "output": str(destination),
            }
        )
    )
    return 2 if failed else 0


def cmd_analyze_context(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    context_pack, cached = _analyze_context(work_dir, read_segments(source), force=args.force)
    config = load_config(work_dir)
    output = _path_in_workspace(
        work_dir, config.get("files", {}).get("context_pack", "context/context-pack.json")
    )
    print(
        json.dumps(
            {
                "cached": cached,
                "output": str(output),
                "scenes": len(context_pack.scene_summaries),
                "characters": len(context_pack.character_guides),
            }
        )
    )
    return 0


def cmd_run_tool(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
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
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    result = import_native_shard(work_dir, source, args.name)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_merge_native_pass(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    source = _path_in_workspace(work_dir, args.input, must_exist=True)
    destination = _path_in_workspace(work_dir, args.output)
    result = merge_native_pass(work_dir, read_segments(source), destination)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["complete"] else 1


def cmd_status(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    actions: list[str] = []
    policy = config.get("repository", {})
    if policy.get("initial_choice") == "pending":
        actions.append("ask_user_for_initial_git_choice")
    game_roots = [Path(item) for item in config.get("game_roots", [])]
    missing_roots = [str(path) for path in game_roots if not path.exists()]
    if not game_roots:
        actions.append("configure_game_root")
    elif missing_roots:
        actions.append("fix_missing_game_root")
    if not any((work_dir / "inputs").glob("*")):
        actions.append("unpack_and_extract_ordered_script")
    context_path = _path_in_workspace(
        work_dir, config.get("files", {}).get("context_pack", "context/context-pack.json")
    )
    if not context_path.exists():
        actions.append("analyze_complete_script_context")
    print(
        json.dumps(
            {
                "workspace": str(work_dir),
                "sdk_root": config.get("sdk_root"),
                "game_roots": [str(path) for path in game_roots],
                "missing_game_roots": missing_roots,
                "repository": policy,
                "next_actions": actions or ["ready"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if missing_roots else 0


def cmd_promote(args: argparse.Namespace) -> int:
    work_dir = Path(args.workspace).resolve()
    export_root = prepare_sdk_profile(
        work_dir,
        args.engine,
        args.version,
        user_confirmed_success=args.user_confirmed_success,
    )
    print(f"Sanitized profile prepared: {export_root}")
    print("ACTION_REQUIRED: ask the user whether it may be committed/pushed, then record approval with `game-l10n policy`.")
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
    parser = argparse.ArgumentParser(prog="game-l10n", description="Refusal-aware game localization agent SDK")
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
        choices=["openai-deepseek", "custom"],
        default="openai-deepseek",
        help="Ready-to-run OpenAI/DeepSeek routing or a manually configured provider chain",
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
        help="Full ordered script used for mandatory narrative analysis; defaults to --input",
    )
    translate.add_argument("--output", required=True)
    translate.add_argument("--force", action="store_true")
    translate.add_argument("--force-context", action="store_true")
    translate.set_defaults(func=cmd_translate)

    context = sub.add_parser("analyze-context", help="Analyze the complete script before translation")
    context.add_argument("--workspace", required=True)
    context.add_argument("--input", required=True)
    context.add_argument("--force", action="store_true")
    context.set_defaults(func=cmd_analyze_context)

    tool = sub.add_parser("run-tool", help="Run cached unpack/pack tooling")
    tool.add_argument("stage", choices=["unpack", "pack"])
    tool.add_argument("--workspace", required=True)
    tool.add_argument("--input", required=True)
    tool.add_argument("--output", required=True)
    tool.add_argument("--force", action="store_true")
    tool.set_defaults(func=cmd_run_tool)

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

    doctor = sub.add_parser("doctor", help="Check workspace configuration without sending game data")
    doctor.add_argument("--workspace", required=True)
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (GameL10nError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
