from __future__ import annotations

import json
from pathlib import Path

from ..control_plane import require_control_plane_entry, require_control_plane_ready
from ..errors import GameL10nError
from ..privacy import require_external_workdir
from ..tooling import record_verified_pack_artifact
from ..workspace import load_config, require_sdk_activation
from . import assets, discovery, package, workflow
from .contracts import AdapterError, fail, output_path, read_json, sha, write_json


REPOSITORY = Path(__file__).resolve().parents[3]


def context(args):
    work = Path(args.workspace).resolve()
    require_external_workdir(work, REPOSITORY)
    config = load_config(work)
    activation = require_sdk_activation(work, config, runtime_root=REPOSITORY)
    target = activation.get("target", {})
    if target.get("engine") != "unity" or target.get("version") != "6000.0" or target.get("branch") != "sdk-unity-6000.0":
        fail("SDK_IDENTITY_MISMATCH")
    roots = [Path(p).resolve() for p in config.get("game_roots", [])]
    root = Path(args.game_root).resolve() if args.game_root else (roots[0] if len(roots) == 1 else None)
    if root is None or root not in roots or not root.is_dir():
        fail("SOURCE_ROOT_NOT_BOUND")
    if any(work.is_relative_to(r) or r.is_relative_to(work) for r in roots):
        fail("WORKSPACE_SOURCE_OVERLAP")
    return work, root, roots


def input_path(work, value):
    path = Path(value)
    path = (work / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_relative_to(work) or not path.exists():
        fail("INPUT_OUTSIDE_WORKSPACE")
    return path


def command(args):
    """Only categorical progress leaves the data plane, even on library errors."""
    try:
        require_control_plane_ready(Path(args.workspace).resolve())
        require_control_plane_entry(Path(args.workspace).resolve())
        work, root, roots = context(args)
        schemas = assets.Schemas(input_path(work, args.schemas), root) if args.schemas else assets.Schemas(root=root)
        operation = args.unity6000_command
        if operation == "discover":
            inventory = discovery.discover(root, schemas, args.managed_file, args.table_file)
            output = output_path(work, args.output, roots, REPOSITORY)
            write_json(output, inventory)
            state = {"status": "discovered", "candidates": len(inventory["records"]), "coverage": "unreconciled", "artifact": str(output)}
        elif operation == "unpack":
            inventory = read_json(input_path(work, args.inventory))
            discovery.validate_inventory(inventory, root, schemas)
            selected = workflow.selected_records(inventory, read_json(input_path(work, args.selection)))
            output = output_path(work, args.output, roots, REPOSITORY)
            write_json(output, {"segments": [{"id": r["id"], "source": r["source"], "metadata": {"unity6000_location": {k: v for k, v in r.items() if k not in {"id", "source"}}}} for r in selected]})
            state = {"status": "unpacked", "targets": len(selected), "artifact": str(output)}
        elif operation == "apply":
            output = output_path(work, args.output, roots, REPOSITORY)
            overlays = read_json(input_path(work, args.overlays)) if args.overlays else []
            receipt = workflow.prepare(root, read_json(input_path(work, args.inventory)), read_json(input_path(work, args.selection)), input_path(work, args.results), schemas, overlays, output, workspace=work)
            state = {"status": "prepared", "files": len(receipt["files"]), "artifact": str(output)}
        elif operation == "pack":
            results = input_path(work, args.results)
            receipt, outputs = workflow.verify_prepared(root, input_path(work, args.prepared), results, schemas)
            output = output_path(work, args.output, roots, REPOSITORY)
            package.pack(outputs, output, result_sha256=sha(results.read_bytes()), inventory_fingerprint=receipt["inventory"]["fingerprint"], workspace=work, source_root=root)
            verified = package.verify(output, root, result_sha256=sha(results.read_bytes()), inventory_fingerprint=receipt["inventory"]["fingerprint"])
            binding = record_verified_pack_artifact(input_path=results, output_path=output, work_dir=work, verifier_fingerprint=verified["fingerprint"])
            state = {"status": binding["status"], "files": verified["files"], "restore": verified["restore"], "artifact": str(output)}
        else:
            results = input_path(work, args.results)
            inventory = read_json(input_path(work, args.inventory))
            discovery.validate_inventory(inventory, root, schemas)
            state = package.verify(input_path(work, args.archive), root, result_sha256=sha(results.read_bytes()), inventory_fingerprint=inventory["fingerprint"])
        print(json.dumps(state, sort_keys=True))
        return 0
    except GameL10nError as exc:
        code = str(exc).split(":", 1)[0]
        if code not in {"E_CONTROL_PLANE_ROTATION_REQUIRED", "E_CONTROL_PLANE_ENTRY_REQUIRED", "E_CONTROL_PLANE_STATE", "E_CONTROL_PLANE_ENTRY_STATE"}:
            code = "U6000_CONFIGURATION_INVALID"
        print(json.dumps({"status": "blocked", "issue_code": code}))
        return 2
    except AdapterError as exc:
        print(json.dumps({"status": "blocked", "issue_code": str(exc)}))
        return 2
    except ImportError:
        print(json.dumps({"status": "blocked", "issue_code": "U6000_OPTIONAL_DEPENDENCY_REQUIRED"}))
        return 2
    except Exception:
        print(json.dumps({"status": "blocked", "issue_code": "U6000_OPERATION_INVALID"}))
        return 2


def build_parser():
    import argparse
    adapter = argparse.ArgumentParser(description="Offline Unity 6000.0 Mono serialized patch adapter")
    operations = adapter.add_subparsers(dest="unity6000_command", required=True)
    for name in ("discover", "unpack", "apply", "pack", "verify"):
        parser = operations.add_parser(name)
        parser.add_argument("--workspace", required=True)
        parser.add_argument("--game-root")
        parser.add_argument("--schemas", help="Explicit version-isolated generated typetree cache")
        if name == "discover":
            parser.add_argument("--managed-file", action="append", default=[], help="Source-root relative unsigned CLR assembly to scan")
            parser.add_argument("--table-file", action="append", default=[], help="Source-root relative asset file with single-line TSV TextAssets")
        if name in {"unpack", "apply", "verify"}:
            parser.add_argument("--inventory", required=True)
        if name in {"unpack", "apply"}:
            parser.add_argument("--selection", required=True)
        if name in {"apply", "pack", "verify"}:
            parser.add_argument("--results", required=True)
        if name == "apply":
            parser.add_argument("--overlays", help="Workspace JSON list of caller-prepared TMP overlay specifications")
        if name == "pack":
            parser.add_argument("--prepared", required=True)
        if name == "verify":
            parser.add_argument("--archive", required=True)
        else:
            parser.add_argument("--output", required=True)
        parser.set_defaults(func=command)
    return adapter


def main(argv=None):
    args = build_parser().parse_args(argv)
    return int(args.func(args))
