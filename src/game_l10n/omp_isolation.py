"""Validate text-free OMP host message-isolation registration, not an OS sandbox."""
from __future__ import annotations

import json
import os
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

from .control_plane import control_plane_state

ISOLATION_PATH = ".game-l10n/native/omp-isolation.json"
ISOLATION_SCHEMA = "parr0t.omp-isolation.v1"
ISOLATION_ENV = "PARR0T_OMP_GUARD_SESSION"
ISOLATION_ISSUE = "E_NATIVE_MESSAGE_ISOLATION_REQUIRED"


def guard_sha256() -> str:
    """Hash installed template bytes, including in a packaged distribution."""
    return sha256(files("game_l10n").joinpath("templates", "omp-isolation-guard.mjs").read_bytes()).hexdigest()


def require_omp_isolation(work_dir: Path) -> dict[str, Any]:
    """Fail categorically; registration data and filesystem errors never escape."""
    try:
        proof = json.loads((work_dir / ISOLATION_PATH).read_text(encoding="utf-8"))
        session = os.environ.get(ISOLATION_ENV)
        generation = control_plane_state(work_dir).get("generation", 0)
        valid = (
            isinstance(proof, dict)
            and set(proof) == {"schema", "control_generation", "session_id", "process_id", "guard_sha256"}
            and proof["schema"] == ISOLATION_SCHEMA
            and type(proof["control_generation"]) is int
            and proof["control_generation"] == generation
            and isinstance(session, str) and bool(session.strip())
            and isinstance(proof["session_id"], str) and bool(proof["session_id"].strip())
            and proof["session_id"] == session
            and type(proof["process_id"]) is int and proof["process_id"] > 0
            and proof["guard_sha256"] == guard_sha256()
        )
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        valid = False
    if not valid:
        raise ValueError(ISOLATION_ISSUE) from None
    return proof


def runtime_isolation(work_dir: Path, runtime: dict[str, Any]) -> dict[str, Any] | None:
    if runtime["host"] == "omp" and runtime.get("transport", "collaboration") == "collaboration":
        return require_omp_isolation(work_dir)
    return None


def tool_transport_contract() -> dict[str, Any]:
    return {
        "scope": "relay_and_semantic_subagents",
        "subagent_python_tool": "parr0t_exec",
        "subagent_python_tool_device": "xd://parr0t_exec",
        "outbound_messaging_allowed": False,
        "parr0t_exec_arguments": {"argv": "python_arguments_without_executable", "cwd": "optional_absolute_directory"},
        "relay_python_argv_prefix": ["-m", "game_l10n.cli"],
        "relay_workspace_binding": "exact_absolute_workspace",
        "relay_prompt_marker": "PARR0T_OMP_RELAY=<workspace-relative-relay-path>",
        "relay_prompt_marker_required_in_task": True,
        "relay_argv_template": ["-m", "game_l10n.cli", "<verb>", "--workspace", "<absolute-workspace>", "--relay", "<bound-relay>"],
        "native_wave_seal_relay_option": "--dispatch",
        "root_dispatch_instruction": "Put relay_prompt_line as a standalone line in the relay task prompt; never open private artifacts to construct it.",
        "denied_tools": ["eval", "bash", "write_agent", "read_agent", "history", "proc"],
        "semantic_worker_denied_tools": ["task", "wait"],
        "relay_wait_allowed": True,
        "role_bindings": {"0": "text_free_root", "1": "isolated_relay", "2": "semantic_worker"},
        "role_binding_authority": "host_ancestry_and_core_bound_relay_metadata",
        "semantic_worker_delegation_allowed": False,
        "relay_dispatch": "exact_core_task_bindings_only",
        "automatic_child_output_forwarding_allowed": False,
        "boundary": "host_tools_and_messages_not_os_filesystem_or_network_sandbox",
    }
