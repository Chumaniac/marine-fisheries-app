"""Read-only dependency acceptance. No topic creation, schema migration or deployment."""

import argparse
import json
import os
import time
from pathlib import Path
import httpx
from kafka.admin import KafkaAdminClient
from marine.fabric import Fabric

REQUIRED_TOPICS = {
    "fish.observations.v1",
    "environment.readings.v1",
    "fish.windows.v1",
    "late.events.v1",
    "quarantine.v1",
}

REQUIRED_CHECKS = {
    "applicationReady",
    "singleFlinkJob",
    "kafkaTopics",
    "fabricIdentityAndChannel",
    "independentIpfsNodeIdentities",
}


def verify_dependencies(origin, flink_url):
    checks = dict.fromkeys(REQUIRED_CHECKS, False)
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            checks["applicationReady"] = (
                client.get(origin.rstrip("/") + "/ready").status_code == 200
            )
            jobs = client.get(flink_url.rstrip("/") + "/jobs/overview")
            jobs.raise_for_status()
            checks["singleFlinkJob"] = (
                sum(
                    j["name"] == "marine-event-time-windows" and j["state"] == "RUNNING"
                    for j in jobs.json()["jobs"]
                )
                == 1
            )
    except (httpx.HTTPError, ValueError, KeyError):
        pass  # Named required checks remain false; no missing check can imply readiness.
    admin = None
    try:
        security = (
            json.loads(Path(os.environ["KAFKA_CLIENT_CONFIG_FILE"]).read_text())
            if os.getenv("KAFKA_CLIENT_CONFIG_FILE")
            else {}
        )
        if os.getenv("MARINE_MODE") == "production" and security.get("security_protocol") not in {
            "SSL",
            "SASL_SSL",
        }:
            raise ValueError("TLS required")
        admin = KafkaAdminClient(
            bootstrap_servers=os.environ["KAFKA_BOOTSTRAP"], request_timeout_ms=5000, **security
        )
        checks["kafkaTopics"] = REQUIRED_TOPICS <= set(admin.list_topics())
    except Exception:
        checks["kafkaTopics"] = False
    finally:
        if admin:
            admin.close()
    try:
        Fabric().query("GetBatch", "commission-readonly-probe")
        checks["fabricIdentityAndChannel"] = True
    except (RuntimeError, ValueError, OSError, KeyError):
        checks["fabricIdentityAndChannel"] = False
    try:
        primary = os.environ["IPFS_API"]
        endpoints = [primary] + json.loads(os.getenv("IPFS_REPLICA_APIS", "[]"))
        identities = set()
        with httpx.Client(timeout=5, trust_env=False) as client:
            for endpoint in endpoints:
                response = client.post(endpoint.rstrip("/") + "/api/v0/id")
                response.raise_for_status()
                identities.add(response.json()["ID"])
        checks["independentIpfsNodeIdentities"] = len(identities) >= int(
            os.getenv("MARINE_IPFS_MIN_REPLICAS", "1")
        )
    except (httpx.HTTPError, ValueError, KeyError):
        checks["independentIpfsNodeIdentities"] = False
    return {
        "checkedAt": time.time(),
        "checks": checks,
        "ready": all(checks[name] is True for name in REQUIRED_CHECKS),
        "dependencyChecksPassed": all(checks[name] is True for name in REQUIRED_CHECKS),
        "scope": "dependency-connectivity-only",
        "businessAcceptanceVerified": False,
        "fieldAccepted": False,
        "automaticProductionCutover": False,
        "readOnly": True,
        "productionDeploymentExecuted": False,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--origin", required=True)
    p.add_argument("--flink", required=True)
    p.add_argument("--output")
    a = p.parse_args()
    result = verify_dependencies(a.origin, a.flink)
    if a.output:
        Path(a.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ready"] else 1)
