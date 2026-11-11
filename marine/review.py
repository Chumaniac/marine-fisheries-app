"""Bounded human review pool, diversity selection, and auditable human labels."""

import hashlib
import csv
import os
from pathlib import Path
import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from marine.storage import canonical, CAPACITIES
from marine.vision import uncertainty_diversity


class HumanLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requestId: str = Field(min_length=8, max_length=128)
    label: str = Field(pattern=r"^(saithe|salmonid|unknown)$")
    expectedRevision: int = Field(ge=1)
    groupId: str = Field(min_length=3, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    notes: str = Field(default="", max_length=1000)


class Reviews:
    def __init__(self, store, objects):
        self.store, self.objects = store, objects
        manifest = Path(
            os.getenv("MARINE_PROTECTED_MANIFEST", "data/processed/norfisk/manifest.csv")
        )
        self.protected = set()
        if manifest.is_file():
            with manifest.open() as file:
                for i, row in enumerate(csv.DictReader(file)):
                    if i >= 100000:
                        raise ValueError("Protected manifest exceeds configured capacity")
                    if row["split"] in {"test", "validation"}:
                        self.protected.add(row["sha256Pixels"])

    def add(self, tenant, data, result):
        embedding = result.pop("_embedding", None)
        pixels_hash = result.pop("_pixelsSha256", None)
        if pixels_hash in self.protected:
            return {"reviewStatus": "heldout_excluded"}
        if embedding is None:
            return {"reviewStatus": "embedding_unavailable"}
        identity = hashlib.sha256(data).hexdigest()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT payload FROM reviews WHERE tenant=? AND id=?", (tenant, identity)
            ).fetchone()
            if old:
                return {"reviewId": identity, "reviewStatus": json.loads(old["payload"])["state"]}
            count, total = db.execute(
                "SELECT count(*),coalesce(sum(json_extract(payload,'$.manifest.byteLength')),0) FROM reviews WHERE tenant=?",
                (tenant,),
            ).fetchone()
            if count >= CAPACITIES['reviews'] or total + len(data) > CAPACITIES['review_bytes']:
                return {"reviewStatus": "capacity_reached"}
            manifest = self.objects.put(data, "human-review", tenant)
            record = {
                "id": identity,
                "prediction": result,
                "embedding": embedding,
                "manifest": manifest,
                "state": "queued",
                "revision": 1,
                "createdAt": time.time(),
                "history": [],
            }
            db.execute(
                "INSERT INTO reviews VALUES(?,?,?)", (tenant, identity, canonical(record).decode())
            )
        return {"reviewId": identity, "reviewStatus": "queued"}


