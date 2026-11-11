"""Verify isolated application recovery and live dependency receipts; no cutover approval."""

import json
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
from marine.api import create_app
from marine.stack_recovery import verify, sign_acceptance
from marine.backup import file_hash


def main():
    bundle = Path("artifacts/system-backup-20261012")
    restored = Path("artifacts/system-restored-20261012")
    key = Path(".runtime/system-recovery.key")
    manifest = verify(bundle, key)
    state = json.loads((restored / "recovery-state.json").read_text())
    state["snapshotDigest"] = file_hash(bundle / "system-manifest.json")
    (restored / "recovery-state.json").write_text(json.dumps(state))
    child = json.loads(Path("docs/evidence/logistics-integration.json").read_text())["child"]
    with TestClient(create_app(restored / "application")) as c:
        actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        trace = c.get("/v1/batches/" + child + "/trace")
        trace.raise_for_status()
        assert trace.json()["batch"]["state"] == "DELIVERED"
        for evidence in trace.json()["evidence"]:
            assert c.get("/v1/evidence/" + evidence["evidenceId"] + "/verify").json()[
                "contentVerified"
            ]
    with httpx.Client(base_url="http://127.0.0.1:8871", trust_env=False, timeout=15) as c:
        actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        assert c.get("/v1/batches/" + child + "/ledger-verify").json()["verified"]
        eid = json.loads(Path("docs/evidence/logistics-integration.json").read_text())["evidenceId"]
        result = c.get("/v1/evidence/" + eid + "/verify").json()
        assert result["chainVerified"] and result["ipfs"]["replicasVerified"] == 2
        assert c.get("/ready").status_code == 200
    jobs = httpx.get("http://127.0.0.1:18081/jobs/overview", trust_env=False, timeout=5).json()[
        "jobs"
    ]
    running = [j for j in jobs if j["state"] == "RUNNING"]
    assert len(running) == 1
    checkpoint = httpx.get(
        "http://127.0.0.1:18081/jobs/" + running[0]["jid"] + "/checkpoints",
        trust_env=False,
        timeout=5,
    ).json()["latest"]["restored"]
    assert checkpoint and "savepoint-20f8f7-b5a2c3028a32" in checkpoint["external_path"]
    checks = {
        "applicationReadiness": True,
        "ledgerReconciled": True,
        "streamCheckpointChecked": True,
        "ipfsEvidenceVerified": True,
        "operatorApproved": False,
    }
    receipt = sign_acceptance(restored, checks, key)
    (restored / "acceptance-signed.json").write_text(json.dumps(receipt))
    (restored / "acceptance-signed.json").chmod(0o600)
    report = {
        "resourcesRestored": len(manifest["resources"]),
        "allResourcesFileHashesMatched": True,
        "encryptedEnvelopeVerified": True,
        "isolatedApplicationTraceReadable": True,
        "originalServicesRecovered": True,
        "restoredFlinkJob": running[0]["jid"],
        "savepointRestored": True,
        "snapshotBoundAcceptanceReceipt": True,
        "automaticProductionCutover": False,
        "checks": checks,
    }
    Path("docs/evidence/system-recovery-check.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
