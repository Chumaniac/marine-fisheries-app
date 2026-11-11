"""New isolated synthetic stream: automatic model and forecast publication, no training endpoint."""

import json
import math
import random
import time
from uuid import uuid5, NAMESPACE_URL
from pathlib import Path
from datetime import datetime, timezone
import httpx

p = Path("artifacts/forecast-cycle-state.json")
if p.exists():
    state = json.loads(p.read_text())
else:
    now = time.time()
    state = {"id": "forecast-cycle-" + str(int(now)), "start": now}
    rng = random.Random(42)
    rows = []
    for i in range(150):
        count = 10 + 50 * rng.random()
        temperature = 18 + 6 * rng.random()
        origin = now - (152 - i) * 7200
        rows.append(
            {
                "id": state["id"] + "-" + str(i),
                "siteId": state["id"],
                "speciesId": "saithe",
                "originAt": origin,
                "labelAvailableAt": origin + 3600,
                "cpue": max(0, 2 + 0.2 * count + 0.1 * temperature + rng.gauss(0, 0.5)),
                "quality": 1,
                "dataSource": "synthetic",
                "features": {
                    "observationCount": count,
                    "temperatureC": temperature,
                    "salinityPsu": 32,
                    "turbidityNtu": 5,
                    "hourSin": math.sin(i),
                    "hourCos": math.cos(i),
                },
            }
        )
    state["labels"] = rows
    example = json.loads(Path("contracts/examples.json").read_text())[0]

    def event(suffix, at):
        return {
            **example,
            "eventId": str(uuid5(NAMESPACE_URL, state["id"] + "-" + suffix)),
            "tenantId": "demo",
            "siteId": state["id"],
            "deviceId": "cycle-device",
            "occurredAt": datetime.fromtimestamp(at, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "data": {**example["data"], "trackId": state["id"] + "-track", "speciesId": "saithe"},
        }

    a = event("base", now - 180)
    b = event("advance", now)
    env = {
        **b,
        "eventId": str(uuid5(NAMESPACE_URL, state["id"] + "-env")),
        "eventType": "environment.reading",
        "data": {
            "temperatureC": 20,
            "salinityPsu": 32,
            "turbidityNtu": 5,
            "sensorQuality": "valid",
            "calibrationVersion": "synthetic",
        },
    }
    state["events"] = [a, b, env]
    p.write_text(json.dumps(state))
with httpx.Client(base_url="http://127.0.0.1:8871", timeout=15, trust_env=False) as c:
    actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
    c.headers["X-Marine-Scope"] = actor["scopeHash"]
    c.post("/v1/labels", json={"labels": state["labels"]}).raise_for_status()
    c.post("/v1/event-batches", json={"events": state["events"]}).raise_for_status()
    deadline = time.time() + 240
    while time.time() < deadline:
        r = c.get("/v1/dashboard", params={"siteId": state["id"], "speciesId": "saithe"})
        r.raise_for_status()
        value = r.json()
        forecast = value["prediction"]
        if forecast:
            assert (
                forecast["lineage"]["sourceEventIds"]
                and forecast["featureSource"] == "flink-minute-windows"
            )
            assert forecast["publishedAt"] - state["start"] < 300
            report = {
                "source": "new isolated synthetic data",
                "siteId": state["id"],
                "automaticTraining": True,
                "automaticPublication": True,
                "manualTrainEndpointCalled": False,
                "publicationLatencySeconds": forecast["publishedAt"] - state["start"],
                "forecast": forecast,
            }
            Path("docs/evidence/forecast-cycle.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(
                json.dumps(
                    {"status": "passed", "latencySeconds": report["publicationLatencySeconds"]}
                )
            )
            break
        time.sleep(2)
    else:
        raise RuntimeError(
            "No automatic forecast published within bounded deadline; inspect persisted source state before retry"
        )
