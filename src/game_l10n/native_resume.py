"""Read-only, text-free discovery of existing native controller bindings."""
from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from .native_pass import (
    NATIVE_REVIEW_SPLIT_CHAIN_SCHEMA,
    NATIVE_WAVE_RECEIPT_SCHEMA,
    NATIVE_WAVE_RELAY_SCHEMA,
    _load_native_control_state,
    _load_task_manifest,
    _manifest_shard_path,
    _native_control_plan_fingerprint,
    _native_control_state_path,
    _review_split_chain_path,
    _wave_receipt_path,
    _wave_relay_dispatch_path,
)
from .store import stable_hash

NATIVE_RESUME_STATUS_SCHEMA = "parr0t.native-resume-status.v1"
_ACTIONS = {
    "", "dispatch_isolated_wave_relay", "wait_for_active_wave",
    "checkpoint_and_stop", "repair_native_runtime", "aggregate_completed_pass",
}
_PROVIDER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")


class _ResumeIssue(Exception):
    def __init__(self, category: str) -> None:
        self.code = "E_NATIVE_RESUME_" + category


def _require(condition: bool, category: str) -> None:
    if not condition:
        raise _ResumeIssue(category)


def _linked(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _contained(root: Path, value: str) -> Path:
    _require(isinstance(value, str) and bool(value), "PATH_INVALID")
    _require(
        not Path(value).is_absolute() and not PureWindowsPath(value).drive
        and "\\" not in value and "\x00" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/")),
        "PATH_INVALID",
    )
    path = root
    for part in value.split("/"):
        path = path / part
        _require(not _linked(path), "PATH_INVALID")
    _require(path.resolve().is_relative_to(root), "PATH_INVALID")
    return path


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "METADATA_INVALID")
        result[key] = value
    return result


def _metadata(path: Path) -> dict[str, Any]:
    _require(path.is_file(), "METADATA_UNREADABLE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
    except (OSError, UnicodeError):
        raise _ResumeIssue("METADATA_UNREADABLE") from None
    except (ValueError, RecursionError):
        raise _ResumeIssue("METADATA_INVALID") from None
    _require(isinstance(value, dict), "METADATA_INVALID")
    return value


def _manifest(root: Path, relative: str) -> dict[str, Any]:
    path = _contained(root, relative)
    _require(path.name == "manifest.json", "MANIFEST_INVALID")
    try:
        manifest = _load_task_manifest(root, path)
        _native_control_plan_fingerprint(manifest)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, RecursionError):
        raise _ResumeIssue("MANIFEST_INVALID") from None
    _contained(root, manifest.get("source_artifact"))
    for task in manifest["tasks"]:
        _contained(root, _relative(root, path.parent / task["file"]))
        _require(type(task.get("slot")) is int and task["slot"] > 0, "MANIFEST_INVALID")
    route = manifest.get("route_binding")
    if route is not None:
        _require(
            isinstance(route, dict) and set(route) == {"provider", "attempt"}
            and isinstance(route["provider"], str)
            and _PROVIDER.fullmatch(route["provider"]) is not None
            and type(route["attempt"]) is int and route["attempt"] > 0,
            "MANIFEST_INVALID",
        )
    return manifest


def _directory(root: Path, relative: str) -> Path:
    path = root if relative == "." else _contained(root, relative)
    _require(not path.exists() or path.is_dir(), "PATH_INVALID")
    return path


