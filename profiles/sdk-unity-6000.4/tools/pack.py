import json
from pathlib import Path
import sys
from unity_adapter import build_payload

if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: pack.py COMPLETED_RESULTS_JSON OUTPUT_DIRECTORY')
    try:
        result = build_payload(Path(sys.argv[1]), Path(sys.argv[2]))
    except Exception:
        print('SDK_PACK_FAILED', file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, sort_keys=True))
