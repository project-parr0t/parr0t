"""Consent-gated setup of the bundled first-party OMP bootstrap."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


ENTRY_NAME = "parr0t-native-process.ts"
EXTENSION_ID = "extension-module:parr0t-native-process"


def _omp(executable: str, *args: str) -> str:
    result = subprocess.run(
        [executable, "config", *args], capture_output=True, encoding="utf-8", timeout=15,
    )
    if result.returncode:
        raise ValueError("E_OMP_BOOTSTRAP_CONFIG_FAILED")
    return result.stdout.strip()


def _extensions(value: object) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(entry, str) for entry in value):
        raise ValueError("E_OMP_BOOTSTRAP_CONFIG_INVALID")
    return value


def _snapshot(executable: str, target: Path, expected: bytes) -> tuple[bool, bool, list[str], list[str]]:
    if target.is_symlink() or (target.exists() and not target.is_file()):
        return False, True, [], []
    try:
        content = target.read_bytes()
    except FileNotFoundError:
        content = None
    if content is not None and content.replace(b"\r\n", b"\n").rstrip(b"\n") != expected.rstrip(b"\n"):
        return False, True, [], []
    global_config = json.loads(_omp(executable, "list", "--scope=global", "--json"))
    effective = json.loads(_omp(executable, "get", "disabledExtensions", "--json"))
    if not isinstance(global_config, dict) or not isinstance(effective, dict):
        raise ValueError("E_OMP_BOOTSTRAP_CONFIG_INVALID")
    global_entry = global_config.get("disabledExtensions", {"value": []})
    if not isinstance(global_entry, dict):
        raise ValueError("E_OMP_BOOTSTRAP_CONFIG_INVALID")
    return content is not None, False, _extensions(global_entry.get("value")), _extensions(effective.get("value"))


def _setup(*, user_approved: bool, prompt: bool) -> dict:
    executable = shutil.which("omp")
    if executable is None:
        raise ValueError("E_OMP_CONTROL_EXECUTABLE_REQUIRED")
    template = Path(__file__).resolve().with_name("templates") / "omp-native-process.mjs"
    if not template.is_file():
        raise ValueError("E_OMP_CONTROL_BOOTSTRAP_REQUIRED")
    agent_dir = Path(_omp(executable, "path"))
    if not agent_dir.is_absolute():
        raise ValueError("E_OMP_BOOTSTRAP_CONFIG_INVALID")
    target = agent_dir / "extensions" / ENTRY_NAME
    expected = f"export {{ default }} from {json.dumps(template.as_uri())};\n".encode("utf-8")
    installed, conflict, global_disabled, effective_disabled = _snapshot(executable, target, expected)
    result = {"path": str(target), "source": str(template)}
    if conflict:
        return {**result, "status": "existing_entry_conflict"}
    actions = []
    if not installed:
        actions.append("install")
    if EXTENSION_ID in global_disabled:
        actions.append("enable_globally")
    if not actions:
        return {**result, "status": "disabled_by_host_scope" if EXTENSION_ID in effective_disabled else "configured"}
    if not user_approved and prompt and sys.stdin.isatty() and sys.stderr.isatty():
        print(
            f"Parr0t first-party OMP extension: {template}\n"
            f"Target: {target}\n"
            f"Apply {', '.join(actions)}? The extension runs local tools as your OMP user. "
            "No download or model/provider changes. [y/N] ", file=sys.stderr, end="", flush=True,
        )
        try:
            user_approved = input().strip().casefold() in {"y", "yes"}
        except EOFError:
            pass
        if not user_approved:
            return {**result, "status": "declined", "actions": actions}
    if not user_approved:
        return {**result, "status": "consent_required", "actions": actions}

    if _snapshot(executable, target, expected) != (installed, conflict, global_disabled, effective_disabled):
        return {**result, "status": "setup_changed", "actions": actions}
    if not installed:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(expected)
    if EXTENSION_ID in global_disabled:
        remaining = [entry for entry in global_disabled if entry != EXTENSION_ID]
        _omp(executable, "set", "disabledExtensions", json.dumps(remaining), "--json")
    installed, conflict, global_disabled, effective_disabled = _snapshot(executable, target, expected)
    if not installed or conflict or EXTENSION_ID in global_disabled:
        raise ValueError("E_OMP_BOOTSTRAP_SETUP_NOT_VERIFIED")
    return {
        **result,
        "status": "disabled_by_host_scope" if EXTENSION_ID in effective_disabled else "configured",
        "actions_applied": actions,
    }


def add_bootstrap_arguments(parser: argparse.ArgumentParser) -> None:
    consent = parser.add_mutually_exclusive_group()
    consent.add_argument("--prompt", action="store_true", help="Ask for consent in an interactive terminal")
    consent.add_argument("--user-approved", action="store_true", help="Apply setup only after explicit user consent")


def run_bootstrap(args: argparse.Namespace) -> int:
    try:
        result = _setup(user_approved=args.user_approved, prompt=args.prompt)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        issue = str(error)
        if not issue.startswith("E_OMP_"):
            issue = "E_OMP_BOOTSTRAP_SETUP_FAILED"
        print(json.dumps({"status": "error", "issue_code": issue}), file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0
