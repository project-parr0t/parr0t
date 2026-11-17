from __future__ import annotations

import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

from unity_inventory import extract_inventory


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {Path(sys.argv[0]).name} INPUT_GAME_ROOT OUTPUT_DIRECTORY", file=sys.stderr)
        return 2
    try:
        from game_l10n.errors import GameL10nError
    except ImportError:
        print("SDK_CORE_WITH_EXTRACTOR_PREPARATION_REQUIRED", file=sys.stderr)
        return 1
    try:
        result = extract_inventory(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
    except (GameL10nError, FileNotFoundError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
