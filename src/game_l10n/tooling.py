from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .errors import ToolExecutionError
from .coverage import COMPLETION_SCHEMA
from .store import stable_hash


TOOL_STATE_SCHEMA = "parr0t.tool-state.v2"


def _hash_path(path: Path) -> str:
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if path.is_dir():
        rows = []
        for item in sorted(path.rglob("*")):
            if item.is_file():
                rows.append((item.relative_to(path).as_posix(), hashlib.sha256(item.read_bytes()).hexdigest()))
        return stable_hash(rows)
    return "missing"


def _immutability_snapshot(path: Path) -> str:
    """Cheaply detect accidental input writes without rereading a large game tree."""
    if path.is_file():
        stat = path.stat()
        return stable_hash(("file", stat.st_size, stat.st_mtime_ns))
    if path.is_dir():
        rows = []
        for item in sorted(path.rglob("*")):
            if item.is_file():
                stat = item.stat()
                rows.append(
                    (item.relative_to(path).as_posix(), stat.st_size, stat.st_mtime_ns)
                )
        return stable_hash(rows)
    return "missing"


def _completion_attestation_hash(work_dir: Path) -> str:
    return _hash_path(work_dir / ".game-l10n" / "completion-attestation.json")


def _completion_binding(work_dir: Path, stage: str) -> str | None:
    return _completion_attestation_hash(work_dir) if stage == "pack" else None


def _require_pack_input_binding(work_dir: Path, input_path: Path) -> None:
    completion_path = work_dir / ".game-l10n" / "completion-attestation.json"
    try:
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolExecutionError(
            "Pack input requires a readable completion attestation"
        ) from exc
    if (
        completion.get("schema") != COMPLETION_SCHEMA
        or completion.get("target_completion") is not True
        or completion.get("static_completion") is not True
    ):
        raise ToolExecutionError("Pack input has not passed static completion")
    binding = completion.get("result_artifact")
    if not isinstance(binding, dict):
        raise ToolExecutionError("Completion attestation has no bound result artifact")
    resolved_input = input_path.resolve()
    if binding.get("path") != str(resolved_input):
        raise ToolExecutionError("Pack input is not the result artifact bound to completion")
    if binding.get("sha256") != _hash_path(resolved_input):
        raise ToolExecutionError("Pack input changed after completion attestation")
    if binding.get("size") != resolved_input.stat().st_size:
        raise ToolExecutionError("Pack input size changed after completion attestation")


def _write_tool_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def cached_tool_state(work_dir: Path, stage: str) -> dict[str, Any]:
    """Return metadata-only validity for a cached unpack/pack artifact."""
    if stage not in {"unpack", "pack"}:
        raise ToolExecutionError("stage must be 'unpack' or 'pack'")
    state_path = work_dir / ".game-l10n" / "tool-state.json"
    if not state_path.is_file():
        return {"status": "missing"}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "invalid", "issue_code": "TOOL_STATE_UNREADABLE"}
    if not isinstance(state, dict) or state.get("schema") != TOOL_STATE_SCHEMA:
        return {"status": "invalid", "issue_code": "TOOL_STATE_SCHEMA_INVALID"}
    entry = state.get(stage)
    if not isinstance(entry, dict):
        return {"status": "missing"}
    output_value = entry.get("output_path")
    if not isinstance(output_value, str) or not output_value:
        return {"status": "invalid", "issue_code": "TOOL_OUTPUT_PATH_MISSING"}
    output_path = Path(output_value).resolve()
    try:
        output_path.relative_to(work_dir.resolve())
    except ValueError:
        return {"status": "invalid", "issue_code": "TOOL_OUTPUT_OUTSIDE_WORKSPACE"}
    if not output_path.exists():
        return {"status": "stale", "issue_code": "TOOL_OUTPUT_MISSING"}
    if entry.get("output_hash") != _hash_path(output_path):
        return {"status": "stale", "issue_code": "TOOL_OUTPUT_CHANGED"}
    input_value = entry.get("input_path")
    if not isinstance(input_value, str) or not input_value:
        return {"status": "invalid", "issue_code": "TOOL_INPUT_PATH_MISSING"}
    input_path = Path(input_value).resolve()
    if not input_path.exists():
        return {"status": "stale", "issue_code": "TOOL_INPUT_MISSING"}
    if entry.get("input_snapshot") != _immutability_snapshot(input_path):
        return {"status": "stale", "issue_code": "TOOL_INPUT_CHANGED"}
    if entry.get("completion_attestation_hash") != _completion_binding(work_dir, stage):
        return {"status": "stale", "issue_code": "COMPLETION_ATTESTATION_CHANGED"}
    if stage == "pack":
        try:
            _require_pack_input_binding(work_dir, input_path)
        except ToolExecutionError:
            return {"status": "stale", "issue_code": "PACK_INPUT_NOT_COMPLETION_RESULT"}
    return {"status": "valid", "artifact_path": str(output_path)}


def _expand(command: list[str], *, input_path: Path, output_path: Path, work_dir: Path) -> list[str]:
    values = {"input": str(input_path), "output": str(output_path), "workdir": str(work_dir)}
    return [part.format(**values) for part in command]