def _relay(root: Path, path: Path, manifests: dict[str, dict]) -> tuple[str, str]:
    payload = _metadata(path)
    _require(payload.get("schema") == NATIVE_WAVE_RELAY_SCHEMA, "RELAY_INVALID")
    unsigned = {key: value for key, value in payload.items() if key != "dispatch_fingerprint"}
    _require(payload.get("dispatch_fingerprint") == stable_hash(unsigned), "RELAY_INVALID")
    relative = payload.get("manifest")
    _require(isinstance(relative, str), "RELAY_INVALID")
    if relative not in manifests:
        manifests[relative] = _manifest(root, relative)
    manifest = manifests[relative]
    plan = _native_control_plan_fingerprint(manifest)
    wave = payload.get("wave")
    _require(type(wave) is int and wave > 0, "RELAY_INVALID")
    _require(
        path == _wave_relay_dispatch_path(root, plan, wave)
        and payload.get("plan_fingerprint") == plan
        and payload.get("manifest_contract_fingerprint") == manifest.get("contract_fingerprint")
        and payload.get("pass_kind") == manifest.get("pass_kind", "translation")
        and payload.get("route_binding") == manifest.get("route_binding"),
        "RELAY_INVALID",
    )
    tasks = [task for task in manifest["tasks"] if task["wave"] == wave]
    bindings = payload.get("task_bindings")
    _require(bool(tasks) and isinstance(bindings, list) and len(bindings) == len(tasks), "RELAY_INVALID")
    parents: set[str] = set()
    manifest_path = root / relative
    for task, binding in zip(tasks, bindings, strict=True):
        _require(isinstance(binding, dict), "RELAY_INVALID")
        expected_task = _relative(root, manifest_path.parent / task["file"])
        _require(binding.get("task") == expected_task, "RELAY_INVALID")
        _contained(root, expected_task)
        result = _contained(root, binding.get("result"))
        _require(
            result.name == task["file"] and binding.get("records") == task["records"]
            and type(binding.get("records")) is int
            and type(binding.get("estimated_material_tokens")) is int
            and binding.get("estimated_material_tokens") == task.get("estimated_material_tokens"),
            "RELAY_INVALID",
        )
        parent = _relative(root, result.parent)
        _directory(root, parent)
        parents.add(parent)
    _require(len(parents) == 1, "RESULTS_CONFLICT")
    return relative, parents.pop()


def _counts(root: Path, relative: str, manifest: dict) -> tuple[dict, str, str]:
    plan = _native_control_plan_fingerprint(manifest)
    state_path = _native_control_state_path(root, manifest, plan)
    _contained(root, _relative(root, state_path))
    exists = state_path.exists()
    try:
        state = _load_native_control_state(state_path, manifest, plan)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise _ResumeIssue("STATE_INVALID") from None
    _require(state["last_action"] in _ACTIONS, "STATE_INVALID")
    tasks = manifest["tasks"]
    waves = sorted({task["wave"] for task in tasks})
    imported: set[int] = set()
    for wave in waves:
        wave_tasks = [task for task in tasks if task["wave"] == wave]
        receipt_path = _wave_receipt_path(root, manifest, wave_tasks, wave)
        _contained(root, _relative(root, receipt_path))
        if not receipt_path.exists():
            continue
        receipt = _metadata(receipt_path)
        _require(
            set(receipt) == {"schema", "source_fingerprint", "wave", "tasks"}
            and receipt.get("schema") == NATIVE_WAVE_RECEIPT_SCHEMA
            and receipt.get("source_fingerprint") == manifest["source_fingerprint"]
            and receipt.get("wave") == wave
            and type(receipt.get("wave")) is int
            and isinstance(receipt.get("tasks"), list)
            and len(receipt["tasks"]) == len(wave_tasks), "STATE_INVALID",
        )
        for task, item in zip(wave_tasks, receipt["tasks"], strict=True):
            shard = _manifest_shard_path(root, manifest, task)
            shard_relative = _relative(root, shard)
            _contained(root, shard_relative)
            _require(
                isinstance(item, dict)
                and set(item) == {"task_file", "task_sha256", "shard", "shard_size", "records"}
                and item.get("task_file") == task["file"]
                and item.get("task_sha256") == task["sha256"]
                and item.get("shard") == shard_relative
                and item.get("records") == task["records"]
                and type(item.get("records")) is int
                and type(item.get("shard_size")) is int and item["shard_size"] >= 0
                and shard.is_file() and shard.stat().st_size == item["shard_size"], "STATE_INVALID",
            )
        imported.add(wave)
    receipt_count = len(imported)
    _require(imported == set(waves[:receipt_count]), "STATE_INVALID")
    if state["checkpoint_initialized"]:
        count = state["imported_wave_count"]
        _require(0 <= count <= len(waves), "STATE_INVALID")
        state_tasks = sum(task["wave"] in waves[:count] for task in tasks)
        _require(state["imported_tasks"] == state_tasks, "STATE_INVALID")
        # Import receipts can precede the final atomic controller-state write.
        _require(count <= receipt_count, "STATE_INVALID")
    counts = {
        "total_tasks": len(tasks),
        "imported_tasks": sum(task["wave"] in imported for task in tasks),
        "total_waves": len(waves),
        "imported_waves": receipt_count,
    }
    status = "complete" if receipt_count == len(waves) else "unfinished" if exists else "missing"
    return counts, status, state["last_action"]


