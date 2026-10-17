import json
from pathlib import Path
import sys
from unity_adapter import extract_inventory

if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: unpack.py SOURCE_ROOT OUTPUT_DIRECTORY')
    try:
        result = extract_inventory(Path(sys.argv[1]), Path(sys.argv[2]))
    except Exception:
        print('SDK_UNPACK_FAILED', file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, sort_keys=True))
