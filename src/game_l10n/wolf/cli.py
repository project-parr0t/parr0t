from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..coverage import save_inventory_attestation
from ..privacy import is_within, require_external_workdir
from ..workspace import load_config
from .batches import (
    apply_batches,
    export_manual_queue,
    import_reviewed_results,
    merge_manual_queue,
    prepare_batches,
)
from .inventory import require_wolf_patch_gate, write_inventory_pair
from .runtime import audit_runtime_scripts, protect_runtime_identifiers
from .tools import decompile_runtime_scripts, pack_pro_archives


def _workspace(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    work_dir = Path(args.workspace).resolve()
    config = load_config(work_dir)
    sdk_root = Path(config["sdk_root"]).resolve()
    require_external_workdir(work_dir, sdk_root)
    return work_dir, config


def _work_path(work_dir: Path, value: str, *, must_exist: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = work_dir / path
    path = path.resolve()
    if not is_within(path, work_dir):
        raise ValueError(f"WOLF work path must stay inside the agent workspace: {path}")
    if must_exist and not path.exists():
        raise FileNotFoundError(path)
    return path


def _source_path(work_dir: Path, config: dict[str, Any], value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = work_dir / path
    path = path.resolve()
    roots = [work_dir, *(Path(item).resolve() for item in config.get("game_roots", []))]
    if not any(path == root or is_within(path, root) for root in roots):
        raise ValueError(f"WOLF source path is outside the workspace and configured game roots: {path}")
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def _tool_command(value: str) -> list[str]:
    candidate = Path(value)
    if candidate.is_file():
        return [str(candidate.resolve())]
    resolved = shutil.which(value)
    if resolved:
        return [resolved]
    raise FileNotFoundError(f"Tool executable not found: {value}")


def cmd_prepare_batches(args: argparse.Namespace) -> int:
    work_dir, _ = _workspace(args)
    source = _work_path(work_dir, args.source, must_exist=True)
    batches = _work_path(work_dir, args.output)
    result = prepare_batches(
        source,
        batches,
        max_chars=args.max_chars,
        translated_copy=_work_path(work_dir, args.copy_to) if args.copy_to else None,
        excluded_json_names=args.exclude_json,
    )
    primary, independent = write_inventory_pair(
        source,
        batches,
        _work_path(work_dir, args.primary_inventory),
        _work_path(work_dir, args.independent_inventory),
        excluded_json_names=args.exclude_json,
    )
    inventory = save_inventory_attestation(work_dir, primary, independent)
    result["inventory"] = {
        "primary": str(primary),
        "independent": str(independent),
        "status": inventory["status"],
        "issue_codes": inventory["issue_codes"],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if inventory["static_complete"] else 1


def cmd_import_reviewed(args: argparse.Namespace) -> int:
    work_dir, _ = _workspace(args)
    primary = _work_path(work_dir, args.segments, must_exist=True)
    require_wolf_patch_gate(work_dir, primary)
    result = import_reviewed_results(
        _work_path(work_dir, args.reviewed, must_exist=True),
        _work_path(work_dir, args.batches, must_exist=True),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_apply_batches(args: argparse.Namespace) -> int:
    work_dir, _ = _workspace(args)
    require_wolf_patch_gate(
        work_dir, _work_path(work_dir, args.segments, must_exist=True)
    )
    result = apply_batches(
        _work_path(work_dir, args.batches, must_exist=True),
        _work_path(work_dir, args.translated_json, must_exist=True),
        allow_incomplete=args.allow_incomplete,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_manual_export(args: argparse.Namespace) -> int:
    work_dir, _ = _workspace(args)
    result = export_manual_queue(
        _work_path(work_dir, args.batches, must_exist=True),
        _work_path(work_dir, args.output),
        sample_path=_work_path(work_dir, args.sample_output) if args.sample_output else None,
    )
    print(json.dumps({"manual_items": result["item_count"], "output": args.output}, ensure_ascii=False))
    return 0


def cmd_manual_merge(args: argparse.Namespace) -> int:
    work_dir, _ = _workspace(args)
    result = merge_manual_queue(
        _work_path(work_dir, args.queue, must_exist=True),
        _work_path(work_dir, args.batches, must_exist=True),
        report_path=_work_path(work_dir, args.report) if args.report else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_protect_runtime(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    audit_root = _work_path(work_dir, args.audit_dir)
    scripts = decompile_runtime_scripts(
        _tool_command(args.wolfdawn),
        _source_path(work_dir, config, args.original_data),
        audit_root / "original",
    )
    result = protect_runtime_identifiers(
        _work_path(work_dir, args.translated_json, must_exist=True),
        scripts,
        report_path=audit_root / "runtime-key-repairs.json",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_audit_runtime(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    audit_root = _work_path(work_dir, args.audit_dir)
    original_data = _source_path(work_dir, config, args.original_data)
    localized_data = _work_path(work_dir, args.localized_data, must_exist=True)
    command = _tool_command(args.wolfdawn)
    original_scripts = decompile_runtime_scripts(command, original_data, audit_root / "original")
    localized_scripts = decompile_runtime_scripts(command, localized_data, audit_root / "localized")
    result = audit_runtime_scripts(
        original_scripts,
        localized_scripts,
        original_text_root=original_data / "Text_Script",
        localized_text_root=localized_data / "Text_Script",
        report_path=audit_root / "runtime-audit.json",
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


def cmd_pack_pro(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    require_wolf_patch_gate(
        work_dir, _work_path(work_dir, args.segments, must_exist=True)
    )
    results = pack_pro_archives(
        _tool_command(args.packer),
        _work_path(work_dir, args.built_data, must_exist=True),
        _source_path(work_dir, config, args.game_archive),
        _work_path(work_dir, args.output),
    )
    print(json.dumps({"archives": [str(path) for path in results]}, ensure_ascii=False, indent=2))
    return 0


def register_wolf_parser(subparsers: argparse._SubParsersAction) -> None:
    wolf = subparsers.add_parser("wolf", help="WOLF RPG/WolfDawn extraction and integrity workflow")
    commands = wolf.add_subparsers(dest="wolf_command", required=True)

    prepare = commands.add_parser("prepare-batches", help="Create stable agent batches from extracted WOLF JSON")
    prepare.add_argument("--workspace", required=True)
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--copy-to", help="Optional untouched copy used as the translated JSON tree")
    prepare.add_argument("--max-chars", type=int, default=9000)
    prepare.add_argument("--primary-inventory", default="inputs/wolf-primary.json")
    prepare.add_argument(
        "--independent-inventory", default="inputs/wolf-independent.json"
    )
    prepare.add_argument(
        "--exclude-json",
        action="append",
        default=[],
        help="Workspace-only extracted JSON filename to exclude; never copy private names into the SDK",
    )
    prepare.set_defaults(func=cmd_prepare_batches)

    import_reviewed = commands.add_parser(
        "import-reviewed",
        help="Import the complete reviewed result set into stable WOLF batches",
    )
    import_reviewed.add_argument("--workspace", required=True)
    import_reviewed.add_argument("--reviewed", required=True)
    import_reviewed.add_argument("--batches", required=True)
    import_reviewed.add_argument("--segments", default="inputs/wolf-primary.json")
    import_reviewed.set_defaults(func=cmd_import_reviewed)

    apply = commands.add_parser("apply-batches", help="Validate and apply completed batches to WOLF JSON")
    apply.add_argument("--workspace", required=True)
    apply.add_argument("--batches", required=True)
    apply.add_argument("--translated-json", required=True)
    apply.add_argument("--segments", default="inputs/wolf-primary.json")
    apply.add_argument("--allow-incomplete", action="store_true")
    apply.set_defaults(func=cmd_apply_batches)

    manual_export = commands.add_parser("manual-export", help="Export manual items and an optional same-format sample")
    manual_export.add_argument("--workspace", required=True)
    manual_export.add_argument("--batches", required=True)
    manual_export.add_argument("--output", required=True)
    manual_export.add_argument("--sample-output")
    manual_export.set_defaults(func=cmd_manual_export)

    manual_merge = commands.add_parser("manual-merge", help="Validate and merge a completed manual queue")
    manual_merge.add_argument("--workspace", required=True)
    manual_merge.add_argument("--queue", required=True)
    manual_merge.add_argument("--batches", required=True)
    manual_merge.add_argument("--report")
    manual_merge.set_defaults(func=cmd_manual_merge)

    protect = commands.add_parser("protect-runtime", help="Restore translated WOLF control keys and resource paths")
    protect.add_argument("--workspace", required=True)
    protect.add_argument("--wolfdawn", required=True)
    protect.add_argument("--original-data", required=True)
    protect.add_argument("--translated-json", required=True)
    protect.add_argument("--audit-dir", default="audit/wolf")
    protect.set_defaults(func=cmd_protect_runtime)

    audit = commands.add_parser("audit-runtime", help="Compare original and rebuilt WScript plus text commands")
    audit.add_argument("--workspace", required=True)
    audit.add_argument("--wolfdawn", required=True)
    audit.add_argument("--original-data", required=True)
    audit.add_argument("--localized-data", required=True)
    audit.add_argument("--audit-dir", default="audit/wolf")
    audit.set_defaults(func=cmd_audit_runtime)

    pack = commands.add_parser("pack-pro", help="Pack all replacement archives with one WOLF Pro key archive")
    pack.add_argument("--workspace", required=True)
    pack.add_argument("--packer", required=True)
    pack.add_argument("--built-data", required=True)
    pack.add_argument("--game-archive", required=True, help="Original Data/Game.wolf used by the external packer")
    pack.add_argument("--output", required=True)
    pack.add_argument("--segments", default="inputs/wolf-primary.json")
    pack.set_defaults(func=cmd_pack_pro)
