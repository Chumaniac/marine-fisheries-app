import json
import time
from pathlib import Path
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from marine.api import create_app


def test_user_site_device_environment_catch_and_geography(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        assert (
            c.put(
                "/v1/sites/A",
                json={
                    "name": "合成站点",
                    "longitude": 120.123,
                    "latitude": 30.123,
                    "dataSource": "synthetic",
                },
            ).status_code
            == 200
        )
        assert c.put("/v1/devices/manual", json={"siteId": "A", "enabled": True}).status_code == 200
        now = time.time()
        r = c.post(
            "/v1/environment-readings",
            json={
                "requestId": "reading-user-01",
                "deviceId": "manual",
                "occurredAt": now - 120,
                "temperatureC": 20,
                "salinityPsu": 32,
                "turbidityNtu": 5,
            },
        )
        assert r.status_code == 200, r.text
        env_id = r.json()["acceptedEventIds"][0]
        e = json.loads(Path("contracts/examples.json").read_text())[0]
        e.update(
            tenantId="demo",
            siteId="A",
            deviceId="manual",
            occurredAt=datetime.fromtimestamp(now - 130, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        )
        assert c.post("/v1/event-batches", json={"events": [e]}).status_code == 202
        body = {
            "requestId": "catch-user-01",
            "siteId": "A",
            "speciesId": e["data"]["speciesId"],
            "originAt": now - 60,
            "labelAvailableAt": now - 1,
            "catchKg": 10,
            "effortHours": 2,
            "sourceEventIds": [env_id, e["eventId"]],
            "dataSource": "synthetic",
        }
        r = c.post("/v1/catches", json=body)
        assert r.status_code == 201, r.text
        assert r.json()["cpue"] == 5
        assert c.post("/v1/catches", json=body).json() == r.json()
        assert (
            c.get(
                "/v1/forecast-status", params={"siteId": "A", "speciesId": body["speciesId"]}
            ).json()["matureLabels"]
            == 1
        )
        assert (
            c.get("/v1/dashboard", params={"siteId": "A"}).json()["siteLocations"][0]["longitude"]
            == 120.1
        )
        body["requestId"] = "cross-site-01"
        body["siteId"] = "other"
        assert c.post("/v1/catches", json=body).status_code == 422
        bad = {"labels": [{"bad": "row"}]}
        assert (
            c.post(
                "/v1/data-import",
                data={"kind": "labels"},
                files={"file": ("bad.json", json.dumps(bad), "application/json")},
            ).status_code
            == 422
        )
        assert c.get("/v1/review-training-export?check=true").status_code == 422


def test_cold_excursion_blocks_delivery_until_audited_resolution(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        c.post(
            "/v1/batches",
            json={
                "requestId": "cold-create-01",
                "batchId": "cold",
                "speciesId": "saithe",
                "massGrams": 1000,
                "areaCode": "A",
            },
        )
        for i, state in enumerate(["LANDED", "IN_COLD_CHAIN"], 1):
            assert (
                c.post(
                    "/v1/batches/cold/transitions",
                    json={
                        "requestId": "cold-move-" + str(i),
                        "expectedRevision": i,
                        "nextState": state,
                    },
                ).status_code
                == 200
            )
        cold = {
            "requestId": "cold-reading-01",
            "startAt": time.time() - 100,
            "endAt": time.time() - 1,
            "minimumC": -2,
            "maximumC": 8,
            "allowedMinimumC": -3,
            "allowedMaximumC": 4,
            "deviceId": "manual",
        }
        r = c.post("/v1/batches/cold/cold-chain", json=cold)
        assert r.status_code == 200
        move = {"requestId": "cold-deliver-01", "expectedRevision": 3, "nextState": "DELIVERED"}
        assert c.post("/v1/batches/cold/transitions", json=move).status_code == 409
        issue = r.json()["issueId"]
        assert c.get("/v1/batches/cold/trace").json()["coldIssues"][0]["status"] == "OPEN"
        resolve = {
            "requestId": "cold-resolution-01",
            "disposition": "release",
            "reason": "Synthetic QA: inspected retained material",
        }
        r = c.post("/v1/cold-issues/" + issue + "/resolve", json=resolve)
        assert r.status_code == 200
        content = c.get("/v1/evidence/" + r.json()["evidenceId"] + "/content")
        assert content.status_code == 200
        assert json.loads(content.content)["reason"] == resolve["reason"]
        assert c.post("/v1/batches/cold/transitions", json=move).status_code == 200
        assert c.post("/v1/cold-issues/" + issue + "/resolve", json=resolve).status_code == 200
