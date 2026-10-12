from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .errors import ToolExecutionError
from .store import stable_hash


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
    fingerprint = stable_hash(
        {
            "stage": stage,
            "command": expanded,
            "input": _hash_path(input_path),
            "tools": [(str(path), _hash_path(path)) for path in tool_files],
        }
    )
    state_path = work_dir / ".game-l10n" / "tool-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    previous = state.get(stage, {})
    if not force and previous.get("fingerprint") == fingerprint and output_path.exists():
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
    if completed.returncode != 0:
        raise ToolExecutionError(f"{stage} tool exited {completed.returncode}: {completed.stderr[-4000:]}")
    if not output_path.exists():
        raise ToolExecutionError(f"{stage} tool succeeded but did not create: {output_path}")
    state[stage] = {
        "fingerprint": fingerprint,
        "output_hash": _hash_path(output_path),
        "command": expanded,
    }
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"stage": stage, "status": "completed", "fingerprint": fingerprint}