def _candidate(root: Path, relative: str, results: str, manifest: dict) -> dict:
    counts, state, last_action = _counts(root, relative, manifest)
    compatible = manifest["version"] in {6, 7}
    return {
        "manifest": relative,
        "results_dir": results,
        "source_artifact": manifest["source_artifact"],
        "pass_kind": manifest.get("pass_kind", "translation"),
        "manifest_version": manifest["version"],
        "compatible": compatible,
        "step_command": [
            "game-l10n", "native-pass-step", "--workspace", str(root),
            "--manifest", relative, "--results-dir", results,
        ] if compatible else None,
        "state": state if compatible else "incompatible",
        "last_action": last_action,
        "counts": counts,
        "route_binding": dict(manifest["route_binding"]) if manifest.get("route_binding") else None,
        "current_child": None,
    }


def _chain(root: Path, path: Path, manifests: dict[str, dict]) -> dict:
    chain = _metadata(path)
    _require(
        set(chain) == {"schema", "root_manifest", "root_plan_fingerprint", "manifests",
                       "current_manifest", "current_results", "complete"}
        and chain.get("schema") == NATIVE_REVIEW_SPLIT_CHAIN_SCHEMA
        and isinstance(chain.get("manifests"), list) and bool(chain["manifests"])
        and all(isinstance(item, str) for item in chain["manifests"])
        and len(set(chain["manifests"])) == len(chain["manifests"])
        and chain.get("root_manifest") == chain["manifests"][0]
        and chain.get("current_manifest") == chain["manifests"][-1]
        and type(chain.get("complete")) is bool, "SPLIT_INVALID",
    )
    previous = ""
    root_source = None
    root_route = None
    for depth, relative in enumerate(chain["manifests"]):
        if relative not in manifests:
            manifests[relative] = _manifest(root, relative)
        manifest = manifests[relative]
        split = manifest.get("review_split", {})
        if depth == 0:
            plan = _native_control_plan_fingerprint(manifest)
            _require(
                chain["root_plan_fingerprint"] == plan
                and path == _review_split_chain_path(root, plan), "SPLIT_INVALID",
            )
            root_source = manifest["source_fingerprint"]
            root_route = manifest.get("route_binding")
        _require(
            manifest["version"] == 7 and manifest.get("pass_kind") == "review"
            and split.get("depth") == depth and split.get("semantic_passes") == depth + 1
            and split.get("parent_manifest") == previous
            and split.get("root_source_fingerprint") == root_source
            and manifest.get("route_binding") in (None, root_route), "SPLIT_INVALID",
        )
        previous = relative
    results = _directory(root, chain["current_results"])
    if len(chain["manifests"]) > 1:
        current = root / chain["current_manifest"]
        _require(results == current.parent.parent / "results", "SPLIT_INVALID")
    return chain


