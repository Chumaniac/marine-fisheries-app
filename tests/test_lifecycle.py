import json
import time
from pathlib import Path

from marine.lifecycle import ForecastService
from marine.storage import Store, canonical
from test_forecast import labels


def test_automatic_release_reuse_lineage_and_stale_forecast(tmp_path):
    store = Store(tmp_path / "db")
    service = ForecastService(store)
    now = time.time()
    rows = labels()
    import random

    rng = random.Random(42)
    for row in rows:
        count = 10 + 50 * rng.random()
        row["features"]["observationCount"] = count
        row["cpue"] = 2 + 0.2 * count + rng.gauss(0, 0.5)
    shift = now - rows[-1]["labelAvailableAt"] - 10
    with store.connect() as db:
        for i, r in enumerate(rows):
            r.update(id=str(i), dataSource="synthetic", siteId="A", speciesId="saithe")
            r["originAt"] += shift
            r["labelAvailableAt"] += shift
            db.execute("INSERT INTO labels VALUES(?,?,?)", ("demo", str(i), canonical(r).decode()))
    model = service.train("demo", "A", "saithe", now)
    assert model["gate"] == "passed"
    assert service.train("demo", "A", "saithe", now + 10)["version"] == model["version"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM model_releases").fetchone()[0] == 1
    assert service.publish("demo", "A", "saithe", now) is None
    event = json.loads(Path("contracts/examples.json").read_text())[1]
    from datetime import datetime, timezone

    event.update(
        tenantId="demo",
        siteId="A",
        occurredAt=datetime.fromtimestamp(now, timezone.utc).isoformat(),
    )
    store.accept("demo", [event])
    prediction = service.publish("demo", "A", "saithe", now)
    assert prediction["lineage"]["sourceEventIds"] == [event["eventId"]]
    assert service.publish("demo", "A", "saithe", now + 1)["forecastId"] == prediction["forecastId"]
    assert service.publish("demo", "A", "saithe", now + 301) is None
    assert service.latest("demo", "A", "saithe")["validUntil"] < now + 301
    assert service.latest("other", "A", "saithe") is None


def test_forecast_refuses_incomplete_source_window(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    store = Store(tmp_path / "capacity.sqlite3")
    service = ForecastService(store)
    now = time.time()
    monkeypatch.setattr(
        store,
        "model",
        lambda *args: {"version": "synthetic", "dataSource": "synthetic", "model": {}},
    )
    template = json.loads(Path("contracts/examples.json").read_text())[0]
    events = []
    for i in range(5001):
        event = json.loads(json.dumps(template))
        event.update(
            eventId=f"capacity-{i}",
            siteId="A",
            occurredAt=datetime.fromtimestamp(now, timezone.utc).isoformat(),
        )
        event["data"]["speciesId"] = "saithe"
        events.append(event)
    environment = json.loads(Path("contracts/examples.json").read_text())[1]
    environment.update(
        eventId="capacity-environment",
        siteId="A",
        occurredAt=datetime.fromtimestamp(now, timezone.utc).isoformat(),
    )
    events.append(environment)
    from types import SimpleNamespace
    from marine.lifecycle import WeightedRegression

    monkeypatch.setattr(
        WeightedRegression, "load", lambda _: SimpleNamespace(predict=lambda _: {"point": 1.0})
    )
    store.accept("demo", events)
    assert service.publish("demo", "A", "saithe", now) is None
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 0
