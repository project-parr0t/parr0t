from __future__ import annotations

import json
import sys
from pathlib import Path

from unity_textasset import build_payload


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {Path(sys.argv[0]).name} REQUEST_JSON OUTPUT_DIRECTORY", file=sys.stderr)
        return 2
    try:
        result = build_payload(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    except (FileNotFoundError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
