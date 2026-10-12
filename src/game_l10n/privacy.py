from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import GameL10nError

DENIED_PARTS = {
    ".game-l10n",
    "game-data",
    "game_data",
    "inputs",
    "outputs",
    "translations",
    "localized",
    "cache",
    "logs",
}
DENIED_NAME_FRAGMENTS = ("dialogue", "dialog", "strings", "translation", "localized", "game-data")
ALLOWED_TOOL_SUFFIXES = {".py", ".ps1", ".sh", ".cs", ".cpp", ".c", ".h", ".rs", ".js", ".ts", ".json", ".toml", ".md"}
MAX_TOOL_SIZE = 2 * 1024 * 1024


class PrivacyViolation(GameL10nError):
    pass


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def require_external_workdir(work_dir: Path, sdk_root: Path) -> None:
    if is_within(work_dir, sdk_root):
        raise PrivacyViolation(
            f"Agent work directory must be outside the SDK repository: {work_dir.resolve()}"
        )


def inspect_exportable_tool(path: Path, tool_root: Path) -> dict[str, str | int]:
    relative = path.resolve().relative_to(tool_root.resolve())
    lowered_parts = {part.casefold() for part in relative.parts}
    if lowered_parts & DENIED_PARTS:
        raise PrivacyViolation(f"Game-data path cannot be exported: {relative}")
    lowered_name = path.name.casefold()
    if any(fragment in lowered_name for fragment in DENIED_NAME_FRAGMENTS):
        raise PrivacyViolation(f"Data-like filename cannot be exported: {relative}")
    if path.suffix.casefold() not in ALLOWED_TOOL_SUFFIXES:
        raise PrivacyViolation(f"Unsupported tool file type: {relative}")
    size = path.stat().st_size
    if size > MAX_TOOL_SIZE:
        raise PrivacyViolation(f"Tool file is unexpectedly large ({size} bytes): {relative}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": relative.as_posix(), "bytes": size, "sha256": digest}


def scan_tools(tool_root: Path) -> list[dict[str, str | int]]:
    if not tool_root.is_dir():
        raise PrivacyViolation(f"Tool directory does not exist: {tool_root}")
    files = [path for path in sorted(tool_root.rglob("*")) if path.is_file()]
    if not files:
        raise PrivacyViolation("No reusable tool files were found")
    return [inspect_exportable_tool(path, tool_root) for path in files]
