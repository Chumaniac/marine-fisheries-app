"""User-facing data entry, forecast status, and training export workflows."""

import json
import math
import shutil
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, HTTPException, Query, UploadFile, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from marine.storage import canonical


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Site(Strict):
    name: str = Field(min_length=1, max_length=128)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    dataSource: str = Field(pattern=r"^(synthetic|field)$")


class Reading(Strict):
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    deviceId: str = Field(min_length=1, max_length=128)
    occurredAt: float
    temperatureC: float = Field(ge=-5, le=45)
    salinityPsu: float = Field(ge=0, le=50)
    turbidityNtu: float = Field(ge=0, le=4000)


class Catch(Strict):
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    siteId: str = Field(min_length=1, max_length=128)
    speciesId: str = Field(min_length=1, max_length=128)
    originAt: float
    labelAvailableAt: float
    catchKg: float = Field(ge=0, le=1000000)
    effortHours: float = Field(gt=0, le=100000)
    sourceEventIds: list[str] = Field(min_length=1, max_length=500)
    dataSource: str = Field(pattern=r"^(synthetic|field)$")


def registered_sites(store, tenant):
    with store.connect() as db:
        return [
            {"siteId": r["id"], "tenantId": tenant, **json.loads(r["payload"])}
            for r in db.execute(
                "SELECT id,payload FROM registered_sites WHERE tenant=? ORDER BY id LIMIT 1000",
                (tenant,),
            )
        ]


