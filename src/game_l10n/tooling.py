from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import ToolExecutionError
from .coverage import COMPLETION_SCHEMA
from .language_policy import language_policy_digest, target_language_policy
from .store import stable_hash


TOOL_STATE_SCHEMA = "parr0t.tool-state.v2"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_path(path: Path) -> str:
    if path.is_file():
        return _hash_file(path)
    if path.is_dir():
        rows = []
        for item in sorted(path.rglob("*")):
            relative = item.relative_to(path).as_posix()
            if item.is_dir():
                rows.append((relative + "/", "directory"))
            elif item.is_file():
                rows.append((relative, _hash_file(item)))
        return stable_hash(rows)
    return "missing"


def _immutability_snapshot(path: Path) -> str:
    """Detect metadata writes cheaply before checking the content digest."""
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


def _require_pack_input_binding(work_dir: Path, input_path: Path) -> str:
    completion_path = work_dir / ".game-l10n" / "completion-attestation.json"
    try:
        completion = json.loads(completion_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolExecutionError(
            "Pack input requires a readable completion attestation"
        ) from exc
    if (
        not isinstance(completion, dict)
        or completion.get("schema") != COMPLETION_SCHEMA
        or completion.get("target_completion") is not True
        or completion.get("static_completion") is not True
    ):
        raise ToolExecutionError("Pack input has not passed static completion")
    target_language = completion.get("target_language")
    policy = target_language_policy(work_dir)
    binding = target_language.get("binding") if isinstance(target_language, dict) else None
    if (
        not isinstance(target_language, dict)
        or target_language.get("enforced") is not True
        or target_language.get("complete") is not True
        or not isinstance(binding, dict)
        or binding.get("target_language") != policy["target_language"]
        or binding.get("policy_revision") != policy["revision"]
        or binding.get("policy_digest") != language_policy_digest(policy)
    ):
        raise ToolExecutionError(
            "Pack input is not bound to the current target-language policy"
        )
    binding = completion.get("result_artifact")
    if not isinstance(binding, dict):
        raise ToolExecutionError("Completion attestation has no bound result artifact")
    resolved_input = input_path.resolve()
    try:
        resolved_input.relative_to(work_dir.resolve())
    except ValueError as exc:
        raise ToolExecutionError("Pack input must stay inside the workspace") from exc
    if binding.get("path") != str(resolved_input):
        raise ToolExecutionError("Pack input is not the result artifact bound to completion")
    if not resolved_input.is_file():
        raise ToolExecutionError("Completion result artifact is not a readable file")
    input_hash = _hash_path(resolved_input)
    if binding.get("sha256") != input_hash:
        raise ToolExecutionError("Pack input changed after completion attestation")
    if binding.get("size") != resolved_input.stat().st_size:
        raise ToolExecutionError("Pack input size changed after completion attestation")
    return input_hash


def _write_tool_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _write_private_tool_failure(
    work_dir: Path,
    *,
    stage: str,
    returncode: int,
    stdout: str,
    stderr: str,
) -> None:
    """Keep arbitrary adapter output in the data plane, never in the CLI exception."""

    path = work_dir / ".game-l10n" / "tool-logs" / f"{stage}-latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema": "parr0t.private-tool-failure.v1",
                "stage": stage,
                "returncode": returncode,
                "stdout": stdout,
                "stderr": stderr,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def cached_tool_state(
    work_dir: Path, stage: str, *, command: list[str] | None = None
) -> dict[str, Any]:
    """Return metadata-only validity for a cached unpack/pack artifact."""
    if stage not in {"unpack", "pack"}:
        raise ToolExecutionError("stage must be 'unpack' or 'pack'")
    work_dir = work_dir.resolve()
    state_path = work_dir / ".game-l10n" / "tool-state.json"
    if not state_path.is_file():
        return {"status": "missing"}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
    output_hash = _hash_path(output_path)
    if entry.get("output_hash") != output_hash:
        return {"status": "stale", "issue_code": "TOOL_OUTPUT_CHANGED"}
    input_value = entry.get("input_path")
    if not isinstance(input_value, str) or not input_value:
        return {"status": "invalid", "issue_code": "TOOL_INPUT_PATH_MISSING"}
    input_path = Path(input_value).resolve()
    if not input_path.exists():
        return {"status": "stale", "issue_code": "TOOL_INPUT_MISSING"}
    try:
        _require_separate_output(input_path, output_path, work_dir)
    except ToolExecutionError:
        return {"status": "invalid", "issue_code": "TOOL_INPUT_OUTPUT_OVERLAP"}
    if entry.get("completion_attestation_hash") != _completion_binding(work_dir, stage):
        return {"status": "stale", "issue_code": "COMPLETION_ATTESTATION_CHANGED"}
    if stage == "pack":
        try:
            input_hash = _require_pack_input_binding(work_dir, input_path)
        except ToolExecutionError:
            return {"status": "stale", "issue_code": "PACK_INPUT_NOT_COMPLETION_RESULT"}
    else:
        input_hash = _hash_path(input_path)
    if entry.get("input_hash") != input_hash:
        return {"status": "stale", "issue_code": "TOOL_INPUT_CHANGED"}
    stored_command = entry.get("command")
    if (
        not isinstance(stored_command, list)
        or not stored_command
        or any(not isinstance(part, str) for part in stored_command)
    ):
        return {"status": "invalid", "issue_code": "TOOL_COMMAND_INVALID"}
    if stage == "pack" and stored_command[0] == "engine-sdk-verified-pack":
        if len(stored_command) != 2 or not stored_command[1].strip():
            return {"status": "invalid", "issue_code": "TOOL_COMMAND_INVALID"}
        fingerprint = stable_hash(
            {"stage": "pack", "input": input_hash, "output": output_hash, "verifier": stored_command[1]}
        )
    else:
        if command is not None:
            try:
                expected_command = _execution_command(
                    command, input_path=input_path, output_path=output_path, work_dir=work_dir
                )
            except ToolExecutionError:
                return {"status": "invalid", "issue_code": "TOOL_COMMAND_INVALID"}
            if expected_command != stored_command:
                return {"status": "stale", "issue_code": "TOOL_COMMAND_CHANGED"}
        fingerprint = _tool_fingerprint(
            stage=stage,
            command=stored_command,
            input_path=input_path,
            output_path=output_path,
            work_dir=work_dir,
            input_hash=input_hash,
        )
    if entry.get("fingerprint") != fingerprint:
        return {"status": "stale", "issue_code": "TOOL_FINGERPRINT_CHANGED"}
    return {"status": "valid", "artifact_path": str(output_path)}


def _expand(command: list[str], *, input_path: Path, output_path: Path, work_dir: Path) -> list[str]:
    values = {"input": str(input_path), "output": str(output_path), "workdir": str(work_dir)}
    return [part.format(**values) for part in command]


def _execution_command(
    command: list[str], *, input_path: Path, output_path: Path, work_dir: Path
) -> list[str]:
    if not command or any(not isinstance(part, str) for part in command):
        raise ToolExecutionError("Tool command must be a nonempty string list")
    try:
        expanded = _expand(
            command, input_path=input_path, output_path=output_path, work_dir=work_dir
        )
    except (KeyError, ValueError) as exc:
        raise ToolExecutionError("Tool command has invalid placeholders") from exc
    if expanded[0] == "python":
        expanded[0] = sys.executable
    elif not Path(expanded[0]).is_absolute():
        if os.path.dirname(expanded[0]):
            expanded[0] = str((work_dir / expanded[0]).resolve())
        elif executable := shutil.which(expanded[0]):
            expanded[0] = str(Path(executable).resolve())
    return expanded


def _tool_fingerprint(
    *,
    stage: str,
    command: list[str],
    input_path: Path,
    output_path: Path,
    work_dir: Path,
    input_hash: str,
) -> str:
    excluded = {input_path, output_path}
    tool_files: dict[Path, str] = {}
    for part in command:
        candidate = Path(part)
        if not candidate.is_absolute():
            candidate = work_dir / candidate
        candidate = candidate.resolve()
        if candidate not in excluded and candidate not in tool_files and candidate.is_file():
            tool_files[candidate] = _hash_path(candidate)
    return stable_hash(
        {
            "stage": stage,
            "command": command,
            "input": input_hash,
            "tools": [(str(path), digest) for path, digest in tool_files.items()],
        }
    )


def _require_separate_output(input_path: Path, output_path: Path, work_dir: Path) -> None:
    try:
        output_path.relative_to(work_dir)
    except ValueError as exc:
        raise ToolExecutionError("Tool output must stay inside the workspace") from exc
    state_path = work_dir / ".game-l10n" / "tool-state.json"
    if state_path.is_relative_to(output_path) or output_path == state_path.with_name("tool-state.json.tmp"):
        raise ToolExecutionError("Tool output cannot contain or overwrite its cache state")
    if input_path == output_path or (
        input_path.exists() and output_path.exists() and input_path.samefile(output_path)
    ):
        raise ToolExecutionError("Tool output must be separate from its read-only input")
    if input_path.is_dir() and output_path.is_relative_to(input_path):
        raise ToolExecutionError("Tool output cannot be created inside its read-only input tree")
    if output_path.is_dir() and input_path.is_relative_to(output_path):
        raise ToolExecutionError("Tool output cannot contain its read-only input")


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
    work_dir = work_dir.resolve()
    resolved_input = input_path.resolve()
    resolved_output = output_path.resolve()
    _require_separate_output(resolved_input, resolved_output, work_dir)
    if not resolved_input.exists():
        raise ToolExecutionError("Tool input does not exist")
    completion_hash = _completion_binding(work_dir, stage)
    input_hash = (
        _require_pack_input_binding(work_dir, resolved_input)
        if stage == "pack"
        else _hash_path(resolved_input)
    )
    execution_command = _execution_command(
        command, input_path=resolved_input, output_path=resolved_output, work_dir=work_dir
    )
    input_snapshot = _immutability_snapshot(resolved_input)
    fingerprint = _tool_fingerprint(
        stage=stage,
        command=execution_command,
        input_path=resolved_input,
        output_path=resolved_output,
        work_dir=work_dir,
        input_hash=input_hash,
    )
    state_path = work_dir / ".game-l10n" / "tool-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict) or state.get("schema") != TOOL_STATE_SCHEMA:
        state = {"schema": TOOL_STATE_SCHEMA}
    previous = state.get(stage)
    if not isinstance(previous, dict):
        previous = {}
    if (
        not force
        and previous.get("fingerprint") == fingerprint
        and previous.get("input_path") == str(resolved_input)
        and previous.get("input_hash") == input_hash
        and previous.get("output_path") == str(resolved_output)
        and resolved_output.exists()
        and previous.get("output_hash") == _hash_path(resolved_output)
        and previous.get("completion_attestation_hash") == completion_hash
    ):
        return {"stage": stage, "status": "skipped", "reason": "identical environment and input", "fingerprint": fingerprint}

    if stage in state:
        del state[stage]
        _write_tool_state(state_path, state)

    env = os.environ.copy()
    env.update({"GAME_L10N_STAGE": stage, "GAME_L10N_WORKDIR": str(work_dir)})
    try:
        completed = subprocess.run(
            execution_command,
            cwd=work_dir,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            shell=False,
            check=False,
        )
    except OSError as exc:
        _write_private_tool_failure(
            work_dir, stage=stage, returncode=-1, stdout="", stderr=str(exc)
        )
        raise ToolExecutionError(f"E_TOOL_START_FAILED: stage={stage}") from exc
    input_changed = (
        _immutability_snapshot(resolved_input) != input_snapshot
        or _hash_path(resolved_input) != input_hash
    )
    completion_changed = _completion_binding(work_dir, stage) != completion_hash
    output_error = None
    try:
        _require_separate_output(resolved_input, resolved_output.resolve(), work_dir)
    except ToolExecutionError as exc:
        output_error = exc
    if (
        completed.returncode != 0
        or input_changed
        or completion_changed
        or output_error is not None
        or not resolved_output.exists()
    ):
        _write_private_tool_failure(
            work_dir,
            stage=stage,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    if input_changed:
        raise ToolExecutionError(
            f"{stage} tool modified its declared read-only input; discard its output"
        )
    if completion_changed:
        raise ToolExecutionError("Completion attestation changed while packing; discard its output")
    if completed.returncode != 0:
        raise ToolExecutionError(
            f"E_TOOL_EXECUTION_FAILED: stage={stage}; details are in the private tool log"
        )
    if output_error is not None:
        raise output_error
    if not resolved_output.exists():
        raise ToolExecutionError(f"{stage} tool succeeded but did not create: {resolved_output}")
    state[stage] = {
        "fingerprint": fingerprint,
        "input_path": str(resolved_input),
        "input_hash": input_hash,
        "input_snapshot": input_snapshot,
        "output_path": str(resolved_output),
        "output_hash": _hash_path(resolved_output),
        "completion_attestation_hash": completion_hash,
        "command": execution_command,
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
    _require_separate_output(input_path, output_path, work_dir)
    if not output_path.exists():
        raise ToolExecutionError("Verified pack output does not exist")
    input_hash = _require_pack_input_binding(work_dir, input_path)
    output_hash = _hash_path(output_path)
    state_path = work_dir / ".game-l10n" / "tool-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict) or state.get("schema") != TOOL_STATE_SCHEMA:
        state = {"schema": TOOL_STATE_SCHEMA}
    fingerprint = stable_hash(
        {
            "stage": "pack",
            "input": input_hash,
            "output": output_hash,
            "verifier": verifier_fingerprint,
        }
    )
    state["pack"] = {
        "fingerprint": fingerprint,
        "input_path": str(input_path),
        "input_hash": input_hash,
        "input_snapshot": _immutability_snapshot(input_path),
        "output_path": str(output_path),
        "output_hash": output_hash,
        "completion_attestation_hash": _completion_binding(work_dir, "pack"),
        "command": ["engine-sdk-verified-pack", verifier_fingerprint],
    }
    _write_tool_state(state_path, state)
    return {"stage": "pack", "status": "verified", "fingerprint": fingerprint}
