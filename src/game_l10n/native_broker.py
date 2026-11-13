"""Deterministic native-host broker; semantic output stays in workspace artifacts."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import secrets
import re
import time
from typing import Callable

from .control_plane import require_control_plane_ready
from .native_host import (HOST_PROTOCOL, PROBE_SCHEMA, OPAQUE_ID, NativeHostClient,
                          _write, native_host_profile, probe_path, verified_host_probe)
from .native_runtime import native_runtime, record_native_launch, _load
from .store import stable_hash


@contextmanager
def _exclusive(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        # Windows byte locks reject reads as well as writes by a second broker.
        # File size remains available before acquiring the first-byte lock.
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("E_NATIVE_BROKER_ALREADY_RUNNING") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _workers(value: dict, keys: set[str], model: str) -> list[dict]:
    rows = value.get("workers")
    if (not isinstance(rows, list) or len(rows) != len(keys)
            or any(not isinstance(row, dict) or not isinstance(row.get("task_key"), str) for row in rows)
            or {row["task_key"] for row in rows} != keys):
        raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
    projected = []
    for row in rows:
        if (not isinstance(row.get("worker_id"), str) or not OPAQUE_ID.fullmatch(row["worker_id"])
                or row.get("model") != model or row.get("observed_model") not in {None, model}
                or row.get("status") not in {"queued", "running", "completed", "failed"}):
            raise ValueError("E_NATIVE_HOST_MODEL_OR_REPLY_INVALID")
        projected.append({key: row.get(key) for key in (
            "task_key", "worker_id", "model", "observed_model", "status")})
        if "probe_digest" in row:
            if not isinstance(row["probe_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["probe_digest"]):
                raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
            projected[-1]["probe_digest"] = row["probe_digest"]
    if len({row["worker_id"] for row in projected}) != len(projected):
        raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
    return projected


def _drive(client: NativeHostClient, state_path: Path, identity: dict, tasks: list[dict],
           *, started: Callable[[list[dict]], None] | None = None,
           progress: Callable[..., None] | None = None, poll_seconds: float = 1.0) -> dict:
    if not math.isfinite(poll_seconds) or poll_seconds < 0:
        raise ValueError("E_NATIVE_BROKER_POLL_INVALID")
    request_id = "wave-" + stable_hash(identity)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
    if state is not None and (not isinstance(state, dict) or state.get("identity") != identity
                              or state.get("request_id") != request_id):
        raise ValueError("E_NATIVE_BROKER_STATE_CHANGED")
    payload = {"protocol": HOST_PROTOCOL, "request_id": request_id,
               "model": identity["model"], "history_mode": "none", "delegation_allowed": False,
               "reply_delivery": "broker_only", "tasks": tasks}
    reconcile = state is not None
    if state is None:
        # Persist intent before the network call. Lost responses are reconciled
        # by the same idempotency key, never answered with a blind second start.
        state = {"identity": identity, "request_id": request_id, "status": "requested"}
        _write(state_path, state)
    if state.get("status") == "completed":
        rows = _workers(state, {task["task_key"] for task in tasks}, identity["model"])
        if any(row["status"] not in {"completed", "failed"} for row in rows):
            raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
        if started:
            started(rows)
        return state
    value = client.wave(request_id, payload, reconcile=reconcile)
    previous_ids = {row["task_key"]: row["worker_id"] for row in state.get("workers", [])}
    while True:
        if value.get("request_id") != request_id:
            raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
        if value.get("status") == "capacity_unavailable":
            return {"status": "pending", "issue_codes": ["E_NATIVE_HOST_CAPACITY_UNAVAILABLE"]}
        if value.get("status") not in {"queued", "running", "completed"}:
            raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
        rows = _workers(value, {task["task_key"] for task in tasks}, identity["model"])
        if value["status"] == "completed" and any(row["status"] not in {"completed", "failed"} for row in rows):
            raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
        if previous_ids and any(previous_ids.get(row["task_key"]) != row["worker_id"] for row in rows):
            raise ValueError("E_NATIVE_HOST_WORKER_ID_CHANGED")
        if started:
            started(rows)
        state = {"identity": identity, "request_id": request_id,
                 "status": value["status"], "workers": rows}
        if "usage" in value:
            usage = value["usage"]
            if (not isinstance(usage, dict) or type(usage.get("input_tokens")) is not int
                    or usage["input_tokens"] < 0 or type(usage.get("provider_cost_usd")) not in {int, float}
                    or not math.isfinite(usage["provider_cost_usd"]) or usage["provider_cost_usd"] < 0):
                raise ValueError("E_NATIVE_HOST_USAGE_INVALID")
            state["usage"] = {key: usage[key] for key in ("input_tokens", "provider_cost_usd")}
        _write(state_path, state)
        previous_ids = {row["task_key"]: row["worker_id"] for row in rows}
        completed = sum(row["status"] in {"completed", "failed"} for row in rows)
        if progress:
            progress(completed_records=sum(task.get("records", 1) for task in tasks if any(
                row["task_key"] == task["task_key"] and row["status"] in {"completed", "failed"} for row in rows)),
                failed_records=sum(task.get("records", 1) for task in tasks if any(
                row["task_key"] == task["task_key"] and row["status"] == "failed" for row in rows)))
        if value["status"] == "completed":
            if completed != len(rows):
                raise ValueError("E_NATIVE_HOST_REPLY_INVALID")
            return state
        time.sleep(poll_seconds)
        value = client.request("GET", "/waves/by-request/" + request_id)


def _probe_native_host(work_dir: Path, route: str, *, live: bool = False,
                      poll_seconds: float = 1.0) -> dict:
    require_control_plane_ready(work_dir)
    runtime = native_runtime(work_dir, required=True)
    if runtime.get("transport") != "native-host-api":
        raise ValueError("E_NATIVE_BROKER_NOT_SELECTED")
    profile = native_host_profile(work_dir, runtime["host"])
    model = runtime["models"].get(route)
    client = NativeHostClient(profile)
    capabilities = client.capabilities()
    if model not in capabilities["models"]:
        raise ValueError("E_NATIVE_WORKER_MODEL_UNAVAILABLE")
    result = {"host": runtime["host"], "route": route, "model": model,
              "max_workers": capabilities["max_workers"], "ready": False}
    if not live:
        return {**result, "requires_live_probe": True}
    target = probe_path(work_dir, runtime["host"], route)
    identity = {"profile_fingerprint": profile["fingerprint"], "model": model,
                "route": route, "control_generation": runtime["control_generation"]}
    pending = target.with_suffix(".pending.json")
    with _exclusive(target.with_suffix(".lock")):
        prior = json.loads(pending.read_text(encoding="utf-8")) if pending.exists() else None
        execution_path = target.with_suffix(".execution.json")
        if prior and prior.get("identity") != identity:
            old = json.loads(execution_path.read_text()) if execution_path.is_file() else {}
            if old.get("status") != "completed" or old.get("identity") != prior.get("identity"):
                raise ValueError("E_NATIVE_HOST_PROBE_PENDING")
            from .native_runtime import _archive_metadata
            _archive_metadata(pending)
            _archive_metadata(execution_path)
            execution_path.unlink()
            prior = None
        canary = prior.get("canary") if prior else secrets.token_hex(24)
        _write(pending, {"identity": identity, "canary": canary})
        driven = _drive(client, target.with_suffix(".execution.json"), identity,
                        [{"task_key": "probe", "kind": "probe", "canary": canary}], poll_seconds=poll_seconds)
        if driven["status"] != "completed":
            return {**result, **driven}
        worker = driven["workers"][0]
        if (worker["status"] != "completed" or worker["observed_model"] != model
                or worker.get("probe_digest") != stable_hash(canary)):
            raise ValueError("E_NATIVE_HOST_LIVE_PROBE_FAILED")
        value = {"schema": PROBE_SCHEMA, **identity, "ready": True,
                 "max_workers": capabilities["max_workers"]}
        _write(target, {**value, "fingerprint": stable_hash(value)})
    return {**result, "ready": True, "execution_verified": True}


def probe_native_host(work_dir: Path, route: str, *, live: bool = False,
                      poll_seconds: float = 1.0) -> dict:
    return _categorical(lambda: _probe_native_host(work_dir.resolve(), route, live=live, poll_seconds=poll_seconds))


def broker_progress_metadata(work_dir: Path, relay_path: Path) -> dict:
    relay, _, _ = _load(work_dir, relay_path)
    return {"records": sum(row["records"] for row in relay.get("task_bindings", [])),
            "requires_sdk": relay.get("worker_role") != "source_probe"}


def _run_native_broker(work_dir: Path, relay_path: Path, *, poll_seconds: float = 1.0,
                      progress: Callable[..., None] | None = None) -> dict:
    require_control_plane_ready(work_dir)
    runtime = native_runtime(work_dir, required=True)
    if runtime.get("transport") != "native-host-api":
        raise ValueError("E_NATIVE_BROKER_NOT_SELECTED")
    relay_path = relay_path.resolve()
    relay, contract, ledger = _load(work_dir, relay_path)
    if contract.get("transport") != "native-host-api":
        raise ValueError("E_NATIVE_BROKER_BINDING_CHANGED")
    profile = native_host_profile(work_dir, runtime["host"])
    probe = verified_host_probe(work_dir, runtime, contract["route"])
    client = NativeHostClient(profile)
    wave = relay.get("schema") == "parr0t.native-wave-relay.v1"
    if wave:
        from .native_pass import (_load_task_manifest, _native_control_plan_fingerprint,
                                 _validate_manifest_task_file, _native_control_state_path,
                                 _load_native_control_state, _native_budget_issue, _record_native_usage)
        manifest_path = (work_dir / relay["manifest"]).resolve()
        if not manifest_path.is_relative_to(work_dir.resolve()):
            raise ValueError("E_NATIVE_BROKER_BINDING_CHANGED")
        manifest = _load_task_manifest(work_dir, manifest_path)
        if (relay["manifest_contract_fingerprint"] != manifest["contract_fingerprint"]
                or relay["plan_fingerprint"] != _native_control_plan_fingerprint(manifest)):
            raise ValueError("E_NATIVE_BROKER_BINDING_CHANGED")
        by_task = {binding["task"]: binding for binding in relay["task_bindings"]}
        unsigned = {key: value for key, value in relay.items() if key != "dispatch_fingerprint"}
        expected = {(manifest_path.parent / task["file"]).resolve(): task for task in manifest["tasks"]
                    if task["wave"] == relay["wave"]}
        if (relay.get("dispatch_fingerprint") != stable_hash(unsigned)
                or len(by_task) != len(relay["task_bindings"])
                or set(expected) != {(work_dir / task).resolve() for task in by_task}):
            raise ValueError("E_NATIVE_BROKER_BINDING_CHANGED")
        results = set()
        for task, binding_row in by_task.items():
            source = (work_dir / task).resolve()
            result_path = (work_dir / binding_row["result"]).resolve()
            bound = expected[source]
            if (not source.is_relative_to(work_dir.resolve()) or not result_path.is_relative_to(work_dir.resolve())
                    or source == result_path or source in results or result_path in expected or result_path in results
                    or result_path.name != source.name or binding_row["records"] != bound["records"]):
                raise ValueError("E_NATIVE_BROKER_BINDING_CHANGED")
            results.add(result_path)
            _validate_manifest_task_file(manifest_path, manifest, bound)
        # Existing immutable manifests can have a wider wave than this host.
        # Admit bounded groups without rewriting tasks or aggregating early.
        width = probe["max_workers"]
        groups = [contract["tasks"][offset:offset + width] for offset in range(0, len(contract["tasks"]), width)]
    else:
        from .data_plane_task import _load_bound_data_plane_relay
        _load_bound_data_plane_relay(work_dir, relay_path)
        by_task = {task: {"task": relay["task_path"], "result": relay["expected_envelope"]} for task in contract["tasks"]}
        groups = [[task for task in contract["tasks"] if task != "finalizer"]]
        if "finalizer" in contract["tasks"]:
            groups.append(["finalizer"])
    binding = stable_hash({"relay": relay, "profile": profile["fingerprint"]})
    state_dir = work_dir / ".game-l10n/native/broker" / binding[:24]
    with _exclusive(state_dir / "broker.lock"):
        admission = state_dir / "admission.json"
        if admission.is_file():
            planned = json.loads(admission.read_text())
            if (planned.get("binding") != binding or not isinstance(planned.get("groups"), list)
                    or any(not isinstance(group, list) or any(not isinstance(task, str) for task in group)
                           for group in planned["groups"])
                    or [task for group in planned["groups"] for task in group] != contract["tasks"]):
                raise ValueError("E_NATIVE_BROKER_STATE_CHANGED")
            if not wave and planned["groups"] != groups:
                raise ValueError("E_NATIVE_BROKER_STATE_CHANGED")
            groups = planned["groups"]
        else:
            _write(admission, {"binding": binding, "groups": groups})
        record_native_launch(work_dir, relay_path, event="capability-available")
        finished_records = 0
        failed_records = 0
        for index, group in enumerate(groups):
            if not group:
                continue
            tasks = []
            task_map = {}
            for task in group:
                key = "task-" + stable_hash(task)[:24]
                task_map[key] = task
                tasks.append({"task_key": key, "kind": "native_task", "workspace": str(work_dir.resolve()),
                              "manifest": relay.get("manifest"), **by_task[task],
                              "slot": int(task[7:]) if task.startswith("worker-") else None,
                              "finalizer": task == "finalizer"})
            state_path = state_dir / f"group-{index}.json"
            if not state_path.is_file() and len(group) > probe["max_workers"]:
                return {"status": "pending", "issue_codes": ["E_NATIVE_HOST_CAPACITY_UNAVAILABLE"]}
            if not state_path.exists() and any(task in ledger["workers"] for task in group):
                raise ValueError("E_NATIVE_HOST_LAUNCH_UNCERTAIN")
            if wave and not state_path.is_file():
                control_path = _native_control_state_path(work_dir, manifest, relay["plan_fingerprint"])
                control = _load_native_control_state(control_path, manifest, relay["plan_fingerprint"])
                issue = _native_budget_issue(manifest["execution_contract"], control, relay["wave"])
                if issue:
                    return {"status": "pending", "issue_codes": [issue]}
                # Explicit per-job limits require an enforcing host. Never
                # silently ignore a user's ceiling on a new transport.
                gate = manifest["execution_contract"].get("bulk_budget_gate", {})
                if gate.get("user_approved"):
                    raise ValueError("E_NATIVE_HOST_EXPLICIT_BUDGET_UNSUPPORTED")

            def started(rows: list[dict]) -> None:
                for row in rows:
                    task = task_map[row["task_key"]]
                    record_native_launch(work_dir, relay_path, event="worker-started", task=task,
                                         worker_id=row["worker_id"], model=contract["model"],
                                         observed_model=row["observed_model"])
                    if not wave and relay.get("execution_plan", {}).get("tracking_required"):
                        from .data_plane_task import record_data_plane_relay_event
                        record_data_plane_relay_event(work_dir, relay_path,
                            event="finalizer-started" if task == "finalizer" else "worker-started",
                            slot=None if task == "finalizer" else int(task[7:]))

            identity = {"profile_fingerprint": profile["fingerprint"], "relay_fingerprint": stable_hash(relay),
                        "model": contract["model"], "group": index, "tasks_fingerprint": stable_hash(tasks)}
            def report(**counts: int) -> None:
                if progress:
                    progress(completed_records=finished_records + counts["completed_records"],
                             failed_records=failed_records + counts["failed_records"])
            result = _drive(client, state_path, identity, tasks, started=started,
                            progress=report, poll_seconds=poll_seconds)
            if result["status"] != "completed":
                return {"status": "pending", "issue_codes": result["issue_codes"]}
            if wave and result.get("usage") is not None:
                control_path = _native_control_state_path(work_dir, manifest, relay["plan_fingerprint"])
                control = _load_native_control_state(control_path, manifest, relay["plan_fingerprint"])
                _record_native_usage(control, usage_event_id=result["request_id"], **result["usage"])
                _write(control_path, control)
            failed_keys = {row["task_key"] for row in result["workers"] if row["status"] == "failed"}
            finished_records += sum(task.get("records", 1) for task in tasks)
            failed_records += sum(task.get("records", 1) for task in tasks if task["task_key"] in failed_keys)
            if progress:
                progress(completed_records=finished_records, failed_records=failed_records)
            if not wave and relay.get("execution_plan", {}).get("tracking_required"):
                from .data_plane_task import record_data_plane_relay_event
                for task in group:
                    record_data_plane_relay_event(work_dir, relay_path,
                        event="finalizer-ended" if task == "finalizer" else "worker-ended",
                        slot=None if task == "finalizer" else int(task[7:]))
        if wave:
            from .native_pass import seal_native_wave_relay
            return seal_native_wave_relay(work_dir, relay_path, relay_ended=True)
        from .data_plane_task import seal_data_plane_task
        return seal_data_plane_task(work_dir, relay_path, worker_ended=True)


def _categorical(operation: Callable[[], dict]) -> dict:
    try:
        return operation()
    except (ValueError, OSError, KeyError, TypeError, AttributeError) as error:
        # Validators may have inspected private files. Only a categorical code
        # may leave this code boundary; never echo raw host or artifact errors.
        code = str(error).split(":", 1)[0]
        if not re.fullmatch(r"E_[A-Z0-9_]+", code):
            code = "E_NATIVE_BROKER_INVALID"
        raise ValueError(code) from None


def run_native_broker(work_dir: Path, relay_path: Path, *, poll_seconds: float = 1.0,
                      progress: Callable[..., None] | None = None) -> dict:
    return _categorical(lambda: _run_native_broker(work_dir.resolve(), relay_path,
                                                 poll_seconds=poll_seconds, progress=progress))
