"""Encrypted capture journal and evidence-before-event transport."""

import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from marine.edge import Outbox
from marine.storage import canonical, digest, CAPACITIES
from marine.evidence_store import EvidenceStore
from marine.evidence_recovery import material_exists


class CaptureJournal:
    def __init__(self, path):
        self.outbox = Outbox(path)
        self.objects = EvidenceStore(Path(path).parent / (Path(path).stem + "-evidence"), require_existing_key=material_exists(self.outbox.path))
        with self.outbox.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS device_sequence(device TEXT PRIMARY KEY,value INTEGER);
            CREATE TABLE IF NOT EXISTS captured(id TEXT PRIMARY KEY,event TEXT,manifest TEXT,review INTEGER,status TEXT,created REAL);
            """)

    def capture(self, event, image, review=False):
        if not image or len(image) > 256 * 1024:
            raise ValueError("Crop exceeds 256 KiB")
        with self.objects.transaction(self.outbox.path), self.outbox.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count, total = db.execute(
                "SELECT count(*),coalesce(sum(json_extract(manifest,'$.byteLength')),0) FROM captured"
            ).fetchone()
            if count >= 1000 or total + len(image) > 256 * 1024 * 1024:
                raise OverflowError("Evidence journal full; archive before resuming acquisition")
            if db.execute("SELECT count(*) FROM pending").fetchone()[0] >= self.outbox.capacity:
                raise OverflowError("Outbox full")
            device = canonical([event["tenantId"], event["deviceId"]]).decode()
            old = db.execute(
                "SELECT value FROM device_sequence WHERE device=?", (device,)
            ).fetchone()
            sequence = old[0] + 1 if old else 0
            db.execute(
                "INSERT INTO device_sequence VALUES(?,?) ON CONFLICT(device) DO UPDATE SET value=excluded.value",
                (device, sequence),
            )
            manifest = self.objects.put(image, "edge-capture", event["tenantId"])
            event = {
                **event,
                "eventId": event.get("eventId") or str(uuid4()),
                "sequenceNo": sequence,
                "data": {**event["data"], "evidenceId": manifest["evidenceId"]},
            }
            db.execute(
                "INSERT INTO captured VALUES(?,?,?,?,?,?)",
                (
                    event["eventId"],
                    canonical(event).decode(),
                    canonical(manifest).decode(),
                    int(review),
                    "waiting_upload",
                    time.time(),
                ),
            )
            db.execute(
                "INSERT INTO pending(id,payload,status) VALUES(?,?,'evidence_pending')",
                (event["eventId"], canonical(event).decode()),
            )
        return event

    def flush(self, base, token):
        accepted = 0
        with self.outbox.connect() as db:
            rows = db.execute(
                "SELECT c.id,c.event,c.manifest,c.review,p.attempt FROM captured c JOIN pending p ON p.id=c.id WHERE p.status='evidence_pending' AND p.next<=? ORDER BY c.created LIMIT 10",
                (time.time(),),
            ).fetchall()
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            for identity, event, manifest, review, attempt in rows:
                try:
                    response = client.post(
                        base.rstrip("/") + "/v1/edge-samples",
                        headers={"Authorization": "Bearer " + token},
                        data={"event": event, "needs_review": str(bool(review)).lower()},
                        files={
                            "image": (
                                "crop.jpg",
                                self.objects.read(json.loads(manifest)),
                                "image/jpeg",
                            )
                        },
                    )
                    if response.status_code in {400, 401, 403, 409, 413, 422}:
                        with self.outbox.connect() as db:
                            db.execute(
                                "UPDATE pending SET status='action_required' WHERE id=?",
                                (identity,),
                            )
                        continue
                    response.raise_for_status()
                    result = response.json()
                    if result.get("sampleId") != identity:
                        raise ValueError("Acknowledgement mismatch")
                    with self.outbox.connect() as db:
                        db.execute("DELETE FROM pending WHERE id=?", (identity,))
                        db.execute(
                            "UPDATE captured SET status=? WHERE id=?", (result["status"], identity)
                        )
                    accepted += 1
                except (httpx.HTTPError, ValueError, KeyError):
                    with self.outbox.connect() as db:
                        db.execute(
                            "UPDATE pending SET attempt=attempt+1,next=? WHERE id=?",
                            (time.time() + min(300, 2 ** min(attempt + 1, 8)), identity),
                        )
        with self.outbox.connect() as db:
            waiting = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM captured WHERE status='review_pending' LIMIT 100"
                )
            ]
        with httpx.Client(timeout=5, trust_env=False) as client:
            for identity in waiting:
                try:
                    response = client.get(
                        base.rstrip("/") + "/v1/edge-samples/" + identity,
                        headers={"Authorization": "Bearer " + token},
                    )
                    response.raise_for_status()
                    result = response.json()
                    if result.get("sampleId") == identity and result.get("status") in {
                        "published",
                        "review_rejected",
                    }:
                        with self.outbox.connect() as db:
                            db.execute(
                                "UPDATE captured SET status=? WHERE id=?",
                                (result["status"], identity),
                            )
                except (httpx.HTTPError, ValueError):
                    pass
        return accepted


def release_reviews(store, tenant):
    """Crash-safe retry after human label commit; receiver event IDs remain stable."""
    with store.connect() as db:
        rows = db.execute(
            "SELECT l.id,l.event,r.payload FROM edge_review_links l JOIN reviews r ON r.tenant=l.tenant AND r.id=l.review_id WHERE l.tenant=? AND l.status='review_pending' AND json_extract(r.payload,'$.state')='reviewed' LIMIT 100",
            (tenant,),
        ).fetchall()
    for candidate in rows:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT l.id,l.event,r.payload FROM edge_review_links l JOIN reviews r ON r.tenant=l.tenant AND r.id=l.review_id WHERE l.tenant=? AND l.id=? AND l.status='review_pending' AND json_extract(r.payload,'$.state')='reviewed'",
                (tenant, candidate["id"]),
            ).fetchone()
            if not row:
                continue
            record = json.loads(row["payload"])
            event = json.loads(row["event"])
            if record["label"] == "unknown":
                status = "review_rejected"
            else:
                event["data"]["speciesId"] = record["label"]
                event["data"]["qualityScore"] = 1
                store.accept(tenant, [event], connection=db)
                status = "published"
            db.execute(
                "UPDATE edge_review_links SET status=? WHERE tenant=? AND id=?",
                (status, tenant, row["id"]),
            )
            db.execute(
                "UPDATE edge_admissions SET status=? WHERE tenant=? AND id=?",
                (status, tenant, row["id"]),
            )


def register_edge(app, store, identity, producer, ingest, EventBatch, validator):
    from fastapi import Depends, Form, UploadFile, HTTPException
    from fastapi.responses import Response
    import hashlib

    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS edge_evidence(tenant TEXT,id TEXT,device TEXT,manifest TEXT,hash TEXT,PRIMARY KEY(tenant,id));
        CREATE TABLE IF NOT EXISTS edge_admissions(tenant TEXT,id TEXT,hash TEXT,evidence_id TEXT,device TEXT,status TEXT,review_id TEXT,PRIMARY KEY(tenant,id),UNIQUE(tenant,evidence_id));
        CREATE TABLE IF NOT EXISTS edge_review_links(tenant TEXT,id TEXT,review_id TEXT,event TEXT,status TEXT,PRIMARY KEY(tenant,id));
        """)

    @app.post("/v1/edge-samples")
    def sample(
        image: UploadFile,
        event: str = Form(...),
        needs_review: bool = Form(False),
        claims=Depends(producer),
    ):
        from marine.identity import check_device
        from datetime import datetime, timezone

        payload = json.loads(event)
        if not isinstance(payload, dict):
            raise HTTPException(422, "Observation must be an object")
        if payload.get("tenantId") != claims["tenant"]:
            raise HTTPException(403, "Tenant mismatch")
        payload["receivedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if list(validator.iter_errors(payload)) or payload.get("eventType") != "fish.observation":
            raise HTTPException(422, "Invalid observation")
        check_device(store, claims, payload, app.state.mode != "local")
        if (
            datetime.fromisoformat(payload["occurredAt"].replace("Z", "+00:00")).timestamp()
            > time.time() + 30
        ):
            raise HTTPException(422, "Future observation")
        image_bytes = image.file.read(256 * 1024 + 1)
        if not image_bytes or len(image_bytes) > 256 * 1024:
            raise HTTPException(413, "Crop too large")
        from PIL import Image
        import io

        try:
            with Image.open(io.BytesIO(image_bytes)) as header:
                if (
                    header.format not in {"PNG", "JPEG", "WEBP"}
                    or not 256 <= header.width * header.height <= 16_000_000
                ):
                    raise ValueError("Invalid crop dimensions")
                header.verify()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise HTTPException(422, "Invalid image evidence") from None
        fingerprint = hashlib.sha256(image_bytes).hexdigest()
        review = needs_review or payload["data"]["confidence"] < 0.8
        request_hash = digest(
            {
                "event": {k: v for k, v in payload.items() if k != "receivedAt"},
                "imageHash": fingerprint,
                "needsReview": review,
            }
        )
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            admission = db.execute(
                "SELECT * FROM edge_admissions WHERE tenant=? AND (id=? OR evidence_id=?)",
                (claims["tenant"], payload["eventId"], payload["data"]["evidenceId"]),
            ).fetchone()
            if admission and (
                admission["id"] != payload["eventId"] or admission["hash"] != request_hash
            ):
                raise ValueError("IDEMPOTENCY_CONFLICT")
            if admission and admission["status"] in {"published", "review_rejected"}:
                return {
                    "sampleId": payload["eventId"],
                    "status": admission["status"],
                    "reviewId": admission["review_id"],
                }
            old = db.execute(
                "SELECT hash,device FROM edge_evidence WHERE tenant=? AND id=?",
                (claims["tenant"], payload["data"]["evidenceId"]),
            ).fetchone()
            prior = db.execute(
                "SELECT id FROM events WHERE tenant=? AND json_extract(payload,'$.data.evidenceId')=? LIMIT 1",
                (claims["tenant"], payload["data"]["evidenceId"]),
            ).fetchone()
            if old and (
                old["hash"] != fingerprint
                or old["device"] != payload["deviceId"]
                or (prior and prior["id"] != payload["eventId"])
            ):
                raise ValueError("IDEMPOTENCY_CONFLICT")
            if not admission:
                if (
                    db.execute(
                        "SELECT count(*) FROM edge_admissions WHERE tenant=?", (claims["tenant"],)
                    ).fetchone()[0]
                    >= CAPACITIES['edge_admissions']
                ):
                    raise HTTPException(429, "Sample admission capacity reached")
                db.execute(
                    "INSERT INTO edge_admissions VALUES(?,?,?,?,?,'received',NULL)",
                    (
                        claims["tenant"],
                        payload["eventId"],
                        request_hash,
                        payload["data"]["evidenceId"],
                        payload["deviceId"],
                    ),
                )
        if not old:
            with app.state.evidence_store.transaction(store.path), store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute('SELECT hash,device FROM edge_evidence WHERE tenant=? AND id=?', (claims['tenant'],payload['data']['evidenceId'])).fetchone()
                used = db.execute("SELECT id FROM events WHERE tenant=? AND json_extract(payload,'$.data.evidenceId')=? LIMIT 1", (claims['tenant'],payload['data']['evidenceId'])).fetchone()
                if current:
                    if current['hash'] != fingerprint or current['device'] != payload['deviceId'] or used and used['id'] != payload['eventId']:
                        raise ValueError('IDEMPOTENCY_CONFLICT')
                else:
                    if db.execute('SELECT count(*) FROM edge_evidence WHERE tenant=?', (claims['tenant'],)).fetchone()[0] >= CAPACITIES['edge_evidence']:
                        raise HTTPException(429, 'Edge evidence capacity reached')
                    manifest = app.state.evidence_store.put(image_bytes, 'edge-capture', claims['tenant'])
                    db.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?)', (claims['tenant'],payload['data']['evidenceId'],payload['deviceId'],canonical(manifest).decode(),fingerprint))

        if review:
            if app.state.classifier is None:
                from marine.inference import Classifier
                import os

                app.state.classifier = Classifier(
                    os.getenv("MARINE_MODEL_DIR", "artifacts/models/norfisk")
                )
            result = app.state.classifier.predict(image_bytes, include_embedding=True)
            selected = app.state.reviews.add(claims["tenant"], image_bytes, result)
            if not selected.get("reviewId"):
                raise HTTPException(409, "Sample cannot enter review pool")
            with store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO edge_review_links VALUES(?,?,?,?,'review_pending')",
                    (
                        claims["tenant"],
                        payload["eventId"],
                        selected["reviewId"],
                        canonical(payload).decode(),
                    ),
                )
                link = db.execute(
                    "SELECT review_id,event,status FROM edge_review_links WHERE tenant=? AND id=?",
                    (claims["tenant"], payload["eventId"]),
                ).fetchone()
                if link["review_id"] != selected["reviewId"] or digest(
                    {k: v for k, v in json.loads(link["event"]).items() if k != "receivedAt"}
                ) != digest({k: v for k, v in payload.items() if k != "receivedAt"}):
                    raise ValueError("IDEMPOTENCY_CONFLICT")
            with store.connect() as db:
                db.execute(
                    "UPDATE edge_admissions SET status='review_pending',review_id=? WHERE tenant=? AND id=?",
                    (selected["reviewId"], claims["tenant"], payload["eventId"]),
                )
            release_reviews(store, claims["tenant"])
            return {
                "sampleId": payload["eventId"],
                "status": link["status"],
                "reviewId": selected["reviewId"],
            }
        ingest(EventBatch(events=[payload]), claims)
        with store.connect() as db:
            db.execute(
                "UPDATE edge_admissions SET status='published' WHERE tenant=? AND id=?",
                (claims["tenant"], payload["eventId"]),
            )
        return {"sampleId": payload["eventId"], "status": "published"}

    @app.get("/v1/edge-samples/{sample_id}")
    def receipt(sample_id: str, claims=Depends(producer)):
        with store.connect() as db:
            row = db.execute(
                "SELECT device,status,review_id,evidence_id FROM edge_admissions WHERE tenant=? AND id=?",
                (claims["tenant"], sample_id),
            ).fetchone()
        if not row or (claims["role"] == "device" and row["device"] != claims.get("deviceId")):
            raise HTTPException(404, "Sample not found")
        with store.connect() as db:
            event = db.execute('SELECT payload FROM events WHERE tenant=? AND id=?', (claims['tenant'],sample_id)).fetchone()
            evidence = db.execute('SELECT 1 FROM edge_evidence WHERE tenant=? AND id=(SELECT evidence_id FROM edge_admissions WHERE tenant=? AND id=?)', (claims['tenant'],claims['tenant'],sample_id)).fetchone()
            link = db.execute("SELECT l.status,l.review_id,l.event,CASE WHEN length(CAST(r.payload AS BLOB))<=262144 THEN CASE WHEN json_valid(r.payload) THEN json_extract(r.payload,'$.state') END END AS review_state FROM edge_review_links l LEFT JOIN reviews r ON r.tenant=l.tenant AND r.id=l.review_id WHERE l.tenant=? AND l.id=?", (claims["tenant"],sample_id)).fetchone()
        def bound(raw):
            try:
                value = json.loads(raw)
                return (value.get('deviceId') == row['device'] and value.get('tenantId') == claims['tenant']
                        and value.get('data', {}).get('evidenceId') == row['evidence_id'])
            except (ValueError, TypeError, AttributeError):
                return False
        status, phase, next_action = 'action_required', 'evidence_saved' if evidence else 'admitted', 'reconcile_original_request'
        if event:
            if bound(event['payload']) and evidence:
                status, phase, next_action = 'published', 'event_committed', 'none'
            else:
                phase = 'evidence_missing' if bound(event['payload']) else 'event_binding_conflict'
        elif link:
            if not bound(link['event']):
                phase = 'review_binding_conflict'
            elif link['status'] == 'review_pending':
                if link['review_state'] == 'queued':
                    status, phase, next_action = 'review_pending', 'review_linked', 'await_human_review'
                else:
                    phase = 'reviewed_publication_pending' if link['review_state'] == 'reviewed' else 'review_missing'
            elif link['status'] == 'review_rejected':
                status, phase, next_action = 'review_rejected', 'review_rejected', 'none'
            else:
                phase = 'event_missing'
        elif row['status'] == 'review_rejected':
            status, phase, next_action = 'review_rejected', 'review_rejected', 'none'

        return {"sampleId":sample_id,"status":status,"reviewId":link['review_id'] if link else row['review_id'],"phase":phase,"nextAction":next_action}

    @app.get("/v1/observations/{event_id}/evidence")
    def evidence(event_id: str, claims=Depends(identity)):
        with store.connect() as db:
            row = db.execute(
                "SELECT e.manifest FROM events v JOIN edge_evidence e ON e.tenant=v.tenant AND e.id=json_extract(v.payload,'$.data.evidenceId') WHERE v.tenant=? AND v.id=?",
                (claims["tenant"], event_id),
            ).fetchone()
        if not row:
            raise HTTPException(404, "Evidence not found")
        raw = app.state.evidence_store.read(json.loads(row[0]))
        extension, media_type = ("bin", "application/octet-stream")
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            extension, media_type = ("png", "image/png")
        elif raw.startswith(b"\xff\xd8\xff"):
            extension, media_type = ("jpg", "image/jpeg")
        return Response(
            raw,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="observation-image.{extension}"'
            },
        )
