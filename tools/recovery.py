"""python -m tools.recovery backup|verify|restore --source ... [--destination ...]."""

import argparse
import json
from marine.backup import backup, restore, verify

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["backup", "verify", "restore"])
    p.add_argument("--source", required=True)
    p.add_argument("--destination")
    p.add_argument("--model")
    a = p.parse_args()
    if a.action == "verify":
        result = verify(a.source)
    else:
        if not a.destination:
            p.error("--destination is required")
        result = (
            backup(a.source, a.destination, a.model)
            if a.action == "backup"
            else restore(a.source, a.destination)
        )
    print(json.dumps(result))
