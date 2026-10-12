from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..privacy import is_within, require_external_workdir
from ..workspace import load_config
from .build import DEFAULT_REFERENCES, build_managed_mod
from .bundle import build_patch_bundle, verify_patch_bundle
from .discovery import discover_unity_installation
from .payload import compile_runtime_payload


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
        raise ValueError(f"Unity work path must stay inside the agent workspace: {path}")
    if must_exist and not path.exists():
        raise FileNotFoundError(path)
    return path


def _source_root(work_dir: Path, config: dict[str, Any], value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = work_dir / path
    path = path.resolve()
    roots = [Path(item).resolve() for item in config.get("game_roots", [])]
    if not any(path == root for root in roots):
        raise ValueError("Unity game root must exactly match a configured read-only game root")
    if not path.is_dir():
        raise FileNotFoundError(path)
    return path


def _managed_root(work_dir: Path, config: dict[str, Any], value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = work_dir / path
    path = path.resolve()
    roots = [Path(item).resolve() for item in config.get("game_roots", [])]
    if not any(is_within(path, root) for root in roots):
        raise ValueError("Managed references must come from a configured read-only game root")
    if not path.is_dir():
        raise FileNotFoundError(path)
    return path


def cmd_discover(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    installation = discover_unity_installation(_source_root(work_dir, config, args.game_root))
    output = _work_path(work_dir, args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(installation.to_dict(), indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "engine": "unity",
                "series": installation.unity_series,
                "backend": installation.backend,
                "managed_mod_build": installation.supports_managed_mod_build,
                "capabilities": installation.capabilities,
                "loader_roots": len(installation.loader_roots),
                "output": str(output),
            }
        )
    )
    return 0 if installation.supports_managed_mod_build else 1


def cmd_compile_payload(args: argparse.Namespace) -> int:
    work_dir, _ = _workspace(args)
    result = compile_runtime_payload(
        _work_path(work_dir, args.segments, must_exist=True),
        _work_path(work_dir, args.reviewed, must_exist=True),
        _work_path(work_dir, args.output),
        locale=args.locale,
        allow_unreviewed=args.allow_unreviewed,
    )
    print(json.dumps(result))
    return 0


def cmd_build_managed(args: argparse.Namespace) -> int:
    work_dir, config = _workspace(args)
    sources = [_work_path(work_dir, value, must_exist=True) for value in args.source]
    references = list(dict.fromkeys([*DEFAULT_REFERENCES, *args.reference]))
    result = build_managed_mod(
        sources,
        _managed_root(work_dir, config, args.managed_root),
        _work_path(work_dir, args.output),
        reference_names=references,
        defines=args.define,
        dotnet=args.dotnet,
    )
    print(json.dumps(result))
    return 0


def cmd_pack(args: argparse.Namespace) -> int:
    work_dir, _ = _workspace(args)
    result = build_patch_bundle(
        _work_path(work_dir, args.spec, must_exist=True),
        work_dir,
        _work_path(work_dir, args.output),
    )
    print(json.dumps(result))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    work_dir, _ = _workspace(args)
    archive = _work_path(work_dir, args.archive, must_exist=True)
    manifest_value = args.manifest or (str(archive.relative_to(work_dir)) + ".manifest.json")
    result = verify_patch_bundle(
        archive,
        _work_path(work_dir, manifest_value, must_exist=True),
    )
    print(json.dumps(result))
    return 0


def register_unity_parser(subparsers: argparse._SubParsersAction) -> None:
    unity = subparsers.add_parser(
        "unity",
        help="Unity 2021.3 Mono managed-mod patch workflow",
    )
    commands = unity.add_subparsers(dest="unity_command", required=True)

    discover = commands.add_parser("discover", help="Inspect a configured Unity player without modifying it")
    discover.add_argument("--workspace", required=True)
    discover.add_argument("--game-root", required=True)
    discover.add_argument("--output", default="reports/unity-discovery.json")
    discover.set_defaults(func=cmd_discover)

    payload = commands.add_parser(
        "compile-payload",
        help="Compile reviewed targets and Unity location metadata into a runtime payload",
    )
    payload.add_argument("--workspace", required=True)
    payload.add_argument("--segments", required=True)
    payload.add_argument("--reviewed", required=True)
    payload.add_argument("--output", required=True)
    payload.add_argument("--locale", required=True)
    payload.add_argument("--allow-unreviewed", action="store_true")
    payload.set_defaults(func=cmd_compile_payload)

    build = commands.add_parser(
        "build-managed",
        help="Build a workspace-only game-loader bridge against read-only Mono references",
    )
    build.add_argument("--workspace", required=True)
    build.add_argument("--managed-root", required=True)
    build.add_argument("--source", action="append", required=True)
    build.add_argument("--reference", action="append", default=[])
    build.add_argument("--define", action="append", default=[])
    build.add_argument("--output", required=True)
    build.add_argument("--dotnet", default="dotnet")
    build.set_defaults(func=cmd_build_managed)

    pack = commands.add_parser("pack", help="Create a deterministic sidecar patch ZIP in the workspace")
    pack.add_argument("--workspace", required=True)
    pack.add_argument("--spec", required=True)
    pack.add_argument("--output", required=True)
    pack.set_defaults(func=cmd_pack)

    verify = commands.add_parser("verify", help="Verify patch contents and digests without installing it")
    verify.add_argument("--workspace", required=True)
    verify.add_argument("--archive", required=True)
    verify.add_argument("--manifest")
    verify.set_defaults(func=cmd_verify)
