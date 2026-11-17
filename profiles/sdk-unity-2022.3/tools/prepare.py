"""Stage synthetic extractor proofs; Core owns executing/admitting their results."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

FORMATS = {
    "managed-cli": ["managed-literals"],
    "dotnet-resources": ["managed-resources"],
    "unity-serialized-v22": ["serialized-fields", "textasset-tables"],
    "unityfs": ["serialized-fields", "textasset-tables"],
    "windows-pe-resources": ["native-resources"],
    "windows-pe-display-calls": ["native-display-dependencies"],
    "unity-app-info": ["app-info"],
}
COMMON = ["independent-physical-bindings", "safe-roundtrip"]


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


def _fixtures():
    from unity_managed_fixture import build_fixture, build_native_fixture
    from unity_native_fixture import SOURCE_RESOURCE, build_resource_fixture
    from unity_objects_fixture import EXPECTED_STRINGS, build_bundle_fixture, build_fixture as objects
    return {
        "managed-cli": (build_fixture(), ["SYNTHETIC SOURCE", "SYNTHETIC SOURCE", "SYNTHETIC SOURCE", "SYNTHETIC OTHER"]),
        "dotnet-resources": (build_fixture(), ["SYNTHETIC RESOURCE", "SYNTHETIC RESOURCE OTHER"]),
        "unity-serialized-v22": (objects(), EXPECTED_STRINGS),
        "unityfs": (build_bundle_fixture(), EXPECTED_STRINGS),
        "windows-pe-resources": (build_resource_fixture(), [SOURCE_RESOURCE, SOURCE_RESOURCE] + [""] * 14),
        "windows-pe-display-calls": (build_native_fixture(), ["SYNTHETIC NATIVE SOURCE", "SYNTHETIC CAPTION"]),
        "unity-app-info": (b"SYNTHETIC COMPANY\r\nSYNTHETIC PRODUCT\r\nUNOWNED\r\n", ["SYNTHETIC PRODUCT"]),
    }


def stage(work: Path, dependency_paths: list[str]):
    from game_l10n.workspace import load_config, require_sdk_activation
    config = load_config(work)
    require_sdk_activation(work, config)
    defaults = {
        "required_formats": list(FORMATS),
        "required_capabilities": sorted({item for values in FORMATS.values() for item in values} | set(COMMON)),
        "source_metadata": {"engine": "unity", "version_series": "2022.3", "backend": "mono", "format_requirements": "source-probe-or-public-sdk-contract"},
    }
    requirements = config.get("extractor_requirements") or defaults
    for format_name in requirements["required_formats"]:
        if format_name not in FORMATS:
            raise ValueError("SDK_PREPARATION_FORMAT_UNSUPPORTED")
    available = {item for values in FORMATS.values() for item in values} | set(COMMON)
    if not set(requirements["required_capabilities"]) <= available:
        raise ValueError("SDK_PREPARATION_CAPABILITY_UNSUPPORTED")
    dependencies = [str(Path(path).resolve()) for path in dependency_paths]
    if any(not Path(path).is_dir() for path in dependencies):
        raise ValueError("SDK_DEPENDENCY_PATH_INVALID")
    tools = work / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent
    for path in source.glob("*.py"):
        target = tools / path.name
        if target.resolve() != path.resolve():
            shutil.copyfile(path, target)
    arguments = [item for path in dependencies for item in ["--dependency-path", path]]
    checks = []
    fixtures = _fixtures()
    for format_name in requirements["required_formats"]:
        raw, expected_sources = fixtures[format_name]
        directory = work / "fixtures" / "sdk-extractor" / format_name
        directory.mkdir(parents=True, exist_ok=True)
        name = "app.info" if format_name == "unity-app-info" else "synthetic.bin"
        input_path = directory / name
        expected_path = directory / "expected.json"
        input_path.write_bytes(raw)
        expected_path.write_text(_json({"sources": expected_sources, "independent": True, "roundtrip": True, "unique_bindings": True}), encoding="utf-8")
        case = {"id": format_name, "input_path": input_path.relative_to(work).as_posix(), "expected_path": expected_path.relative_to(work).as_posix(), "actual_path": ".game-l10n/preparation-output/" + format_name + ".json"}
        checks.append({"id": format_name, "format": format_name, "capabilities": sorted(set(FORMATS[format_name] + COMMON) & set(requirements["required_capabilities"])), "runner": "tools/prepare.py", "arguments": arguments, "cases": [case]})
    profile = {"schema": "parr0t.extractor-preparation-profile.v1", **requirements, "extractor_paths": [path.relative_to(work).as_posix() for path in sorted(tools.glob("*.py"))], "checks": checks}
    (tools / "extractor-preparation.json").write_text(_json(profile), encoding="utf-8")
    if not config.get("extractor_requirements"):
        config["extractor_requirements"] = requirements
        (work / "game-l10n.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"status": "staged_requires_reactivation_and_core_check", "checks": len(checks), "profile": "tools/extractor-preparation.json"}


def _scan(format_name, path, method):
    if format_name in {"managed-cli", "dotnet-resources"}:
        from unity_managed import scan_assembly
        result = scan_assembly(path, "synthetic.bin", method=method)
        kind = "managed_literal" if format_name == "managed-cli" else "managed_resource"
        return [row for row in result["occurrences"] if row["kind"] == kind]
    if format_name in {"unity-serialized-v22", "unityfs"}:
        from unity_objects import scan_serialized
        result = scan_serialized(path, "synthetic.bin", method=method)
    elif format_name == "windows-pe-display-calls":
        from unity_native_calls import scan_native_dependencies
        result = scan_native_dependencies(path.read_bytes(), "synthetic.bin", method=method)
    else:
        from unity_native import scan_native
        result = scan_native(path, "app.info" if format_name == "unity-app-info" else "synthetic.bin", method=method)
    return result["occurrences"]


def _project(rows):
    from unity_inventory_cache import occurrence_id, source_order
    rows = sorted(rows, key=source_order)
    return [(occurrence_id(row), row["source"]) for row in rows], rows


def check_case(format_name, path):
    from unity_managed import patch_assembly
    from unity_native import patch_native
    from unity_native_calls import patch_native_literals
    from unity_objects import patch_serialized
    original, rows = _project(_scan(format_name, path, "primary"))
    independent, _ = _project(_scan(format_name, path, "independent"))
    if not rows:
        raise ValueError("SDK_SYNTHETIC_EXPECTED_TARGET_MISSING")
    selected = next(row for row in rows if row["source"])
    replacement = {"file": selected["file"], "location": selected["location"], "source": selected["source"], "target": "SYNTHETIC TARGET"}
    if format_name in {"managed-cli", "dotnet-resources"}:
        patch = patch_assembly
    elif format_name in {"unity-serialized-v22", "unityfs"}:
        patch = patch_serialized
    elif format_name == "windows-pe-display-calls":
        patch = patch_native_literals
    else:
        patch = lambda raw, replacements: patch_native(raw, replacements, app_info=format_name == "unity-app-info")
    raw = path.read_bytes()
    rebuilt = patch(raw, [replacement])
    with tempfile.TemporaryDirectory(prefix="sdk-preparation-roundtrip-") as directory:
        target = Path(directory) / path.name
        target.write_bytes(rebuilt)
        actual, changed_rows = _project(_scan(format_name, target, "primary"))
        from unity_inventory_cache import occurrence_id
        selected_id = occurrence_id(selected)
        expected_changed = [(key, "SYNTHETIC TARGET" if key == selected_id else text) for key, text in original]
        restored_row = next(row for row in changed_rows if occurrence_id(row) == selected_id)
        restored = patch(rebuilt, [{"file": restored_row["file"], "location": restored_row["location"], "source": restored_row["source"], "target": selected["source"]}])
        target.write_bytes(restored)
        restored_rows, _ = _project(_scan(format_name, target, "independent"))
    return {"sources": [text for _, text in original], "independent": original == independent, "roundtrip": actual == expected_changed and restored_rows == original, "unique_bindings": len({key for key, _ in original}) == len(original)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--dependency-path", action="append", default=[])
    parser.add_argument("--preparation-request", type=Path)
    parser.add_argument("--preparation-result", type=Path)
    args = parser.parse_args()
    if args.preparation_request:
        # Isolated check mode: only explicitly selected dependency roots and the
        # standard library participate, never ambient site-packages.
        sys.path[:] = [str(Path(__file__).resolve().parent)] + [str(Path(path).resolve()) for path in args.dependency_path] + [path for path in sys.path if "site-packages" not in path.lower() and "dist-packages" not in path.lower()]
        request = json.loads(args.preparation_request.read_text(encoding="utf-8"))
        if request.get("schema") != "parr0t.extractor-preparation-request.v1" or not args.preparation_result:
            raise ValueError("SDK_PREPARATION_REQUEST_INVALID")
        work = args.preparation_request.resolve()
        while work.parent != work and not (work / "game-l10n.json").is_file():
            work = work.parent
        if not (work / "game-l10n.json").is_file():
            raise ValueError("SDK_PREPARATION_WORKSPACE_MISSING")
        check = request["check"]
        for case in check["cases"]:
            output = (work / case["actual_path"]).resolve()
            if not output.is_relative_to(work):
                raise ValueError("SDK_PREPARATION_OUTPUT_OUTSIDE_WORKSPACE")
            actual = check_case(check["format"], work / case["input_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(_json(actual), encoding="utf-8")
        args.preparation_result.write_text(_json({"schema": "parr0t.extractor-preparation-check.v1", "request_id": request["request_id"], "cases": check["cases"]}), encoding="utf-8")
    else:
        if not args.workspace:
            parser.error("--workspace is required when staging")
        print(_json(stage(args.workspace.resolve(), args.dependency_path)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
