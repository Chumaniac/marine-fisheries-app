"""Idempotent local two-organization verification; persists its run ID for resume."""

import json
import time
from pathlib import Path
import httpx


def main():
    checkpoint = Path("artifacts/logistics-verification-state.json")
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text())
    else:
        state = {
            "runId": "software-" + str(int(time.time())),
            "startAt": time.time() - 120,
            "endAt": time.time() - 1,
        }
        checkpoint.write_text(json.dumps(state))
    run = state["runId"]
    parent = run + "-catch"
    child = run + "-processed"
    with httpx.Client(base_url="http://127.0.0.1:8871", timeout=55, trust_env=False) as c:

        def login(org):
            r = c.post(
                "/v1/session",
                params={"organization": org},
                headers={"X-Requested-With": "marine-web"},
            )
            r.raise_for_status()
            c.headers["X-Marine-Scope"] = r.json()["scopeHash"]

        def post(path, body=None):
            r = c.post(path, json=body)
            r.raise_for_status()
            return r.json()

        login("Org1MSP")
        post(
            "/v1/batches",
            {
                "requestId": run + "-create",
                "batchId": parent,
                "speciesId": "saithe",
                "massGrams": 1000,
                "areaCode": "synthetic-test",
            },
        )
        post(
            "/v1/batches/" + parent + "/transitions",
            {"requestId": run + "-land", "expectedRevision": 1, "nextState": "LANDED"},
        )
        post(
            "/v1/batches/" + parent + "/transfers",
            {"requestId": run + "-proposal", "expectedRevision": 2, "target": "Org2MSP"},
        )
        login("Org2MSP")
        post(
            "/v1/transfers/" + run + "-proposal/accept",
            {"requestId": run + "-accept", "expectedRevision": 3},
        )
        post(
            "/v1/batch-transforms",
            {
                "requestId": run + "-transform",
                "parents": {parent: 4},
                "outputs": [{"requestId": run + "-output", "batchId": child, "massGrams": 900}],
                "lossGrams": 100,
            },
        )
        post(
            "/v1/batches/" + child + "/transitions",
            {"requestId": run + "-cold", "expectedRevision": 1, "nextState": "IN_COLD_CHAIN"},
        )
        cold = post(
            "/v1/batches/" + child + "/cold-chain",
            {
                "requestId": run + "-temperatures",
                "startAt": state["startAt"],
                "endAt": state["endAt"],
                "minimumC": -2,
                "maximumC": 7,
                "allowedMinimumC": -3,
                "allowedMaximumC": 4,
                "deviceId": "synthetic-cold-sensor",
            },
        )
        # This endpoint may report unknown; repeated runs reconcile the same command IDs.
        post("/v1/ledger/sync")
        eid = cold["evidenceId"]
        post("/v1/evidence/" + eid + "/pin")
        anchor = post("/v1/evidence/" + eid + "/anchor")
        again = post("/v1/evidence/" + eid + "/anchor")
        assert anchor["txId"] == again["txId"]
        post(
            "/v1/batches/" + child + "/transitions",
            {"requestId": run + "-delivered", "expectedRevision": 2, "nextState": "DELIVERED"},
        )
        post("/v1/ledger/sync")
        verified = c.get("/v1/evidence/" + eid + "/verify").json()
        ledger = c.get("/v1/batches/" + child + "/ledger-verify").json()
        assert (
            verified["contentVerified"]
            and verified["chainVerified"]
            and verified["ipfs"]["verified"]
        )
        assert ledger["verified"] and ledger["chainBatch"]["state"] == "DELIVERED"
        report = {
            "runId": run,
            "dataSource": "synthetic-local-integration",
            "parent": parent,
            "child": child,
            "evidenceId": eid,
            "receipt": anchor,
            "evidence": verified,
            "batch": ledger,
            "anchorRetrySameTx": True,
            "coldExcursionDetected": cold["excursion"],
        }
        Path("docs/evidence/logistics-integration.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