def native_resume_status(work_dir: Path, *, manifest_path: Path | None = None) -> dict:
    """Discover bound checkpoints without reading text or advancing a controller.

    Only relay dispatches, split-chain metadata, their exact manifests, controller
    states and wave receipts are read. No result location is inferred or created.
    """
    output: dict[str, Any] = {
        "schema": NATIVE_RESUME_STATUS_SCHEMA, "status": "missing",
        "selected": None, "candidates": [], "issue_codes": [],
    }
    try:
        _require(not any(_linked(path) for path in (work_dir, *work_dir.parents)), "PATH_INVALID")
        root = work_dir.resolve()
        selected_manifest = None
        if manifest_path is not None:
            supplied = manifest_path if manifest_path.is_absolute() else root / manifest_path
            try:
                selected_manifest = supplied.relative_to(root).as_posix()
            except ValueError:
                raise _ResumeIssue("PATH_INVALID") from None
            _contained(root, selected_manifest)
        relay_dir = _directory(root, ".game-l10n/native/relay")
        split_dir = _directory(root, ".game-l10n/native/review-splits")
        manifests: dict[str, dict] = {}
        bindings: dict[str, str] = {}
        if relay_dir.exists():
            for path in sorted(relay_dir.iterdir()):
                if not path.match("*-wave-*.json") or path.name.endswith(("-receipt.json", "-launch.json")):
                    continue
                _contained(root, _relative(root, path))
                relative, results = _relay(root, path, manifests)
                _require(relative not in bindings or bindings[relative] == results, "RESULTS_CONFLICT")
                bindings[relative] = results
        chains = []
        if split_dir.exists():
            for directory in sorted(split_dir.iterdir()):
                _contained(root, _relative(root, directory))
                if not directory.is_dir():
                    continue
                path = _contained(root, _relative(root, directory / "chain.json"))
                if path.exists():
                    chains.append(_chain(root, path, manifests))
        owners: dict[str, str] = {}
        for chain in chains:
            for relative in chain["manifests"]:
                _require(relative not in owners, "SPLIT_CONFLICT")
                owners[relative] = chain["root_manifest"]
            current = chain["current_manifest"]
            results = chain["current_results"]
            _require(current not in bindings or bindings[current] == results, "RESULTS_CONFLICT")
            bindings[current] = results
        for relative, manifest in manifests.items():
            if manifest.get("version") == 7 and manifest.get("pass_kind") == "review":
                if manifest["review_split"]["depth"] > 0:
                    _require(relative in owners, "SPLIT_INVALID")
        candidates = {}
        for relative, results in sorted(bindings.items()):
            candidates[relative] = _candidate(root, relative, results, manifests[relative])
        for chain in chains:
            root_relative = chain["root_manifest"]
            _require(root_relative in candidates, "RESULTS_MISSING")
            candidate = candidates[root_relative]
            current = chain["current_manifest"]
            if current != root_relative:
                candidate["current_child"] = candidates[current]
                candidate["current_child"]["step_command"] = None
            _require(not chain["complete"] or candidates[current]["state"] == "complete", "SPLIT_INVALID")
            candidate["state"] = "complete" if chain["complete"] else "unfinished"
        visible = [candidate for relative, candidate in candidates.items()
                   if relative not in owners or owners[relative] == relative]
        output["candidates"] = visible
        if selected_manifest is not None:
            _require(selected_manifest in bindings, "MANIFEST_NOT_FOUND")
            selected_relative = owners.get(selected_manifest, selected_manifest)
            choices = [candidates[selected_relative]]
        else:
            unfinished = [candidate for candidate in visible if candidate["state"] in {"unfinished", "missing"}]
            choices = unfinished or visible
        if len(choices) == 1:
            output["selected"] = choices[0]
            if choices[0]["compatible"]:
                output["status"] = "ready"
            else:
                output["status"] = "invalid"
                output["issue_codes"] = ["E_NATIVE_RESUME_INCOMPATIBLE"]
        elif choices:
            output["status"] = "selection_required"
            output["issue_codes"] = ["E_NATIVE_RESUME_SELECTION_REQUIRED"]
    except _ResumeIssue as exc:
        output.update(status="invalid", selected=None, candidates=[], issue_codes=[exc.code])
    except (OSError, UnicodeError):
        output.update(status="invalid", selected=None, candidates=[],
                      issue_codes=["E_NATIVE_RESUME_METADATA_UNREADABLE"])
    except (ValueError, TypeError, KeyError, RecursionError):
        output.update(status="invalid", selected=None, candidates=[],
                      issue_codes=["E_NATIVE_RESUME_METADATA_INVALID"])
    return output
