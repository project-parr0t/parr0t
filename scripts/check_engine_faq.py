from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MARKER = "<!-- parr0t-engine-faq:v1 -->"
TITLE = "# Engine exception runbook"
EMPTY_STATEMENT = "_No SDK-external engine exceptions are currently documented._"
REQUIRED_FIELDS = (
    "Trigger",
    "Automation boundary",
    "Stop condition",
    "Required input or tool",
    "Procedure",
    "Resume verification",
    "Do not",
)
ENTRY_RE = re.compile(r"(?m)^## (.+?)\s*$")


def validate_engine_faq_text(text: str) -> list[str]:
    errors: list[str] = []
    if MARKER not in text:
        errors.append(f"missing schema marker: {MARKER}")
    if TITLE not in text.splitlines():
        errors.append(f"missing title: {TITLE}")

    matches = list(ENTRY_RE.finditer(text))
    if not matches:
        if EMPTY_STATEMENT not in text:
            errors.append(
                "an entry or the explicit no-exceptions statement is required"
            )
        return errors

    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end]
        for field in REQUIRED_FIELDS:
            if not re.search(rf"(?m)^- \*\*{re.escape(field)}:\*\*\s+\S", body):
                errors.append(f"{heading!r} is missing a nonempty {field!r} field")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an engine SDK's manual exception runbook"
    )
    parser.add_argument("path", nargs="?", default="docs/engine-faq.md")
    args = parser.parse_args(argv)
    path = Path(args.path)
    if not path.is_file():
        print(f"Engine exception runbook not found: {path}", file=sys.stderr)
        return 1
    errors = validate_engine_faq_text(path.read_text(encoding="utf-8"))
    if errors:
        print("Engine exception runbook check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Engine exception runbook check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