def register_review(app, store, identity, operator):
    app.state.reviews = Reviews(store, app.state.evidence_store)

    @app.get("/v1/reviews")
    def queue(claims=Depends(identity)):
        with store.connect() as db:
            records = [
                json.loads(row["payload"])
                for row in db.execute(
                    "SELECT payload FROM reviews WHERE tenant=? AND json_extract(payload,'$.state')='queued' ORDER BY rowid LIMIT 1000",
                    (claims["tenant"],),
                )
            ]
            total = db.execute(
                "SELECT count(*),sum(CASE WHEN json_extract(payload,'$.state')='queued' THEN 1 ELSE 0 END) FROM reviews WHERE tenant=?",
                (claims["tenant"],),
            ).fetchone()
        queued_total = total[1] or 0
        reviewed_total = total[0] - queued_total
        queued = records
        if not queued:
            return {"items": [], "queued": 0, "reviewed": reviewed_total}
        classes = sorted(queued[0]["prediction"]["probabilities"])
        chosen = uncertainty_diversity(
            [[r["prediction"]["probabilities"][c] for c in classes] for r in queued],
            [r["embedding"] for r in queued],
            min(20, len(queued)),
        )
        return {
            "items": [
                {"id": queued[i]["id"], "revision": queued[i]["revision"], "state": "queued"}
                for i in chosen
            ],
            "queued": queued_total,
            "reviewed": reviewed_total,
            "candidatePool": {
                "considered": len(queued),
                "limit": 1000,
                "hasMore": queued_total > len(queued),
            },
            "strategy": "entropy + embedding diversity + random sentinel; blind labels",
        }

    @app.get("/v1/reviews/{review_id}/image")
    def image(review_id: str, claims=Depends(identity)):
        from fastapi.responses import Response

        with store.connect() as db:
            row = db.execute(
                "SELECT payload FROM reviews WHERE tenant=? AND id=?", (claims["tenant"], review_id)
            ).fetchone()
        if not row:
            raise KeyError("review")
        record = json.loads(row["payload"])
        raw = app.state.evidence_store.read(record["manifest"])
        # Decode and re-encode as PNG: original metadata is not exposed to the browser.
        import cv2
        import numpy as np

        decoded = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        ok, encoded = cv2.imencode(".png", decoded)
        if not ok:
            raise HTTPException(422, "Image unavailable")
        return Response(encoded.tobytes(), media_type="image/png")

    @app.post("/v1/reviews/{review_id}/claim")
    def claim(review_id: str, claims=Depends(operator)):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM reviews WHERE tenant=? AND id=?", (claims["tenant"], review_id)
            ).fetchone()
            if not row:
                raise KeyError("review")
            record = json.loads(row[0])
            lease = record.get("lease", {})
            if lease.get("expiresAt", 0) > time.time() and lease.get("actor") != claims["sub"]:
                raise HTTPException(409, "Review already claimed")
            record["lease"] = {
                "actor": claims["sub"],
                "startedAt": time.time(),
                "expiresAt": time.time() + 900,
            }
            db.execute(
                "UPDATE reviews SET payload=? WHERE tenant=? AND id=?",
                (canonical(record).decode(), claims["tenant"], review_id),
            )
        return {"id": review_id, "expiresAt": record["lease"]["expiresAt"]}

    @app.post("/v1/reviews/{review_id}/label")
    def label(review_id: str, body: HumanLabel, claims=Depends(operator)):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM reviews WHERE tenant=? AND id=?", (claims["tenant"], review_id)
            ).fetchone()
            if not row:
                raise KeyError("review")
            record = json.loads(row["payload"])
            if record["history"] and record["history"][-1]["requestId"] == body.requestId:
                if any(record["history"][-1].get(k) != v for k, v in body.model_dump().items()):
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return {"id": review_id, "revision": record["revision"], "state": record["state"]}
            published = db.execute(
                "SELECT 1 FROM edge_review_links l JOIN events e ON e.tenant=l.tenant AND e.id=l.id WHERE l.tenant=? AND l.review_id=?",
                (claims["tenant"], review_id),
            ).fetchone()
            if published:
                raise HTTPException(
                    409,
                    "Published observation label is immutable; submit a separate correction record",
                )
            lease = record.get("lease", {})
            if lease.get("expiresAt", 0) > time.time() and lease.get("actor") != claims["sub"]:
                raise HTTPException(409, "Review claimed by another operator")
            elapsed = (
                time.time() - lease["startedAt"]
                if lease.get("actor") == claims["sub"] and lease.get("expiresAt", 0) >= time.time()
                else None
            )
            if record["revision"] != body.expectedRevision:
                raise ValueError("REVISION_CONFLICT")
            if len(record["history"]) >= 20:
                raise HTTPException(409, "Review history capacity reached")
            record["revision"] += 1
            record["state"] = "reviewed"
            record["label"] = body.label
            record["history"].append(
                {
                    **body.model_dump(),
                    "reviewer": claims["sub"],
                    "reviewedAt": time.time(),
                    "elapsedSeconds": elapsed,
                }
            )
            db.execute(
                "UPDATE reviews SET payload=? WHERE tenant=? AND id=?",
                (canonical(record).decode(), claims["tenant"], review_id),
            )
        from marine.edge_samples import release_reviews

        release_reviews(store, claims["tenant"])
        return {"id": review_id, "revision": record["revision"], "state": "reviewed"}