def run_cached_tool(
    *,
    stage: str,
    command: list[str],
    input_path: Path,
    output_path: Path,
    work_dir: Path,
    force: bool = False,
) -> dict[str, Any]:
    if stage not in {"unpack", "pack"}:
        raise ToolExecutionError("stage must be 'unpack' or 'pack'")
    resolved_input = input_path.resolve()
    resolved_output = output_path.resolve()
    if resolved_input == resolved_output:
        raise ToolExecutionError("Tool output must be separate from its read-only input")
    if resolved_input.is_dir():
        try:
            resolved_output.relative_to(resolved_input)
        except ValueError:
            pass
        else:
            raise ToolExecutionError("Tool output cannot be created inside its read-only input tree")
    if stage == "pack":
        _require_pack_input_binding(work_dir, resolved_input)
    expanded = _expand(command, input_path=input_path, output_path=output_path, work_dir=work_dir)
    tool_files: list[Path] = []
    excluded = {input_path.resolve(), output_path.resolve()}
    for part in expanded:
        candidate = Path(part)
        if not candidate.is_absolute():
            candidate = work_dir / candidate
        candidate = candidate.resolve()
        if candidate.is_file() and candidate not in excluded:
            tool_files.append(candidate)
    input_hash = _hash_path(input_path)
    input_snapshot = _immutability_snapshot(input_path)
    fingerprint = stable_hash(
        {
            "stage": stage,
            "command": expanded,
            "input": input_hash,
            "tools": [(str(path), _hash_path(path)) for path in tool_files],
        }
    )
    state_path = work_dir / ".game-l10n" / "tool-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict) or state.get("schema") != TOOL_STATE_SCHEMA:
        state = {"schema": TOOL_STATE_SCHEMA}
    previous = state.get(stage, {})
    completion_hash = _completion_binding(work_dir, stage)
    if (
        not force
        and previous.get("fingerprint") == fingerprint
        and previous.get("output_path") == str(resolved_output)
        and previous.get("output_hash") == _hash_path(output_path)
        and previous.get("completion_attestation_hash") == completion_hash
    ):
        return {"stage": stage, "status": "skipped", "reason": "identical environment and input", "fingerprint": fingerprint}

    env = os.environ.copy()
    env.update({"GAME_L10N_STAGE": stage, "GAME_L10N_WORKDIR": str(work_dir)})
    try:
        completed = subprocess.run(
            expanded,
            cwd=work_dir,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise ToolExecutionError(f"Could not start {stage} tool: {exc}") from exc
    if _immutability_snapshot(input_path) != input_snapshot:
        raise ToolExecutionError(
            f"{stage} tool modified its declared read-only input; discard its output"
        )
    if completed.returncode != 0:
        raise ToolExecutionError(f"{stage} tool exited {completed.returncode}: {completed.stderr[-4000:]}")
    if not output_path.exists():
        raise ToolExecutionError(f"{stage} tool succeeded but did not create: {output_path}")
    state[stage] = {
        "fingerprint": fingerprint,
        "input_path": str(resolved_input),
        "input_hash": input_hash,
        "input_snapshot": input_snapshot,
        "output_path": str(resolved_output),
        "output_hash": _hash_path(output_path),
        "completion_attestation_hash": completion_hash,
        "command": expanded,
    }
    _write_tool_state(state_path, state)
    return {"stage": stage, "status": "completed", "fingerprint": fingerprint}


def record_verified_pack_artifact(
    *,
    input_path: Path,
    output_path: Path,
    work_dir: Path,
    verifier_fingerprint: str,
) -> dict[str, Any]:
    """Let an engine SDK bind its already verified pack output to the core state chain."""

    work_dir = work_dir.resolve()
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    if not verifier_fingerprint.strip():
        raise ToolExecutionError("Engine pack verifier fingerprint is required")
    try:
        output_path.relative_to(work_dir)
    except ValueError as exc:
        raise ToolExecutionError("Verified pack output must stay inside the workspace") from exc
    if not output_path.exists():
        raise ToolExecutionError("Verified pack output does not exist")
    _require_pack_input_binding(work_dir, input_path)
    state_path = work_dir / ".game-l10n" / "tool-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict) or state.get("schema") != TOOL_STATE_SCHEMA:
        state = {"schema": TOOL_STATE_SCHEMA}
    input_hash = _hash_path(input_path)
    fingerprint = stable_hash(
        {
            "stage": "pack",
            "input": input_hash,
            "output": _hash_path(output_path),
            "verifier": verifier_fingerprint,
        }
    )
    state["pack"] = {
        "fingerprint": fingerprint,
        "input_path": str(input_path),
        "input_hash": input_hash,
        "input_snapshot": _immutability_snapshot(input_path),
        "output_path": str(output_path),
        "output_hash": _hash_path(output_path),
        "completion_attestation_hash": _completion_binding(work_dir, "pack"),
        "command": ["engine-sdk-verified-pack", verifier_fingerprint],
    }
    _write_tool_state(state_path, state)
    return {"stage": "pack", "status": "verified", "fingerprint": fingerprint}
