from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..privacy import is_within, require_external_workdir
from ..workspace import load_config
from .adapter import apply_translations, extract_game, pack_patch, verify_patch


def _workspace(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    sdk_root = Path(config.get("sdk_root") or Path(__file__).resolve().parents[3]).resolve()
    require_external_workdir(work_dir, sdk_root)
    return work_dir, config


def _work_path(work_dir: Path, value: str, *, must_exist: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = work_dir / path
    path = path.resolve()
    if path == work_dir or not is_within(path, work_dir):
        raise ValueError(f"MV artifact path must stay below the agent workspace: {path}")
    if must_exist and not path.exists():
        raise ValueError(f"MV artifact path does not exist: {path}")
    return path


def _game_root(work_dir: Path, config: dict[str, Any], value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = work_dir / path
    path = path.resolve()
    roots = [Path(item).resolve() for item in config.get("game_roots", [])]
    if not roots or not any(path == root or is_within(path, root) for root in roots):
        raise ValueError("MV source must be a configured read-only game root")
    if not path.is_dir():
        raise ValueError(f"MV game root does not exist: {path}")
    return path


def _write_report(path: Path | None, report: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_extract(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    report = extract_game(
        _game_root(work_dir, config, args.game_root),
        _work_path(work_dir, args.output),
    )
    _write_report(_work_path(work_dir, args.report) if args.report else None, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    report = apply_translations(
        _work_path(work_dir, args.manifest, must_exist=True),
        _work_path(work_dir, args.results, must_exist=True),
        _game_root(work_dir, config, args.game_root),
        _work_path(work_dir, args.output),
        preserve_missing=not args.require_all_results,
        preserve_invalid=not args.fail_on_invalid,
    )
    _write_report(_work_path(work_dir, args.report) if args.report else None, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    report = verify_patch(
        _work_path(work_dir, args.manifest, must_exist=True),
        _game_root(work_dir, config, args.game_root),
        _work_path(work_dir, args.patch, must_exist=True),
    )
    _write_report(_work_path(work_dir, args.report) if args.report else None, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    patch = _work_path(work_dir, args.patch, must_exist=True)
    verification = verify_patch(
        _work_path(work_dir, args.manifest, must_exist=True),
        _game_root(work_dir, config, args.game_root),
        patch,
    )
    report = pack_patch(
        patch,
        _work_path(work_dir, args.output),
    )
    report["verification"] = verification["status"]
    _write_report(_work_path(work_dir, args.report) if args.report else None, report)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def register_rpg_maker_mv_parser(subparsers: argparse._SubParsersAction) -> None:
    mv = subparsers.add_parser(
        "rpg-maker-mv",
        help="RPG Maker MV 1.6.1 extraction, patching, and verification",
    )
    commands = mv.add_subparsers(dest="rpg_maker_mv_command", required=True)

    extract = commands.add_parser("extract", help="Extract ordered text and an immutable apply manifest")
    extract.add_argument("--workspace", required=True)
    extract.add_argument("--game-root", required=True)
    extract.add_argument("--output", required=True)
    extract.add_argument("--report")
    extract.set_defaults(func=cmd_extract)

    build = commands.add_parser("build", help="Build a separate validated JSON patch tree")
    build.add_argument("--workspace", required=True)
    build.add_argument("--game-root", required=True)
    build.add_argument("--manifest", required=True)
    build.add_argument("--results", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--report")
    build.add_argument(
        "--require-all-results",
        action="store_true",
        help="Fail instead of preserving source for missing or unsuccessful results",
    )
    build.add_argument(
        "--fail-on-invalid",
        action="store_true",
        help="Fail instead of preserving source for deterministic integrity violations",
    )
    build.set_defaults(func=cmd_build)

    verify = commands.add_parser("verify", help="Reparse output and reject changes outside manifest paths")
    verify.add_argument("--workspace", required=True)
    verify.add_argument("--game-root", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--patch", required=True)
    verify.add_argument("--report")
    verify.set_defaults(func=cmd_verify)

    pack = commands.add_parser("pack", help="Verify a patch tree and create a deterministic ZIP")
    pack.add_argument("--workspace", required=True)
    pack.add_argument("--game-root", required=True)
    pack.add_argument("--manifest", required=True)
    pack.add_argument("--patch", required=True)
    pack.add_argument("--output", required=True)
    pack.add_argument("--report")
    pack.set_defaults(func=cmd_pack)