def register_workbench(
    app, store, identity, operator, ingest, EventBatch, labels, LabelBatch, folder
):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS registered_sites(tenant TEXT,id TEXT,payload TEXT,PRIMARY KEY(tenant,id))"
        )

    @app.get("/v1/sites")
    def sites(claims=Depends(identity)):
        return {"sites": registered_sites(store, claims["tenant"])}

    @app.put("/v1/sites/{site_id}")
    def site(site_id: str, body: Site, claims=Depends(operator)):
        if not 1 <= len(site_id) <= 128:
            raise HTTPException(422, "Invalid site ID")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT 1 FROM registered_sites WHERE tenant=? AND id=?",
                (claims["tenant"], site_id),
            ).fetchone()
            if (
                not old
                and db.execute(
                    "SELECT count(*) FROM registered_sites WHERE tenant=?", (claims["tenant"],)
                ).fetchone()[0]
                >= 1000
            ):
                raise HTTPException(429, "Site capacity reached")
            db.execute(
                "INSERT INTO registered_sites VALUES(?,?,?) ON CONFLICT(tenant,id) DO UPDATE SET payload=excluded.payload",
                (claims["tenant"], site_id, canonical(body.model_dump()).decode()),
            )
        return {"siteId": site_id, **body.model_dump()}

    @app.get("/v1/devices")
    def devices(claims=Depends(identity)):
        with store.connect() as db:
            rows = db.execute(
                "SELECT id,payload FROM devices WHERE tenant=? ORDER BY id LIMIT 1000",
                (claims["tenant"],),
            ).fetchall()
        return {"devices": [{"deviceId": r[0], **json.loads(r[1])} for r in rows]}

    @app.post("/v1/environment-readings")
    def reading(body: Reading, claims=Depends(operator)):
        with store.connect() as db:
            device = db.execute(
                "SELECT payload FROM devices WHERE tenant=? AND id=?",
                (claims["tenant"], body.deviceId),
            ).fetchone()
        if not device or not json.loads(device[0])["enabled"]:
            raise HTTPException(403, "请选择已启用的登记设备")
        from uuid import uuid5, NAMESPACE_URL

        at = (
            datetime.fromtimestamp(body.occurredAt, timezone.utc).isoformat().replace("+00:00", "Z")
        )
        event = {
            "schemaVersion": "1.0",
            "eventId": str(uuid5(NAMESPACE_URL, claims["tenant"] + ":" + body.requestId)),
            "eventType": "environment.reading",
            "tenantId": claims["tenant"],
            "siteId": json.loads(device[0])["siteId"],
            "deviceId": body.deviceId,
            "sequenceNo": int(body.occurredAt * 1000),
            "occurredAt": at,
            "receivedAt": at,
            "clockUncertaintyMs": 0,
            "traceId": body.requestId,
            "data": {
                "temperatureC": body.temperatureC,
                "salinityPsu": body.salinityPsu,
                "turbidityNtu": body.turbidityNtu,
                "sensorQuality": "valid",
                "calibrationVersion": "operator-entered",
            },
        }
        return ingest(EventBatch(events=[event]), claims)

    @app.get("/v1/source-events")
    def sources(
        siteId: str = Query(max_length=128),
        before: int = Query(default=0, ge=0, le=2**63 - 1),
        claims=Depends(identity),
    ):
        with store.connect() as db:
            rows = db.execute(
                "SELECT rowid AS cursor,payload,hash FROM events WHERE tenant=? AND json_extract(payload,'$.siteId')=? AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT 501",
                (claims["tenant"], siteId, before, before),
            ).fetchall()
            page = [{**json.loads(row["payload"]), "revision": row["hash"]} for row in rows[:500]]
            ids = {event["data"].get("evidenceId") for event in page} - {None}
            images = (
                {
                    row[0]
                    for row in db.execute(
                        "SELECT id FROM edge_evidence WHERE tenant=? AND id IN ("
                        + ",".join("?" for _ in ids)
                        + ")",
                        (claims["tenant"], *ids),
                    )
                }
                if ids
                else set()
            )
        return {
            "events": [
                {**event, "imageAvailable": event["data"].get("evidenceId") in images}
                for event in page
            ],
            "hasMore": len(rows) > 500,
            "nextCursor": rows[min(len(rows), 500) - 1]["cursor"] if rows else before,
        }

    @app.post("/v1/catches", status_code=201)
    def catch(body: Catch, claims=Depends(operator)):
        rows = []
        with store.connect() as db:
            for identity_ in set(body.sourceEventIds):
                row = db.execute(
                    "SELECT payload FROM events WHERE tenant=? AND id=?",
                    (claims["tenant"], identity_),
                ).fetchone()
                if not row:
                    raise HTTPException(422, "来源观测不存在")
                event = json.loads(row[0])
                at = datetime.fromisoformat(event["occurredAt"].replace("Z", "+00:00")).timestamp()
                if (
                    event["siteId"] != body.siteId
                    or not body.originAt - 1800 <= at <= body.originAt
                ):
                    raise HTTPException(422, "请选择预测起点前30分钟内同站点的观测")
                rows.append(event)
        readings = sorted(
            [e for e in rows if e["eventType"] == "environment.reading"],
            key=lambda e: e["occurredAt"],
        )
        fish = [
            e
            for e in rows
            if e["eventType"] == "fish.observation" and e["data"]["speciesId"] == body.speciesId
        ]
        if not readings or readings[-1]["data"]["sensorQuality"] != "valid" or not fish:
            raise HTTPException(422, "需要关联有效环境读数和该物种的鱼类观测")
        env = readings[-1]["data"]
        hour = datetime.fromtimestamp(body.originAt, timezone.utc).hour
        features = {k: env[k] for k in ["temperatureC", "salinityPsu", "turbidityNtu"]}
        features.update(
            observationCount=len(
                {
                    (e["deviceId"], e["data"]["trackId"])
                    for e in fish
                    if e["data"]["countMethod"] == "track"
                }
            ),
            hourSin=math.sin(2 * math.pi * hour / 24),
            hourCos=math.cos(2 * math.pi * hour / 24),
        )
        payload = {
            "id": body.requestId,
            "siteId": body.siteId,
            "speciesId": body.speciesId,
            "originAt": body.originAt,
            "labelAvailableAt": body.labelAvailableAt,
            "cpue": body.catchKg / body.effortHours,
            "quality": 1,
            "features": features,
            "sourceEventIds": sorted(set(body.sourceEventIds)),
            "dataSource": body.dataSource,
        }
        result = labels(LabelBatch(labels=[payload]), claims)
        return {
            **result,
            "cpue": payload["cpue"],
            "unit": "kg/standardizedHour",
            "labelId": body.requestId,
        }

    @app.get("/v1/forecast-status")
    def forecast_status(
        siteId: str = Query(max_length=128),
        speciesId: str = Query(max_length=128),
        claims=Depends(identity),
    ):
        rows = [
            r
            for r in store.labels(claims["tenant"])
            if r.get("siteId", "synthetic-area-A") == siteId
            and r.get("speciesId", "saithe") == speciesId
            and r["labelAvailableAt"] <= time.time()
        ]
        model = store.model(claims["tenant"], siteId, speciesId)
        with store.connect() as db:
            row = db.execute(
                "SELECT payload FROM forecast_candidates WHERE tenant=? AND site=? AND species=? ORDER BY json_extract(payload,'$.lineage.trainedAt') DESC LIMIT 1",
                (claims["tenant"], siteId, speciesId),
            ).fetchone()
        candidate = json.loads(row[0]) if row else None
        return {
            "matureLabels": len(rows),
            "modelAvailable": bool(model),
            "candidateGate": candidate.get("gate") if candidate else None,
            "message": "已发布模型；预测还需要新鲜观测和环境数据"
            if model
            else "最新候选未通过质量检查，保留原模型"
            if candidate and candidate["gate"] == "rejected"
            else "等待足够的成熟样本及按时间分开的回测数据，当前不生成替代预测",
            "modelVersion": model["version"] if model else None,
        }

    @app.post("/v1/data-import")
    def import_data(file: UploadFile, kind: str = Form(...), claims=Depends(operator)):
        data = file.file.read(400 * 1024 + 1)
        if len(data) > 400 * 1024:
            raise HTTPException(413, "导入文件最多400KiB，请拆分批次")
        value = json.loads(data)
        if kind == "labels":
            return labels(LabelBatch.model_validate(value), claims)
        if kind == "events":
            return ingest(EventBatch.model_validate(value), claims)
        raise HTTPException(422, "Unknown import kind")

    @app.get("/v1/review-training-export")
    def export(check: bool = False, claims=Depends(operator)):
        with store.connect() as db:
            count = db.execute(
                "SELECT count(*) FROM reviews WHERE tenant=? AND json_extract(payload,'$.state')='reviewed' AND json_extract(payload,'$.label')!='unknown'",
                (claims["tenant"],),
            ).fetchone()[0]
        if not count:
            raise HTTPException(422, "尚无已确认物种的人工复核样本")
        if check:
            return {"samples": count, "ready": True}
        temporary = Path(tempfile.mkdtemp(prefix="marine-training-"))
        try:
            from tools.export_reviews import export as export_reviews

            export_reviews(folder, claims["tenant"], temporary / "samples")
            path = temporary / "reviewed-training.zip"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for sample in (temporary / "samples").iterdir():
                    archive.write(sample, sample.name)
            return FileResponse(
                path,
                filename="reviewed-training.zip",
                background=BackgroundTask(shutil.rmtree, temporary),
            )
        except Exception:
            shutil.rmtree(temporary)
            raise
