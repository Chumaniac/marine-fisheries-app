"""Disposable synthetic API fixture; never uses configured external services."""

import os
import json
import tempfile
import uvicorn


def seed_source_images(app):
    """Three synthetic observations share one retained test image; no inference."""
    import base64
    import hashlib
    import time
    from marine.storage import canonical

    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXioAAAAASUVORK5CYII=")
    manifest = app.state.evidence_store.put(image, "synthetic-source-image", "demo")
    with app.state.store.connect() as db:
        db.execute("INSERT OR IGNORE INTO edge_evidence VALUES(?,?,?,?,?)", ("demo", manifest["evidenceId"], "synthetic-device", canonical(manifest).decode(), hashlib.sha256(image).hexdigest()))
        for name in ["image-a", "image-b", "image-c"]:
            event = {"eventId": name, "siteId": "synthetic-image-pages", "deviceId": "synthetic-device", "eventType": "fish.observation", "occurredAt": "2026-10-24T00:00:00Z", "data": {"evidenceId": manifest["evidenceId"], "speciesId": "saithe"}}
            payload = canonical(event)
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)", ("demo", name, payload.decode(), hashlib.sha256(payload).hexdigest(), time.time()))


def main():
    if os.getenv("MARINE_SYNTHETIC_CHECK") != "1":
        raise SystemExit("Explicit synthetic-check flag required")
    for key in list(os.environ):
        if key.startswith(("MARINE_", "KAFKA_", "IPFS_")):
            del os.environ[key]
    os.environ["MARINE_MODE"] = "local"
    from marine.api import create_app
    from marine.storage import canonical

    with tempfile.TemporaryDirectory(prefix="marine-quality-") as temp:
        app = create_app(temp)
        seed_source_images(app)
        batch = {
            "batchId": "synthetic-pages",
            "speciesId": "saithe",
            "massGrams": 1000,
            "availableMassGrams": 1000,
            "areaCode": "A",
            "ownerMSP": "FisherMSP",
            "state": "CAUGHT",
            "revision": 1,
            "chainStatus": "LOCAL_ONLY",
            "localPending": False,
        }
        with app.state.store.connect() as db:
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?)",
                ("demo", batch["batchId"], canonical(batch).decode(), 1),
            )
            other = {**batch, "batchId": "synthetic-other"}
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?)",
                ("demo", other["batchId"], canonical(other).decode(), 1),
            )
            for i in range(205):
                db.execute(
                    "INSERT INTO evidence VALUES(?,?,?,?,?,?)",
                    ("demo", f"e-{i}", batch["batchId"], "{}", "UPLOADED", None),
                )
                db.execute(
                    "INSERT INTO cold_issues VALUES(?,?,?,?,?,?)",
                    (
                        "demo",
                        f"c-{i}",
                        batch["batchId"],
                        json.dumps(
                            {
                                "minimumC": 1,
                                "maximumC": 8,
                                "allowedMinimumC": 0,
                                "allowedMaximumC": 5,
                            }
                        ),
                        "OPEN",
                        None,
                    ),
                )
                db.execute(
                    "INSERT INTO ledger_jobs(tenant,id,batch,operation,status) VALUES(?,?,?,?,?)",
                    ("demo", f"l-{i}", batch["batchId"], "synthetic", "pending"),
                )
            db.execute(
                "INSERT INTO outbox(tenant,id,topic,payload,status,attempt) VALUES(?,?,?,?,?,?)",
                ("demo", "original-retry", "synthetic", '{"siteId":"s"}', "action_required", 8),
            )
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ["QUALITY_PORT"]), access_log=False)


if __name__ == "__main__":
    main()
