"""Content-bound extractor readiness; deliberately separate from inventory coverage."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import ConfigurationError
from .privacy import is_within
from .store import stable_hash

PROFILE_PATH = "tools/extractor-preparation.json"
ATTESTATION_PATH = ".game-l10n/extractor-preparation.json"
PROFILE_SCHEMA = "parr0t.extractor-preparation-profile.v1"
CHECK_SCHEMA = "parr0t.extractor-preparation-check.v1"
REQUEST_SCHEMA = "parr0t.extractor-preparation-request.v1"
ATTESTATION_SCHEMA = "parr0t.extractor-preparation.v1"
PURPOSE = "extractor_preparation"
ACTION = "prepare_and_validate_extractors"


def _fail() -> None:
    raise ConfigurationError("E_EXTRACTOR_PREPARATION_PENDING: bound extractor preparation is required")


def _file(work_dir: Path, relative: Any) -> Path:
    from .control_plane import safe_artifact_path
    if not isinstance(relative, str) or not safe_artifact_path(Path(relative)):
        _fail()
    path = (work_dir / relative).resolve()
    if not is_within(path, work_dir.resolve()):
        _fail()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        _fail()
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _codes(value: Any) -> list[str]:
    if (
        not isinstance(value, list) or not value
        or any(not isinstance(item, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", item) is None for item in value)
        or len(set(value)) != len(value)
    ):
        _fail()
    return sorted(value)


def preparation_required(work_dir: Path, config: dict[str, Any]) -> bool:
    selection = config.get("sdk_selection", {})
    decision = selection.get("decision") or {}
    evaluation = selection.get("evaluations", {}).get(decision.get("branch"), {})
    requirements = config.get("extractor_requirements", {})
    return bool(
        decision.get("mode") in {"extend", "derive-new", "private-new"}
        or evaluation.get("result") == "partial"
        or requirements
        or (work_dir / PROFILE_PATH).exists()
        or (work_dir / ATTESTATION_PATH).exists()
    )


def _binding(work_dir: Path, config: dict[str, Any], activation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from .workspace import _workspace_tool_binding
    requirements = config.get("extractor_requirements")
    if not isinstance(requirements, dict) or not isinstance(requirements.get("source_metadata"), dict) or not requirements["source_metadata"]:
        _fail()
    capabilities = _codes(requirements.get("required_capabilities"))
    formats = _codes(requirements.get("required_formats"))
    profile = _read(work_dir / PROFILE_PATH)
    if (
        profile.get("schema") != PROFILE_SCHEMA
        or _codes(profile.get("required_capabilities")) != capabilities
        or _codes(profile.get("required_formats")) != formats
        or profile.get("source_metadata") != requirements["source_metadata"]
    ):
        _fail()
    paths = profile.get("extractor_paths")
    checks = profile.get("checks")
    if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths) or not isinstance(checks, list) or not checks:
        _fail()
    files = {relative: _digest(_file(work_dir, relative)) for relative in paths}
    seen: set[str] = set()
    covered_formats: set[str] = set()
    covered_capabilities: set[str] = set()
    outputs: set[str] = set()
    inputs: set[str] = set()
    for check in checks:
        if not isinstance(check, dict):
            _fail()
        identifier = _codes([check.get("id")])[0]
        if identifier in seen or check.get("format") not in formats:
            _fail()
        seen.add(identifier)
        covered_formats.add(check["format"])
        check_capabilities = _codes(check.get("capabilities"))
        if not set(check_capabilities) <= set(capabilities):
            _fail()
        covered_capabilities.update(check_capabilities)
        runner = check.get("runner")
        if runner not in files or _file(work_dir, runner).suffix != ".py":
            _fail()
        arguments = check.get("arguments", [])
        if not isinstance(arguments, list) or any(not isinstance(arg, str) for arg in arguments):
            _fail()
        cases = check.get("cases")
        if not isinstance(cases, list) or not cases:
            _fail()
        case_ids: set[str] = set()
        for case in cases:
            if not isinstance(case, dict) or set(case) != {"id", "input_path", "expected_path", "actual_path"}:
                _fail()
            case_id = _codes([case["id"]])[0]
            if case_id in case_ids:
                _fail()
            case_ids.add(case_id)
            for field in ("input_path", "expected_path"):
                relative = case[field]
                files[relative] = _digest(_file(work_dir, relative))
                inputs.add(relative)
            output = case["actual_path"]
            _file(work_dir, output)
            if output in outputs:
                _fail()
            outputs.add(output)
    if covered_formats != set(formats) or covered_capabilities != set(capabilities) or outputs & (inputs | set(files)):
        _fail()
    return {
        "selection": config.get("sdk_selection"),
        "activation": activation,
        "workspace_tools": _workspace_tool_binding(work_dir),
        "requirements": requirements,
        "profile_sha256": _digest(work_dir / PROFILE_PATH),
        "implemented_files": files,
    }, profile


def _validate_evidence(work_dir: Path, evidence: dict[str, Any], binding: dict[str, Any], profile: dict[str, Any]) -> int:
    if evidence.get("schema") != ATTESTATION_SCHEMA or evidence.get("status") != "ready" or evidence.get("binding") != binding:
        _fail()
    records = evidence.get("checks")
    if not isinstance(records, list) or len(records) != len(profile["checks"]):
        _fail()
    count = 0
    for check, record in zip(profile["checks"], records):
        if not isinstance(record, dict) or record.get("id") != check["id"]:
            _fail()
        request_path = _file(work_dir, record.get("request_path"))
        result_path = _file(work_dir, record.get("result_path"))
        request = _read(request_path)
        result = _read(result_path)
        unsigned = {key: value for key, value in request.items() if key != "request_id"}
        if (
            request.get("schema") != REQUEST_SCHEMA
            or request.get("request_id") != stable_hash(unsigned)
            or request.get("binding_fingerprint") != stable_hash(binding)
            or request.get("task_id") != evidence.get("task_id")
            or request.get("check") != check
            or result.get("schema") != CHECK_SCHEMA
            or set(result) != {"schema", "request_id", "cases"}
            or result.get("request_id") != request.get("request_id")
            or result.get("cases") != check["cases"]
            or record.get("request_sha256") != _digest(request_path)
            or record.get("result_sha256") != _digest(result_path)
        ):
            _fail()
        actual = {}
        for case in check["cases"]:
            actual[case["actual_path"]] = _digest(_file(work_dir, case["actual_path"]))
            if actual[case["actual_path"]] != _digest(_file(work_dir, case["expected_path"])):
                _fail()
            count += 1
        if actual != record.get("outputs"):
            _fail()
    return count


def require_extractor_preparation(work_dir: Path, config: dict[str, Any], activation: dict[str, Any]) -> dict[str, Any]:
    if not preparation_required(work_dir, config):
        return {"status": "not_required"}
    try:
        binding, profile = _binding(work_dir, config, activation)
        evidence = _read(work_dir / ATTESTATION_PATH)
        from .data_plane_task import _load_bound_data_plane_relay
        relay, task = _load_bound_data_plane_relay(work_dir, _file(work_dir, evidence.get("relay_path")))
        if task.get("purpose") != PURPOSE or task.get("task_id") != evidence.get("task_id") or relay.get("task_id") != evidence.get("task_id"):
            _fail()
        if task.get("sdk_activation", {}).get("selection_fingerprint") != activation.get("selection_fingerprint"):
            _fail()
        original = task.get("extractor_requirements")
        if original is not None and original != config.get("extractor_requirements"):
            _fail()
        count = _validate_evidence(work_dir, evidence, binding, profile)
        return {"status": "ready", "counts": {"checks": len(profile["checks"]), "cases": count}, "attestation_path": ATTESTATION_PATH}
    except (ConfigurationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        _fail()


def preparation_state(work_dir: Path, config: dict[str, Any], activation: dict[str, Any]) -> dict[str, Any]:
    try:
        return require_extractor_preparation(work_dir, config, activation)
    except ConfigurationError:
        return {"status": "pending", "issue_codes": ["E_EXTRACTOR_PREPARATION_PENDING"], "next_action": ACTION, "dispatch_allowed": False}


def _check_extractor_preparation(work_dir: Path, relay_path: Path) -> dict[str, Any]:
    """Run activated local checks inside the isolated worker, never in status."""
    from .data_plane_task import _load_bound_data_plane_relay
    from .workspace import load_config, require_sdk_activation
    relay, task = _load_bound_data_plane_relay(work_dir, relay_path)
    if task.get("purpose") != PURPOSE or task.get("worker_role") != "source_probe":
        _fail()
    _write(work_dir / ATTESTATION_PATH, {"schema": ATTESTATION_SCHEMA, "status": "pending", "task_id": task["task_id"]})
    config = load_config(work_dir)
    activation = require_sdk_activation(work_dir, config)
    if activation.get("selection_fingerprint") != task.get("sdk_activation", {}).get("selection_fingerprint"):
        _fail()
    if task.get("extractor_requirements") is not None and task["extractor_requirements"] != config.get("extractor_requirements"):
        _fail()
    binding, profile = _binding(work_dir, config, activation)
    records = []
    check_root = relay_path.resolve().parent / "checks"
    for check in profile["checks"]:
        request_path = check_root / (check["id"] + ".request.json")
        result_path = check_root / (check["id"] + ".result.json")
        unsigned = {"schema": REQUEST_SCHEMA, "task_id": task["task_id"], "binding_fingerprint": stable_hash(binding), "check": check}
        request = {**unsigned, "request_id": stable_hash(unsigned)}
        _write(request_path, request)
        result_path.unlink(missing_ok=True)
        for case in check["cases"]:
            output = _file(work_dir, case["actual_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.unlink(missing_ok=True)
        process = subprocess.run(
            [sys.executable, "-I", "-B", str(_file(work_dir, check["runner"])), *check.get("arguments", []), "--preparation-request", str(request_path), "--preparation-result", str(result_path)],
            cwd=work_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        if process.returncode != 0:
            _fail()
        records.append({
            "id": check["id"], "request_path": request_path.relative_to(work_dir).as_posix(),
            "result_path": result_path.relative_to(work_dir).as_posix(),
            "request_sha256": _digest(request_path), "result_sha256": _digest(result_path),
            "outputs": {case["actual_path"]: _digest(_file(work_dir, case["actual_path"])) for case in check["cases"]},
        })
    # A check cannot rewrite its implementation, fixtures, requirements or activation.
    current = load_config(work_dir)
    current_binding, _ = _binding(work_dir, current, require_sdk_activation(work_dir, current))
    if current_binding != binding:
        _fail()
    evidence = {"schema": ATTESTATION_SCHEMA, "status": "ready", "task_id": task["task_id"], "relay_path": relay_path.resolve().relative_to(work_dir.resolve()).as_posix(), "binding": binding, "checks": records}
    _validate_evidence(work_dir, evidence, binding, profile)
    _write(work_dir / ATTESTATION_PATH, evidence)
    return require_extractor_preparation(work_dir, config, activation)


def check_extractor_preparation(work_dir: Path, relay_path: Path) -> dict[str, Any]:
    try:
        return _check_extractor_preparation(work_dir.resolve(), relay_path)
    except (ConfigurationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        _fail()
