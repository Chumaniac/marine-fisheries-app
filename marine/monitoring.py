"""Bounded confidence drift and measured work counters; never self-labels samples."""

import json
import math
import time

from marine.storage import canonical


def psi(reference, current):
    if len(reference) != len(current) or not reference or sum(reference) <= 0 or sum(current) <= 0:
        raise ValueError("Comparable histograms required")
    a = [max(n / sum(reference), 1e-6) for n in reference]
    b = [max(n / sum(current), 1e-6) for n in current]
    return sum((y - x) * math.log(y / x) for x, y in zip(a, b))


class Monitor:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS inference_metrics(tenant TEXT,version TEXT,payload TEXT,created REAL)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS inference_scope ON inference_metrics(tenant,version,created)"
            )

    def record(self, tenant, result, elapsed, byte_count):
        item = {
            "confidence": result["confidence"],
            "label": result["speciesId"],
            "milliseconds": elapsed * 1000,
            "bytes": byte_count,
        }
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO inference_metrics VALUES(?,?,?,?)",
                (tenant, result["modelVersion"], canonical(item).decode(), time.time()),
            )
            db.execute(
                "DELETE FROM inference_metrics WHERE tenant=? AND rowid NOT IN (SELECT rowid FROM inference_metrics WHERE tenant=? ORDER BY rowid DESC LIMIT 500)",
                (tenant, tenant),
            )

    def report(self, tenant, reference=None):
        if reference is None:
            import os
            from pathlib import Path

            path = (
                Path(os.getenv("MARINE_MODEL_DIR", "artifacts/models/norfisk")) / "calibration.json"
            )
            if path.is_file():
                try:
                    reference = json.loads(path.read_text())
                except (ValueError, OSError):
                    pass
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT version,payload FROM inference_metrics WHERE tenant=? ORDER BY rowid DESC LIMIT 500",
                (tenant,),
            ).fetchall()
            reviews = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM reviews WHERE tenant=? LIMIT 1000", (tenant,)
                )
            ]
        version = rows[0]["version"] if rows else None
        items = [json.loads(r["payload"]) for r in rows if r["version"] == version]
        histogram = [0] * 10
        for r in items:
            histogram[min(9, int(r["confidence"] * 10))] += 1
        baseline = (
            reference
            and reference.get("modelVersion") == version
            and reference.get("activeConfidenceHistogram")
        )
        enough = len(items) >= 100 and baseline
        score = psi(baseline, histogram) if enough else None
        attempts = [h for r in reviews for h in r.get("history", [])]
        measured = [h["elapsedSeconds"] for h in attempts if h.get("elapsedSeconds") is not None]
        return {
            "modelVersion": version,
            "windowSamples": len(items),
            "confidenceHistogram": histogram,
            "confidencePsi": score,
            "driftStatus": "review_required"
            if score is not None and score > 0.2
            else "no_alert"
            if score is not None
            else "insufficient_baseline_or_samples",
            "threshold": 0.2,
            "minimumSamples": 100,
            "humanLabels": len(attempts),
            "reworkLabels": sum(max(0, len(r.get("history", [])) - 1) for r in reviews),
            "measuredLabelSeconds": sum(measured),
            "unmeasuredLabels": len(attempts) - len(measured),
            "inferenceMilliseconds": sum(r["milliseconds"] for r in items),
            "inputBytes": sum(r["bytes"] for r in items),
            "interpretation": "confidence shift warning only; not a proof of accuracy loss or label-cost reduction",
        }
