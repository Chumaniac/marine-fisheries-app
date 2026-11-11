"""Explicit synthetic integration fixture. Never use this script on field-data tenants."""

import argparse
import json
import math
import random
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx


def timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def main(base):
    if not base.startswith(("http://127.0.0.1:", "http://localhost:")):
        raise ValueError("Local loopback only")
    rng = random.Random(42)
    now = time.time()
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        response = client.post("/v1/session", headers={"X-Requested-With": "marine-web"})
        response.raise_for_status()
        client.headers["X-Marine-Scope"] = response.json()["scopeHash"]
        events = []
        for i in range(120):
            at = now - (120 - i) * 15
            events.append(
                {
                    "schemaVersion": "1.0",
                    "eventId": str(uuid4()),
                    "eventType": "fish.observation",
                    "tenantId": "demo",
                    "siteId": "synthetic-area-A",
                    "deviceId": "synthetic-camera",
                    "sequenceNo": i,
                    "occurredAt": timestamp(at),
                    "receivedAt": timestamp(now),
                    "clockUncertaintyMs": 0,
                    "traceId": "synthetic-flow",
                    "data": {
                        "trackId": f"synthetic-track-{i // 2}",
                        "speciesId": "saithe",
                        "confidence": 0.9,
                        "countMethod": "track",
                        "qualityScore": 1.0,
                        "exposureSeconds": 15,
                        "modelVersion": "synthetic-fixture",
                        "preprocessVersion": "fixture",
                        "evidenceId": f"synthetic-evidence-{i}",
                    },
                }
            )
        env = {
            **events[-1],
            "eventId": str(uuid4()),
            "eventType": "environment.reading",
            "data": {
                "temperatureC": 20.0,
                "salinityPsu": 32.0,
                "turbidityNtu": 5.0,
                "sensorQuality": "valid",
                "calibrationVersion": "synthetic",
            },
        }
        response = client.post("/v1/event-batches", json={"events": [*events, env]})
        response.raise_for_status()
        labels = []
        for i in range(240):
            origin = now - (242 - i) * 7200
            count = 10 + 50 * rng.random()
            temperature = 18 + 6 * rng.random()
            features = {
                "observationCount": count,
                "temperatureC": temperature,
                "salinityPsu": 32.0,
                "turbidityNtu": 5.0,
                "hourSin": math.sin(i),
                "hourCos": math.cos(i),
            }
            labels.append(
                {
                    "id": f"synthetic-label-{int(now)}-{i}",
                    "originAt": origin,
                    "labelAvailableAt": origin + 3600,
                    "cpue": max(0, 2 + 0.2 * count + 0.1 * temperature + rng.gauss(0, 0.5)),
                    "features": features,
                    "quality": 1.0,
                    "dataSource": "synthetic",
                }
            )
        client.post("/v1/labels", json={"labels": labels}).raise_for_status()
        trained = client.post("/v1/forecast-models")
        trained.raise_for_status()
        print(
            json.dumps(
                {
                    "source": "synthetic-only",
                    "eventsAccepted": len(events) + 1,
                    "training": trained.json(),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8871")
    main(parser.parse_args().base)
