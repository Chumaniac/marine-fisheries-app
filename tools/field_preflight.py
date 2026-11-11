"""Read a bounded local plan without connecting to devices or executing a trial."""

import argparse
import json
import os
import stat
from pathlib import Path
from marine.field_readiness import field_preflight

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", nargs="?", default="contracts/field-acceptance-plan.json")
    args = parser.parse_args()
    path = Path(args.plan)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise SystemExit("Field plan must be a regular local file")
            raw = source.read(65537)
    except OSError:
        raise SystemExit("Field plan cannot be read") from None
    if len(raw) > 65536:
        raise SystemExit("Field plan exceeds 64 KiB")
    try:
        result = field_preflight(json.loads(raw))
    except ValueError:
        raise SystemExit("Field plan is invalid") from None
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["planFieldsComplete"] else 1)
