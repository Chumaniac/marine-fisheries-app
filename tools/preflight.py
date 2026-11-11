"""Read-only production readiness validation; outputs no configuration values."""

import json
from marine.config import preflight

if __name__ == "__main__":
    result = preflight()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ready"] else 1)
