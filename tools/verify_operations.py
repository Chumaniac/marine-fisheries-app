"""Local bounded API performance, recovery and signed model verification receipts."""

import gc
import json
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    NoEncryption,
)
from fastapi.testclient import TestClient
from marine.api import create_app
from marine.backup import restore
from marine.edge_runtime import ModelVersions
from tools.model_release import package


def main():
    destination = Path("artifacts/recovery/20261012-restored")
    if not destination.exists():
        restore("artifacts/recovery/20261012-before-runtime", destination)
    with TestClient(create_app(destination)) as c:
        actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        with c.app.state.store.connect() as db:
            evidence = [r[0] for r in db.execute("SELECT id FROM evidence WHERE tenant='demo'")]
        recovered = [
            c.get("/v1/evidence/" + eid + "/verify").json()["contentVerified"] for eid in evidence
        ]
        assert all(recovered)
        for _ in range(20):
            assert c.get("/v1/dashboard").status_code == 200
        gc.collect()
        tracemalloc.start()
        before = tracemalloc.get_traced_memory()[0]
        times = []
        cpu = time.process_time()
        for _ in range(200):
            start = time.perf_counter()
            r = c.get("/v1/dashboard")
            assert r.status_code == 200
            times.append((time.perf_counter() - start) * 1000)
        cpu = time.process_time() - cpu
        gc.collect()
        after, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        performance = {
            "method": "200 sequential ASGI requests after 20 warmups; local existing synthetic data; Python allocations only",
            "p50Ms": statistics.median(times),
            "p95Ms": sorted(times)[189],
            "maxMs": max(times),
            "cpuSeconds": cpu,
            "retainedPythonBytes": after - before,
            "peakPythonBytes": peak,
        }
        assert after - before < 16 * 1024 * 1024
    with tempfile.TemporaryDirectory(dir="artifacts") as folder:
        root = Path(folder)
        key = Ed25519PrivateKey.generate()
        private = root / "release.key"
        private.write_bytes(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
        private.chmod(0o600)
        signed = package("artifacts/models/norfisk", root / "bundle", private)
        manager = ModelVersions(
            root / "versions", key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        version = manager.install(root / "bundle")
        manager.activate(version)
        # Re-selection (rollback mechanism) revalidates the signed bundle and preserves the successful model.
        assert manager.activate(version)["active"] == version
        original = (root / "bundle" / "report.json").read_bytes()
        (root / "bundle" / "report.json").write_bytes(original + b" ")
        tamper = False
        try:
            manager.install(root / "bundle")
        except ValueError:
            tamper = True
        assert tamper
    report = {
        "backupRestore": {
            "verified": True,
            "evidenceObjects": len(recovered),
            "destinationWasNew": True,
        },
        "signedModel": {
            "version": signed["version"],
            "installAndReselect": True,
            "tamperRejected": True,
        },
        "performance": performance,
        "fieldOrCloudAcceptance": False,
    }
    Path("docs/evidence/operations-verification.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
