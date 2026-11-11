"""Production API entry point: preflight, one writer, no credential-bearing URL logs."""

import os
import uvicorn
from marine.config import require_production

if __name__ == "__main__":
    if os.getenv("MARINE_MODE") != "production":
        raise SystemExit("Use tools/run_local.py for local mode")
    require_production()
    uvicorn.run(
        "marine.api:create_app",
        factory=True,
        host="0.0.0.0",
        port=8870,
        workers=1,
        access_log=False,
        proxy_headers=False,
        timeout_graceful_shutdown=55,
    )
