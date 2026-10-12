from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DENIED_PARTS = {
    ".game-l10n",
    "game-data",
    "game_data",
    "inputs",
    "translations",
    "localized",
    "logs",
    "sdk-export",
}
DENIED_SUFFIXES = {".jsonl", ".csv", ".po", ".mo", ".locres", ".assets", ".bundle"}
MAX_FILE_BYTES = 2 * 1024 * 1024


def tracked_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        shell=False,
    )
    if completed.returncode == 0:
        return [ROOT / line for line in completed.stdout.splitlines() if line]
    return [path for path in ROOT.rglob("*") if path.is_file() and ".git" not in path.parts]


def main() -> int:
    violations: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        folded_parts = {part.casefold() for part in relative.parts}
        if folded_parts & DENIED_PARTS:
            violations.append(f"private-data path: {relative}")
        if path.suffix.casefold() in DENIED_SUFFIXES:
            violations.append(f"game/translation data extension: {relative}")
        if path.exists() and path.stat().st_size > MAX_FILE_BYTES:
            violations.append(f"unexpectedly large file: {relative}")
    if violations:
        print("Repository privacy check failed:", file=sys.stderr)
        for item in violations:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("Repository privacy check passed; no game/translation data paths detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
