"""Bounded, restart-safe forecast training and immutable publication snapshots."""

import json
import math
import time
from datetime import datetime, timezone

from marine.forecast import WeightedRegression, backtest, FEATURES
from marine.storage import canonical, digest


def timestamp(event):
    return datetime.fromisoformat(event["occurredAt"].replace("Z", "+00:00")).timestamp()


class ForecastService:
    # Operational guardrails, not claims about field accuracy. Versioned in every release.
    POLICY = {"version": 1, "maxWape": 0.5, "minCoverage": 0.8, "minSamples": 20}

    def __init__(self, store, streamed=False):
        self.store, self.streamed = store, streamed
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS forecast_candidates(tenant TEXT,site TEXT,species TEXT,version TEXT,payload TEXT,PRIMARY KEY(tenant,version));
            CREATE TABLE IF NOT EXISTS forecasts(tenant TEXT,site TEXT,species TEXT,id TEXT,payload TEXT,created REAL,PRIMARY KEY(tenant,id));
            CREATE INDEX IF NOT EXISTS forecast_scope ON forecasts(tenant,site,species,created);
            CREATE TABLE IF NOT EXISTS model_releases(tenant TEXT,site TEXT,species TEXT,version TEXT,created REAL,reason TEXT);
            """)

    def train(self, tenant, site, species, now=None):
        now = time.time() if now is None else now
        rows = [
            r
            for r in self.store.labels(tenant)
            if r.get("siteId", "synthetic-area-A") == site
            and r.get("speciesId", "saithe") == species
            and r["originAt"] < r["labelAvailableAt"] <= now
            and r.get("quality", 1) > 0
        ]
        rows.sort(key=lambda r: (r["originAt"], r["id"]))
        if len({r["dataSource"] for r in rows}) != 1:
            raise ValueError("One provenance class required")
        version = digest(
            {
                "rows": rows,
                "features": FEATURES,
                "algorithm": "weighted-huber-v2",
                "policy": self.POLICY,
            }
        )
        with self.store.connect() as db:
            old = db.execute(
                "SELECT payload FROM forecast_candidates WHERE tenant=? AND version=?",
                (tenant, version),
            ).fetchone()
        if old:
            payload = json.loads(old["payload"])
            with self.store.connect() as db:
                released = db.execute(
                    "SELECT 1 FROM model_releases WHERE tenant=? AND version=?", (tenant, version)
                ).fetchone()
            if payload["gate"] == "passed" and not released:
                self.promote(tenant, version, "recovered-publication")
            return payload
        fitted = WeightedRegression().fit(rows, now)
        metrics = backtest(rows)
        passed = (
            metrics["samples"] >= self.POLICY["minSamples"]
            and metrics["wape"] is not None
            and metrics["wape"] <= self.POLICY["maxWape"]
            and metrics["coverage95"] >= self.POLICY["minCoverage"]
        )
        incumbent = self.store.model(tenant, site, species)
        comparison = None
        if incumbent:
            holdout = rows[-max(10, len(rows) // 5) :]
            old = WeightedRegression.load(incumbent["model"])
            candidate_error = sum(
                abs(r["cpue"] - fitted.predict(r["features"])["point"]) for r in holdout
            ) / len(holdout)
            incumbent_error = sum(
                abs(r["cpue"] - old.predict(r["features"])["point"]) for r in holdout
            ) / len(holdout)
            comparison = {
                "samples": len(holdout),
                "candidateMae": candidate_error,
                "incumbentMae": incumbent_error,
                "scope": "shared temporal calibration segment; not independent field acceptance",
            }
            passed = passed and candidate_error <= incumbent_error * 1.05 + 1e-8
        payload = {
            "comparison": comparison,
            "version": version,
            "model": fitted.dump(),
            "metrics": metrics,
            "dataSource": rows[0]["dataSource"],
            "gate": "passed" if passed else "rejected",
            "policy": self.POLICY,
            "lineage": {
                "inputHash": digest(rows),
                "labelIds": [r["id"] for r in rows],
                "featureVersion": 1,
                "algorithm": "weighted-huber-v2",
                "trainedAt": now,
            },
        }
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO forecast_candidates VALUES(?,?,?,?,?)",
                (tenant, site, species, version, canonical(payload).decode()),
            )
            if passed:
                db.execute(
                    "INSERT INTO scoped_models VALUES(?,?,?,?) ON CONFLICT(tenant,site,species) DO UPDATE SET payload=excluded.payload",
                    (tenant, site, species, canonical(payload).decode()),
                )
                db.execute(
                    "INSERT INTO model_releases VALUES(?,?,?,?,?,?)",
                    (tenant, site, species, version, now, "automatic-gate"),
                )
        return payload

    def promote(self, tenant, version, reason="operator-rollback"):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM forecast_candidates WHERE tenant=? AND version=?", (tenant, version)
            ).fetchone()
            if not row:
                raise KeyError("model")
            model = json.loads(row["payload"])
            if model["gate"] != "passed":
                raise ValueError("Rejected candidate cannot be promoted")
            db.execute(
                "INSERT INTO scoped_models VALUES(?,?,?,?) ON CONFLICT(tenant,site,species) DO UPDATE SET payload=excluded.payload",
                (tenant, row["site"], row["species"], row["payload"]),
            )
            db.execute(
                "INSERT INTO model_releases VALUES(?,?,?,?,?,?)",
                (tenant, row["site"], row["species"], version, time.time(), reason),
            )
        return {"version": version, "status": "active"}

    def publish(self, tenant, site, species, now=None):
        now = time.time() if now is None else now
        model = self.store.model(tenant, site, species)
        if not model:
            return None
        events = self.store.events(tenant, 5001, site=site, species=species, since=now - 1800)
        if len(events) > 5000:
            return None  # Do not publish a forecast from silently truncated source evidence.
        envs = sorted([e for e in events if e["eventType"] == "environment.reading"], key=timestamp)
        # An invalid latest reading suppresses forecasts; never silently reuse an older good reading.
        if (
            not envs
            or envs[-1]["data"]["sensorQuality"] != "valid"
            or timestamp(envs[-1]) < now - 300
        ):
            return None
        observations = [
            e
            for e in events
            if e["eventType"] == "fish.observation" and e["data"]["speciesId"] == species
        ]
        source_ids = [e["eventId"] for e in observations] + [envs[-1]["eventId"]]
        count = len(
            {
                (e["deviceId"], e["data"]["trackId"])
                for e in observations
                if e["data"]["countMethod"] == "track"
            }
        )
        watermark = None
        windows = []
        if self.streamed:
            with self.store.connect() as db:
                windows = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT payload FROM windows WHERE tenant=? AND json_extract(payload,'$.siteId')=? AND json_extract(payload,'$.speciesId')=? AND json_extract(payload,'$.windowEnd')>=? ORDER BY id LIMIT 2001",
                        (tenant, site, species, (now - 1800) * 1000),
                    )
                ]
            if not windows or len(windows) > 2000 or any(w.get("truncated") for w in windows):
                return None
            watermark = max(w["sourceWatermark"] for w in windows)
            if watermark / 1000 < now - 300:
                return None
            if any("trackIds" not in w for w in windows):
                return None  # old windows cannot provide distinct tracks across minutes
            count = len({track for w in windows for track in w["trackIds"]})
        env = envs[-1]["data"]
        hour = datetime.fromtimestamp(now, timezone.utc).hour
        features = {k: env[k] for k in ["temperatureC", "salinityPsu", "turbidityNtu"]}
        features.update(
            observationCount=count,
            hourSin=math.sin(2 * math.pi * hour / 24),
            hourCos=math.cos(2 * math.pi * hour / 24),
        )
        lineage = {
            "featureVersion": 1,
            "features": features,
            "sourceEventIds": sorted(source_ids),
            "windowHash": digest(windows),
            "sourceWatermark": watermark,
            "modelVersion": model["version"],
        }
        identity = digest(lineage)
        snapshot = {
            **WeightedRegression.load(model["model"]).predict(features),
            "forecastId": identity,
            "modelVersion": model["version"],
            "dataSource": model["dataSource"],
            "publishedAt": now,
            "validUntil": min(
                timestamp(envs[-1]), watermark / 1000 if watermark is not None else now
            )
            + 300,
            "horizonMinutes": 60,
            "featureSource": "flink-minute-windows" if self.streamed else "local-reference",
            "sourceWatermark": watermark,
            "lineage": lineage,
        }
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO forecasts VALUES(?,?,?,?,?,?)",
                (tenant, site, species, identity, canonical(snapshot).decode(), now),
            )
            # Retain at most 1000 snapshots per scope; anchored exports retain their own immutable content.
            db.execute(
                "DELETE FROM forecasts WHERE tenant=? AND site=? AND species=? AND id NOT IN (SELECT id FROM forecasts WHERE tenant=? AND site=? AND species=? ORDER BY created DESC LIMIT 1000)",
                (tenant, site, species, tenant, site, species),
            )
        return self.latest(tenant, site, species)

    def latest(self, tenant, site, species):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT payload FROM forecasts WHERE tenant=? AND site=? AND species=? ORDER BY created DESC LIMIT 1",
                (tenant, site, species),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def tick(self):
        with self.store.connect() as db:
            scopes = db.execute(
                "SELECT DISTINCT tenant,json_extract(payload,'$.siteId'),json_extract(payload,'$.speciesId') FROM labels LIMIT 1000"
            ).fetchall()
        results = []
        for tenant, site, species in scopes:
            try:
                self.train(tenant, site or "synthetic-area-A", species or "saithe")
                self.publish(tenant, site or "synthetic-area-A", species or "saithe")
                results.append({"tenant": tenant, "site": site, "status": "checked"})
            except (ValueError, ArithmeticError):
                results.append(
                    {"tenant": tenant, "site": site, "status": "insufficient-or-invalid-data"}
                )
        return results
