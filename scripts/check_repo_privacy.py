from __future__ import annotations

import re
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
    "batches",
    "audit",
    "logs",
    "sdk-export",
}
DENIED_SUFFIXES = {".jsonl", ".csv", ".po", ".mo", ".locres", ".assets", ".bundle"}
MAX_FILE_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
CJK_LITERAL_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7a3]")
RESOURCE_PATH_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+"
    r"\.(?:bmp|gif|jpe?g|mp3|mp4|ogg|ogv|otf|png|ttf|wav|webp)\b",
    re.IGNORECASE,
)


def is_provenance_text_scope(relative: Path) -> bool:
    normalized = relative.as_posix().casefold()
    return (
        normalized.startswith("docs/")
        or normalized.startswith("tests/")
        or (
            normalized.startswith("src/game_l10n/")
            and len(relative.parts) > 3
        )
    )


def content_violations(relative: Path, text: str) -> list[str]:
    if not is_provenance_text_scope(relative):
        return []

    violations: list[str] = []
    if CJK_LITERAL_RE.search(text):
        violations.append(f"literal localization content in protected SDK scope: {relative}")

    for match in RESOURCE_PATH_RE.finditer(text):
        candidate = match.group(0).casefold()
        components = re.split(r"[/\\]", candidate)
        if not any(part.startswith(("fixture", "synthetic")) for part in components):
            violations.append(f"non-synthetic resource path in protected SDK scope: {relative}")
            break
    return violations


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
        if path.exists() and path.suffix.casefold() in TEXT_SUFFIXES:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                violations.append(f"non-UTF-8 text file: {relative}")
            else:
                violations.extend(content_violations(relative, text))
    if violations:
        print("Repository privacy check failed:", file=sys.stderr)
        for item in violations:
            print(f"- {item}", file=sys.stderr)
        return 1
    print("Repository privacy check passed; no game data or provenance markers detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
