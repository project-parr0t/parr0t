"""Text-free native host/model bindings and relay launch evidence.

These are launcher records, not provider self-reports or authentication proofs.
No session log or semantic result is read to determine the executing model.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .control_plane import control_plane_state, require_control_plane_ready
from .store import stable_hash

RUNTIME_PATH = ".game-l10n/native/runtime.json"
RUNTIME_SCHEMA = "parr0t.native-runtime.v1"
LAUNCH_SCHEMA = "parr0t.native-launch.v1"
RUNTIME_ISSUES = {
    "E_NATIVE_RELAY_PREFLIGHT_REQUIRED", "E_NATIVE_DISPATCH_UNAVAILABLE",
    "E_NATIVE_WORKER_NOT_STARTED", "E_NATIVE_WORKER_MODEL_MISMATCH",
    "E_NATIVE_WORKER_MODEL_UNAVAILABLE",
}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _exact_model(value: str) -> bool:
    return (
        isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]*", value) is not None
        and value not in {"default", "inherit", "task", "smol", "slow", "luna", "sol"}
        and not value.startswith("@")
    )


def configure_native_runtime(work_dir: Path, host: str, models: dict[str, str]) -> dict[str, Any]:
    require_control_plane_ready(work_dir)
    if host not in {"codex", "omp"}:
        raise ValueError("E_NATIVE_RUNTIME_INVALID: unsupported host")
    prefix = "openai-codex/" if host == "omp" else ""
    bindings = {"native-luna": prefix + "gpt-6-luna", "native-sol": prefix + "gpt-6.1-sol"}
    try:
        previous = native_runtime(work_dir, allow_stale_generation=True)
    except ValueError:
        previous = None  # An explicit bind may repair invalid runtime metadata.
    if previous is not None and previous["host"] == host:
        bindings.update(previous["models"])
    if (
        not all(re.fullmatch(r"native-[a-z0-9][a-z0-9._-]*", route) for route in models)
        or not all(_exact_model(value) for value in models.values())
    ):
        raise ValueError("E_NATIVE_RUNTIME_INVALID: use exact route=model bindings")
    bindings.update(models)
    payload = {
        "schema": RUNTIME_SCHEMA, "host": host, "models": bindings,
        "control_generation": int(control_plane_state(work_dir).get("generation", 0)),
    }
    _write(work_dir / RUNTIME_PATH, {**payload, "fingerprint": stable_hash(payload)})
    return {**payload, "runtime_path": RUNTIME_PATH}


def native_runtime(work_dir: Path, *, required: bool = False,
                   allow_stale_generation: bool = False) -> dict[str, Any] | None:
    path = work_dir / RUNTIME_PATH
    if not path.is_file():
        if required:
            raise ValueError("E_NATIVE_RUNTIME_REQUIRED: run native-runtime --host codex or omp")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        unsigned = {key: item for key, item in value.items() if key != "fingerprint"}
        valid = (
            set(value) == {"schema", "host", "models", "control_generation", "fingerprint"}
            and value["schema"] == RUNTIME_SCHEMA and value["host"] in {"codex", "omp"}
            and value["fingerprint"] == stable_hash(unsigned)
            and (allow_stale_generation or value["control_generation"] == int(control_plane_state(work_dir).get("generation", 0)))
            and {"native-luna", "native-sol"} <= set(value["models"])
            and all(re.fullmatch(r"native-[a-z0-9][a-z0-9._-]*", route) for route in value["models"])
            and all(_exact_model(model) for model in value["models"].values())
        )
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        valid = False
    if not valid:
        raise ValueError("E_NATIVE_RUNTIME_INVALID: rebind the current host and generation")
    return value


def launch_contract(work_dir: Path, route: str, tasks: list[str]) -> dict[str, Any] | None:
    runtime = native_runtime(work_dir)
    if runtime is None:
        return None  # Legacy library/checkpoint compatibility; public CLI requires binding.
    if route not in runtime["models"]:
        raise ValueError("E_NATIVE_RUNTIME_INVALID: native route has no exact model")
    return {
        "host": runtime["host"], "runtime_fingerprint": runtime["fingerprint"],
        "route": route, "model": runtime["models"][route], "tasks": tasks,
        "history_mode": "none", "inherit_model_allowed": False,
        "capability_preflight_required": True,
        "spawn_model_parameter": "tasks[].model" if runtime["host"] == "omp" else "model",
        "preflight": "inspect_available_dispatch_tool_and_exact_model_before_starting_any_worker",
        "launch_policy": "pass_model_explicitly_then_record_returned_worker_id; skip_already_started_tasks",
        "unavailable_action": "record_capability_unavailable_or_model_unavailable_then_seal_without_results",
        "record_command": "game-l10n record-native-launch --workspace <workspace> --relay <relay-path>",
        "model_evidence": "explicit_spawn_arguments_and_host_model_metadata_when_available",
    }


def _ledger_path(relay_path: Path) -> Path:
    return relay_path.with_name(relay_path.stem + "-launch.json")


def bind_data_plane_launch(work_dir: Path, relay_path: Path) -> None:
    """Attach host routing to an existing core-bound task without rebuilding it."""
    if not relay_path.resolve().is_relative_to(work_dir.resolve() / ".game-l10n"):
        raise ValueError("E_NATIVE_LAUNCH_INVALID: relay escaped control metadata")
    relay = json.loads(relay_path.read_text(encoding="utf-8"))
    if "launch_contract" in relay:
        _load(work_dir, relay_path)
        return
    relay["launch_contract"] = _contract_for_relay(work_dir, relay)
    _write(relay_path, relay)


def _contract_for_relay(work_dir: Path, relay: dict) -> dict | None:
    if relay.get("schema") == "parr0t.native-wave-relay.v1":
        tasks = [binding["task"] for binding in relay["task_bindings"]]
        route = (relay.get("route_binding") or {}).get("provider") or (
            "native-luna" if relay["pass_kind"] == "translation" else "native-sol"
        )
    else:
        plan = relay.get("execution_plan", {})
        slots = plan.get("required_worker_slots", []) if plan.get("tracking_required") else [0]
        tasks = [f"worker-{slot}" for slot in slots]
        if plan.get("finalizer_required"):
            tasks.append("finalizer")
        route = relay.get("worker_route") or "native-sol"
    legacy_results = (relay.get("launch_contract") or {}).get("legacy_results", [])
    if legacy_results:
        from hashlib import sha256
        by_task = {binding["task"]: binding for binding in relay.get("task_bindings", [])}
        if (not isinstance(legacy_results, list) or any(
            not isinstance(row, dict) or set(row) != {"task", "result_sha256"}
            or not isinstance(row["task"], str) or not isinstance(row["result_sha256"], str)
            for row in legacy_results
        ) or len({row["task"] for row in legacy_results}) != len(legacy_results)):
            raise ValueError("E_NATIVE_LAUNCH_INVALID: legacy result bindings changed")
        for row in legacy_results:
            if not isinstance(row, dict) or set(row) != {"task", "result_sha256"} or row["task"] not in by_task:
                raise ValueError("E_NATIVE_LAUNCH_INVALID: legacy result bindings changed")
            result = (work_dir / by_task[row["task"]]["result"]).resolve()
            if not result.is_relative_to(work_dir.resolve()) or not result.is_file() or sha256(result.read_bytes()).hexdigest() != row["result_sha256"]:
                raise ValueError("E_NATIVE_LAUNCH_INVALID: preserved legacy result changed")
        tasks = [task for task in tasks if task not in {row["task"] for row in legacy_results}]
    contract = launch_contract(work_dir, route, tasks)
    if legacy_results and contract is not None:
        contract["legacy_results"] = legacy_results
    return contract


def _load(work_dir: Path, relay_path: Path) -> tuple[dict, dict, dict]:
    require_control_plane_ready(work_dir)
    relay_path = relay_path.resolve()
    if not relay_path.is_relative_to(work_dir.resolve() / ".game-l10n"):
        raise ValueError("E_NATIVE_LAUNCH_INVALID: relay escaped control metadata")
    relay = json.loads(relay_path.read_text(encoding="utf-8"))
    contract = relay.get("launch_contract")
    if not isinstance(contract, dict):
        raise ValueError("E_NATIVE_LAUNCH_INVALID: legacy relay has no launch contract")
    expected = _contract_for_relay(work_dir, relay)
    if isinstance(expected, dict):
        # A mapping added for another route or a clean control-generation
        # refresh does not change this wave's locked host/model/task binding.
        expected["runtime_fingerprint"] = contract.get("runtime_fingerprint")
    if contract != expected:
        raise ValueError("E_NATIVE_LAUNCH_INVALID: runtime binding changed")
    path = _ledger_path(relay_path)
    ledger = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
        "schema": LAUNCH_SCHEMA, "relay_fingerprint": stable_hash(relay),
        "capability": "unknown", "workers": {}, "issue_code": "",
    }
    if (
        not isinstance(ledger, dict) or ledger.get("schema") != LAUNCH_SCHEMA
        or ledger.get("relay_fingerprint") != stable_hash(relay)
        or ledger.get("capability") not in {"unknown", "available", "unavailable"}
        or not isinstance(ledger.get("workers"), dict)
        or set(ledger["workers"]) - set(contract["tasks"])
        or not all(
            isinstance(row, dict) and set(row) == {"worker_id", "model", "observed_model"}
            and isinstance(row["worker_id"], str) and bool(row["worker_id"])
            and isinstance(row["model"], str)
            and (row["observed_model"] is None or isinstance(row["observed_model"], str))
            for row in ledger["workers"].values()
        )
        or ledger.get("issue_code", "") not in RUNTIME_ISSUES | {""}
    ):
        raise ValueError("E_NATIVE_LAUNCH_INVALID: launcher evidence changed")
    return relay, contract, ledger


def record_native_launch(
    work_dir: Path, relay_path: Path, *, event: str, task: str | None = None,
    worker_id: str | None = None, model: str | None = None, observed_model: str | None = None,
) -> dict[str, Any]:
    relay, contract, ledger = _load(work_dir, relay_path)
    if event in {"capability-available", "capability-unavailable", "model-unavailable"}:
        if any(item is not None for item in (task, worker_id, model, observed_model)):
            raise ValueError("E_NATIVE_LAUNCH_INVALID: preflight takes no worker fields")
        ledger["capability"] = "available" if event == "capability-available" else "unavailable"
        ledger["issue_code"] = {
            "capability-available": "", "capability-unavailable": "E_NATIVE_DISPATCH_UNAVAILABLE",
            "model-unavailable": "E_NATIVE_WORKER_MODEL_UNAVAILABLE",
        }[event]
    elif event == "worker-started":
        if ledger["capability"] != "available" or task not in contract["tasks"] or not (
            isinstance(worker_id, str) and re.fullmatch(r"[A-Za-z0-9/][A-Za-z0-9._:/\-]{0,255}", worker_id)
        ):
            raise ValueError("E_NATIVE_LAUNCH_INVALID: verify dispatch capability and task first")
        if model != contract["model"] or (observed_model is not None and observed_model != model):
            ledger["issue_code"] = "E_NATIVE_WORKER_MODEL_MISMATCH"
            # Quarantine a racing wrong-model result before another launch can
            # be recorded. Preserve its bytes; never relabel it as the new model.
            for binding in relay.get("task_bindings", []):
                if binding["task"] == task:
                    result = (work_dir / binding["result"]).resolve()
                    if not result.is_relative_to(work_dir.resolve()):
                        raise ValueError("E_NATIVE_LAUNCH_INVALID: result escaped workspace")
                    if result.is_file():
                        from hashlib import sha256
                        rejected = result.with_name(result.name + ".model-rejected-" + sha256(result.read_bytes()).hexdigest())
                        result.replace(rejected)
        else:
            prior = ledger["workers"].get(task)
            worker = {"worker_id": worker_id, "model": model, "observed_model": observed_model}
            if prior is not None and prior != worker:
                raise ValueError("E_NATIVE_LAUNCH_INVALID: worker already started; do not replay it")
            if any(item.get("worker_id") == worker_id for key, item in ledger["workers"].items() if key != task):
                raise ValueError("E_NATIVE_LAUNCH_INVALID: worker must be fresh for each task")
            ledger["workers"][task] = worker
    else:
        raise ValueError("E_NATIVE_LAUNCH_INVALID: unsupported event")
    _write(_ledger_path(relay_path), ledger)
    return native_launch_summary(work_dir, relay_path)


def native_launch_summary(work_dir: Path, relay_path: Path) -> dict[str, Any]:
    _, contract, ledger = _load(work_dir, relay_path)
    workers = ledger["workers"]
    valid = [task for task, row in workers.items() if (
        isinstance(row, dict) and row.get("worker_id") and row.get("model") == contract["model"]
        and row.get("observed_model") in {None, contract["model"]}
    )]
    issue = ledger["issue_code"]
    if not issue and contract["tasks"] and ledger["capability"] != "available":
        issue = "E_NATIVE_RELAY_PREFLIGHT_REQUIRED"
    if not issue and len(valid) != len(contract["tasks"]):
        issue = "E_NATIVE_WORKER_NOT_STARTED"
    return {
        "expected_workers": len(contract["tasks"]), "started_workers": len(valid),
        "unstarted_tasks": [task for task in contract["tasks"] if task not in valid],
        "issue_codes": [issue] if issue else [],
        "model": contract["model"], "host": contract["host"],
        "legacy_unverified_results": len(contract.get("legacy_results", [])),
        "observed_model_workers": sum(workers[task].get("observed_model") is not None for task in valid),
        "model_evidence": (
            "legacy_results_unverified_recorded_workers_separate"
            if contract.get("legacy_results")
            else "host_metadata_when_present_otherwise_explicit_spawn_arguments"
        ),
    }
