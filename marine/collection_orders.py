"""Durable collection metadata and distinct, authenticated handoff receipts."""

import json
import time
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from marine.storage import canonical, digest

MAX_SOURCES = 100
MAX_RECORDS = 128
MAX_REQUESTS = 256
MAX_HANDOFFS = 20


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NewOrder(Strict):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    siteId: str = Field(min_length=1, max_length=128)
    deviceId: str = Field(min_length=1, max_length=128)
    startAt: float
    endAt: float


class Change(Strict):
    requestId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    expectedRevision: int = Field(ge=1)


class SourceRef(Strict):
    eventId: str = Field(min_length=1, max_length=128)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class Sources(Change):
    sources: list[SourceRef] = Field(min_length=1, max_length=MAX_SOURCES)


class Record(Change):
    path: str = Field(min_length=1, max_length=512)
    recordId: str = Field(min_length=1, max_length=128)


class ReuseRecord(Change):
    sourceOrderId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    sourceRequestId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")
    path: str = Field(min_length=1, max_length=512)
    recordId: str = Field(min_length=1, max_length=128)
    evidenceHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    includeSources: bool = Field(default=False, strict=True)


class Offer(Change):
    recipient: str = Field(min_length=1, max_length=200)


def migrate(store):
    statements = [
        "CREATE TABLE IF NOT EXISTS collection_schema(version INTEGER PRIMARY KEY,applied REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS collection_orders(tenant TEXT,id TEXT,creator TEXT,creation_hash TEXT,payload TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(tenant,id))",
        "CREATE TABLE IF NOT EXISTS collection_requests(tenant TEXT,id TEXT,order_id TEXT,actor TEXT,hash TEXT,result TEXT,PRIMARY KEY(tenant,id))",
        "CREATE TABLE IF NOT EXISTS collection_handoffs(tenant TEXT,id TEXT,order_id TEXT,recipient TEXT,state TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,id))",
        "CREATE INDEX IF NOT EXISTS collection_owner ON collection_orders(tenant,creator,id)",
        "CREATE INDEX IF NOT EXISTS collection_request_order ON collection_requests(tenant,order_id)",
        "CREATE INDEX IF NOT EXISTS collection_incoming ON collection_handoffs(tenant,recipient,order_id)",
    ]
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for statement in statements:
            db.execute(statement)
        version = db.execute("SELECT max(version) FROM collection_schema").fetchone()[0]
        if version not in {None, 1}:
            raise RuntimeError("COLLECTION_SCHEMA_UNSUPPORTED")
        db.execute("INSERT OR IGNORE INTO collection_schema VALUES(1,?)", (time.time(),))


def register_collections(app, store, identity, operator, external_identity, validator):
    migrate(store)

    def members(tenant):
        if external_identity is None:
            return {}
        with external_identity.members.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise HTTPException(503, "成员目录超过容量，请联系管理员")
        directory = json.loads(raw)
        if not isinstance(directory, dict) or len(directory) > 5000:
            raise HTTPException(503, "成员目录不可用")
        return {
            subject: {
                "subject": subject,
                "role": member["role"],
                "organization": member["organization"],
            }
            for subject, member in directory.items()
            if isinstance(subject, str)
            and 1 <= len(subject) <= 200
            and isinstance(member, dict)
            and member.get("tenant") == tenant
            and not member.get("disabled")
            and member.get("role") in {"operator", "auditor"}
            and member.get("organization") in {"Org1MSP", "Org2MSP"}
        }

    def read_order(db, tenant, order_id):
        row = db.execute(
            "SELECT payload FROM collection_orders WHERE tenant=? AND id=?", (tenant, order_id)
        ).fetchone()
        if not row:
            raise HTTPException(404, "工单不存在或不可访问")
        return json.loads(row[0])

    def accessible(db, order, claims):
        if order["createdBy"] == claims["sub"] or claims["role"] == "auditor":
            return
        found = db.execute(
            "SELECT 1 FROM collection_handoffs WHERE tenant=? AND order_id=? AND recipient=? LIMIT 1",
            (claims["tenant"], order["id"], claims["sub"]),
        ).fetchone()
        if not found:
            raise HTTPException(404, "工单不存在或不可访问")

    def view(db, order):
        handoffs = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT payload FROM collection_handoffs WHERE tenant=? AND order_id=? ORDER BY rowid LIMIT 20",
                (order["tenant"], order["id"]),
            )
        ]
        return {
            **order,
            "handoffs": handoffs,
            "closed": order["state"] != "open",
            "serverPersisted": True,
        }

    def device_ok(db, order):
        site = db.execute(
            "SELECT 1 FROM registered_sites WHERE tenant=? AND id=?",
            (order["tenant"], order["siteId"]),
        ).fetchone()
        row = db.execute(
            "SELECT payload FROM devices WHERE tenant=? AND id=?",
            (order["tenant"], order["deviceId"]),
        ).fetchone()
        device = json.loads(row[0]) if row else {}
        if not site or not device.get("enabled") or device.get("siteId") != order["siteId"]:
            raise HTTPException(403, "工单站点或设备已失效，请先核对")

    def source_ref(db, order, event_id, expected=None):
        row = db.execute(
            "SELECT payload,hash FROM events WHERE tenant=? AND id=?", (order["tenant"], event_id)
        ).fetchone()
        if not row:
            raise HTTPException(422, "来源记录不存在")
        event = json.loads(row[0])
        if (
            digest({key: value for key, value in event.items() if key != "receivedAt"})
            != row["hash"]
        ):
            raise HTTPException(409, "来源内容与记录摘要不符")
        if len(event["occurredAt"]) > 64 or len(event["deviceId"]) > 128:
            raise HTTPException(422, "来源元数据超过工单限制")
        at = datetime.fromisoformat(event["occurredAt"].replace("Z", "+00:00")).timestamp()
        if (
            event["eventType"] not in {"environment.reading", "fish.observation"}
            or event["siteId"] != order["siteId"]
            or not order["startAt"] <= at <= order["endAt"]
        ):
            raise HTTPException(422, "来源不在工单站点或时间范围内")
        if expected is not None and expected != row["hash"]:
            raise HTTPException(409, "来源版本已变化，请重新核对")
        return {
            **{
                key: event[key]
                for key in ["eventId", "eventType", "siteId", "deviceId", "occurredAt"]
            },
            "revision": row["hash"],
        }

    def sources_current(db, order):
        for source in order["sources"]:
            source_ref(db, order, source["eventId"], source["revision"])

    def record_ref(db, order, body):
        tenant = order["tenant"]
        kind, revision, state, chain = "", None, "received", None
        if body.path == "/v1/environment-readings":
            expected = str(uuid5(NAMESPACE_URL, tenant + ":" + body.requestId))
            if body.recordId != expected:
                raise HTTPException(422, "收件记录与原请求不符")
            source = source_ref(db, order, body.recordId)
            if (
                source["deviceId"] != order["deviceId"]
                or source["eventType"] != "environment.reading"
            ):
                raise HTTPException(422, "环境记录设备不符")
            kind, fingerprint = "environment", source["revision"]
        elif body.path == "/v1/catches":
            row = db.execute(
                "SELECT payload FROM labels WHERE tenant=? AND id=?", (tenant, body.requestId)
            ).fetchone()
            if not row or body.recordId != body.requestId:
                raise HTTPException(422, "渔获收件记录不存在")
            label = json.loads(row[0])
            if (
                label["siteId"] != order["siteId"]
                or not order["startAt"] <= label["originAt"] <= order["endAt"]
            ):
                raise HTTPException(422, "渔获记录不在工单范围内")
            if not set(label["sourceEventIds"]).issubset(
                {source["eventId"] for source in order["sources"]}
            ):
                raise HTTPException(422, "请先关联渔获使用的来源")
            kind, fingerprint = "catch", digest(label)
        elif body.path == "/v1/batches":
            saved = db.execute(
                "SELECT payload FROM receipts WHERE tenant=? AND id=?", (tenant, body.requestId)
            ).fetchone()
            if not saved:
                raise HTTPException(422, "批次操作回执不存在；已成功的原记录不会重做")
            receipt = json.loads(saved[0])
            batch_id = receipt.get("batchId") or receipt.get("batch", {}).get("batchId")
            if not batch_id or batch_id != body.recordId:
                raise HTTPException(422, "批次回执与关联记录不符")
            row = db.execute(
                "SELECT payload,revision FROM batches WHERE tenant=? AND id=?", (tenant, batch_id)
            ).fetchone()
            batch = json.loads(row[0]) if row else {}
            if batch.get("areaCode") != order["siteId"]:
                raise HTTPException(422, "批次不属于工单站点")
            kind, fingerprint = "batch", digest(receipt)
            revision = receipt.get("revision", receipt.get("batch", {}).get("revision"))
            chain = receipt.get("chainStatus", receipt.get("batch", {}).get("chainStatus"))
            if receipt.get("status") in {
                "pending_review",
                "review_pending",
                "needs_review",
                "review",
            }:
                state = "review"
        else:
            raise HTTPException(422, "此记录类型尚不支持工单关联")
        return {
            "requestId": body.requestId,
            "recordId": body.recordId,
            "path": body.path,
            "kind": kind,
            "evidenceHash": fingerprint,
            "revision": revision,
            "state": state,
            "chainStatus": chain,
            "receivedAt": time.time(),
        }

    def records_current(db, order):
        for saved in order["records"]:
            current = record_ref(
                db,
                order,
                Record(
                    requestId=saved["requestId"],
                    expectedRevision=order["revision"],
                    path=saved["path"],
                    recordId=saved["recordId"],
                ),
            )
            if current["evidenceHash"] != saved["evidenceHash"]:
                raise HTTPException(409, "收件记录原件已变化，请先复核")

    def contents(order):
        return {
            key: order[key]
            for key in [
                "id",
                "tenant",
                "createdBy",
                "organization",
                "siteId",
                "deviceId",
                "startAt",
                "endAt",
                "sources",
                "records",
            ]
        }

    def mutate(order_id, body, claims, action, update, *, receiver=False):
        tenant = claims["tenant"]
        request_hash = digest(
            {
                "order": order_id,
                "action": action,
                "actor": claims["sub"],
                "organization": claims.get("organization"),
                "role": claims["role"],
                "body": body.model_dump(exclude={"expectedRevision"}),
            }
        )
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = read_order(db, tenant, order_id)
            accessible(db, order, claims)
            old = db.execute(
                "SELECT actor,hash,result FROM collection_requests WHERE tenant=? AND id=?",
                (tenant, body.requestId),
            ).fetchone()
            if old:
                if old[0] != claims["sub"] or old[1] != request_hash:
                    raise HTTPException(409, "请求编号已用于其他操作")
                return {"order": view(db, order), **json.loads(old[2]), "duplicate": True}
            if not receiver and (
                claims["role"] != "operator"
                or order["createdBy"] != claims["sub"]
                or order["organization"] != claims.get("organization")
            ):
                raise HTTPException(403, "只有工单创建人可修改")
            if order["revision"] != body.expectedRevision:
                raise HTTPException(409, "工单版本已变化，请刷新后重试")
            terminal = action == "close" or action.startswith(("accept:", "cancel:"))
            if db.execute(
                "SELECT count(*) FROM collection_requests WHERE tenant=? AND order_id=?",
                (tenant, order_id),
            ).fetchone()[0] >= MAX_REQUESTS + (2 if terminal else 0):
                raise HTTPException(409, "工单操作记录已满，请保留结果后新建工单")
            extra = update(db, order)
            previous = order["revision"]
            order["revision"] += 1
            order["updatedAt"] = time.time()
            payload = canonical(order)
            if len(payload) > 256 * 1024:
                raise HTTPException(413, "工单元数据已达容量上限")
            changed = db.execute(
                "UPDATE collection_orders SET payload=?,revision=? WHERE tenant=? AND id=? AND revision=?",
                (payload.decode(), order["revision"], tenant, order_id, previous),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "工单版本已变化，请刷新后重试")
            db.execute(
                "INSERT INTO collection_requests VALUES(?,?,?,?,?,?)",
                (
                    tenant,
                    body.requestId,
                    order_id,
                    claims["sub"],
                    request_hash,
                    canonical(extra).decode(),
                ),
            )
            return {"order": view(db, order), **extra, "duplicate": False}

    def editable(db, order):
        if order["state"] != "open":
            raise HTTPException(409, "工单已关闭或正在交接，不能继续录入")
        device_ok(db, order)
        sources_current(db, order)

    @app.get("/v1/collection-members")
    def recipients(claims=Depends(identity)):
        directory = members(claims["tenant"]) if claims["role"] in {"operator", "auditor"} else {}
        eligible = [value for key, value in sorted(directory.items()) if key != claims["sub"]]
        return {
            "available": external_identity is not None,
            "members": eligible[:100],
            "truncated": len(eligible) > 100,
        }

    @app.post("/v1/collection-orders", status_code=201)
    def create(body: NewOrder, claims=Depends(operator)):
        if body.startAt > body.endAt or body.endAt - body.startAt > 7 * 86400:
            raise HTTPException(422, "请选择有效工单时间，最多7天")
        fingerprint = digest(
            {
                "body": body.model_dump(),
                "actor": claims["sub"],
                "organization": claims.get("organization"),
            }
        )
        tenant = claims["tenant"]
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT creation_hash,payload FROM collection_orders WHERE tenant=? AND id=?",
                (tenant, body.id),
            ).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise HTTPException(409, "工单编号已被使用")
                return view(db, json.loads(old[1]))
            if (
                db.execute(
                    "SELECT count(*) FROM collection_orders WHERE tenant=?", (tenant,)
                ).fetchone()[0]
                >= 10000
            ):
                raise HTTPException(409, "工单容量已满，请先归档核对")
            order = {
                **body.model_dump(),
                "schema": "marine.collection-order/v1",
                "tenant": tenant,
                "createdBy": claims["sub"],
                "organization": claims.get("organization"),
                "createdAt": time.time(),
                "updatedAt": time.time(),
                "revision": 1,
                "state": "open",
                "sources": [],
                "records": [],
            }
            device_ok(db, order)
            db.execute(
                "INSERT INTO collection_orders VALUES(?,?,?,?,?,?)",
                (tenant, body.id, claims["sub"], fingerprint, canonical(order).decode(), 1),
            )
            return view(db, order)

    @app.get("/v1/collection-orders")
    def listing(after: str = Query(default="", max_length=128), claims=Depends(identity)):
        with store.connect() as db:
            rows = db.execute(
                "SELECT payload FROM collection_orders o WHERE tenant=? AND id>? AND (creator=? OR ?='auditor' OR EXISTS(SELECT 1 FROM collection_handoffs h WHERE h.tenant=o.tenant AND h.order_id=o.id AND h.recipient=?)) ORDER BY id LIMIT 26",
                (claims["tenant"], after, claims["sub"], claims["role"], claims["sub"]),
            ).fetchall()
            items = [
                {
                    key: order[key]
                    for key in [
                        "id",
                        "siteId",
                        "deviceId",
                        "startAt",
                        "endAt",
                        "createdBy",
                        "state",
                        "revision",
                    ]
                }
                for order in map(lambda row: json.loads(row[0]), rows[:25])
            ]
        return {
            "items": items,
            "hasMore": len(rows) > 25,
            "nextCursor": items[-1]["id"] if items else after,
        }

    @app.get("/v1/collection-orders/{order_id}")
    def get(order_id: str, claims=Depends(identity)):
        with store.connect() as db:
            order = read_order(db, claims["tenant"], order_id)
            accessible(db, order, claims)
            return view(db, order)

    @app.post("/v1/collection-orders/{order_id}/sources")
    def attach(order_id: str, body: Sources, claims=Depends(operator)):
        def update(db, order):
            editable(db, order)
            linked = {row["eventId"]: row for row in order["sources"]}
            for source in body.sources:
                linked[source.eventId] = source_ref(db, order, source.eventId, source.revision)
            if len(linked) > MAX_SOURCES:
                raise HTTPException(409, "一次工单最多关联100条来源")
            order["sources"] = list(linked.values())
            return {"linkedSources": len(linked)}

        return mutate(order_id, body, claims, "sources", update)

    @app.post("/v1/collection-orders/{order_id}/records")
    def link(order_id: str, body: Record, claims=Depends(operator)):
        def update(db, order):
            editable(db, order)
            if any(row["requestId"] == body.requestId for row in order["records"]):
                raise HTTPException(409, "记录已关联，请保留原收件回执")
            if len(order["records"]) >= MAX_RECORDS:
                raise HTTPException(409, "工单最多保留128条收件记录")
            receipt = record_ref(db, order, body)
            order["records"].append(receipt)
            return {"syncReceipt": receipt}

        return mutate(order_id, body, claims, "record", update)

    @app.get("/v1/collection-orders/{order_id}/record-receipts")
    def receipt_directory(order_id: str, claims=Depends(identity)):
        with store.connect() as db:
            order = read_order(db, claims["tenant"], order_id)
            accessible(db, order, claims)
            return {"schema": "marine.collection-records/v1", **{
                key: order[key] for key in ["id", "tenant", "siteId", "deviceId", "startAt",
                                          "endAt", "revision", "sources", "records"]}}

    @app.post("/v1/collection-orders/{order_id}/records/reuse")
    def reuse(order_id: str, body: ReuseRecord, claims=Depends(operator)):
        def update(db, order):
            editable(db, order)
            if body.sourceOrderId == order["id"]:
                raise HTTPException(422, "请选择另一份原工单；当前工单中的收件不重复关联")
            source = read_order(db, claims["tenant"], body.sourceOrderId)
            accessible(db, source, claims)
            original = next((row for row in source["records"] if row["requestId"] == body.sourceRequestId
                             and row["path"] == body.path and row["recordId"] == body.recordId), None)
            if original is None:
                raise HTTPException(422, "原工单没有这份收件；请重新读取原记录")
            if original["evidenceHash"] != body.evidenceHash:
                raise HTTPException(409, "原收件摘要已变化，请重新读取后核对")
            if any(row["requestId"] == body.sourceRequestId for row in order["records"]):
                raise HTTPException(409, "此收件已关联到当前工单，原记录不会重复执行")
            if len(order["records"]) >= MAX_RECORDS:
                raise HTTPException(409, "工单最多保留128条收件记录")
            if body.includeSources:
                linked = {row["eventId"]: row for row in order["sources"]}
                for saved in source["sources"]:
                    reference = source_ref(db, order, saved["eventId"], saved["revision"])
                    if reference["eventId"] in linked and linked[reference["eventId"]] != reference:
                        raise HTTPException(409, "目标工单的来源版本不同，请先核对")
                    linked[reference["eventId"]] = reference
                if len(linked) > MAX_SOURCES:
                    raise HTTPException(409, "一次工单最多关联100条来源")
                order["sources"] = list(linked.values())
            # Resolve the immutable business request, not the new association key.
            reference = record_ref(db, order, Record(requestId=body.sourceRequestId,
                expectedRevision=body.expectedRevision, path=body.path, recordId=body.recordId))
            if reference["evidenceHash"] != original["evidenceHash"] or reference["state"] != "received":
                raise HTTPException(409, "原业务收件已变化或仍待复核，不重复执行业务")
            if reference["kind"] == "environment" and body.recordId not in {row["eventId"] for row in order["sources"]}:
                raise HTTPException(422, "请先关联读数来源，或勾选同时关联原工单来源")
            reference.update(associationRequestId=body.requestId, reusedFromOrderId=source["id"],
                             reusedFromRevision=source["revision"])
            order["records"].append(reference)
            return {"syncReceipt": reference, "reused": True, "linkedSources": len(order["sources"])}

        return mutate(order_id, body, claims, "reuse-record", update)

    @app.post("/v1/collection-orders/{order_id}/close")
    def close(order_id: str, body: Change, claims=Depends(operator)):
        def update(db, order):
            editable(db, order)
            order["state"] = "closed"
            return {"closed": True}

        return mutate(order_id, body, claims, "close", update)

    @app.post("/v1/collection-orders/{order_id}/handoffs")
    def offer(order_id: str, body: Offer, claims=Depends(operator)):
        def update(db, order):
            if order["state"] not in {"open", "closed"} or not order["records"]:
                raise HTTPException(409, "请先同步工单记录；正在交接的工单不能重复发起")
            device_ok(db, order)
            sources_current(db, order)
            records_current(db, order)
            target = members(claims["tenant"]).get(body.recipient)
            if not target or body.recipient == claims["sub"]:
                raise HTTPException(422, "请选择本租户中另一名已授权成员；单人演示不可用于双人交接")
            if (
                db.execute(
                    "SELECT count(*) FROM collection_handoffs WHERE tenant=? AND order_id=?",
                    (claims["tenant"], order_id),
                ).fetchone()[0]
                >= MAX_HANDOFFS
            ):
                raise HTTPException(409, "工单交接记录已达上限")
            handoff_id = (
                "handoff_"
                + digest(
                    {"tenant": claims["tenant"], "order": order_id, "request": body.requestId}
                )[:32]
            )
            handoff = {
                "id": handoff_id,
                "orderId": order_id,
                "fromSubject": claims["sub"],
                "recipient": target,
                "state": "pending",
                "offeredAt": time.time(),
                "contentDigest": digest(contents(order)),
                "returnState": order["state"],
            }
            db.execute(
                "INSERT INTO collection_handoffs VALUES(?,?,?,?,?,?)",
                (
                    claims["tenant"],
                    handoff_id,
                    order_id,
                    body.recipient,
                    "pending",
                    canonical(handoff).decode(),
                ),
            )
            order["state"] = "handoff-pending"
            return {"handoffId": handoff_id}

        return mutate(order_id, body, claims, "offer", update)

    @app.post("/v1/collection-orders/{order_id}/handoffs/{handoff_id}/accept")
    def accept(order_id: str, handoff_id: str, body: Change, claims=Depends(identity)):
        if claims["role"] not in {"operator", "auditor"}:
            raise HTTPException(403, "当前身份不能接收工单")

        def update(db, order):
            row = db.execute(
                "SELECT payload FROM collection_handoffs WHERE tenant=? AND id=? AND order_id=?",
                (claims["tenant"], handoff_id, order_id),
            ).fetchone()
            handoff = json.loads(row[0]) if row else {}
            target = handoff.get("recipient", {})
            if (
                target.get("subject") != claims["sub"]
                or handoff.get("fromSubject") == claims["sub"]
            ):
                raise HTTPException(403, "只有指定接收人可以确认")
            if handoff.get("state") != "pending" or order["state"] != "handoff-pending":
                raise HTTPException(409, "交接状态已变化，请刷新")
            if members(claims["tenant"]).get(claims["sub"]) != target or any(
                claims.get(key) != target[key] for key in ["role", "organization"]
            ):
                raise HTTPException(403, "成员授权已变化，请重新确认交接")
            device_ok(db, order)
            sources_current(db, order)
            records_current(db, order)
            if digest(contents(order)) != handoff["contentDigest"]:
                raise HTTPException(409, "工单内容已变化，不能确认原交接")
            receipt = {
                "id": handoff_id,
                "orderId": order_id,
                "fromSubject": handoff["fromSubject"],
                "acceptedBy": claims["sub"],
                "acceptedAt": time.time(),
                "contentDigest": handoff["contentDigest"],
                "orderRevision": order["revision"] + 1,
            }
            handoff.update(state="accepted", receipt=receipt)
            db.execute(
                "UPDATE collection_handoffs SET state='accepted',payload=? WHERE tenant=? AND id=?",
                (canonical(handoff).decode(), claims["tenant"], handoff_id),
            )
            order["state"] = "handed-over"
            return {"handoffReceipt": receipt}

        return mutate(order_id, body, claims, "accept:" + handoff_id, update, receiver=True)

    @app.post("/v1/collection-orders/{order_id}/handoffs/{handoff_id}/cancel")
    def cancel(order_id: str, handoff_id: str, body: Change, claims=Depends(operator)):
        def update(db, order):
            row = db.execute(
                "SELECT payload FROM collection_handoffs WHERE tenant=? AND id=? AND order_id=?",
                (claims["tenant"], handoff_id, order_id),
            ).fetchone()
            handoff = json.loads(row[0]) if row else {}
            if handoff.get("state") != "pending" or order["state"] != "handoff-pending":
                raise HTTPException(409, "交接已变化，不能撤回")
            handoff.update(state="cancelled", cancelledAt=time.time())
            db.execute(
                "UPDATE collection_handoffs SET state='cancelled',payload=? WHERE tenant=? AND id=?",
                (canonical(handoff).decode(), claims["tenant"], handoff_id),
            )
            order["state"] = handoff["returnState"]
            return {"handoffCancelled": handoff_id}

        return mutate(order_id, body, claims, "cancel:" + handoff_id, update)

    from marine.collection_acceptance import register_acceptance

    register_acceptance(app, store, identity, operator, validator, read_order=read_order,
        accessible=accessible, device_ok=device_ok, source_ref=source_ref, record_ref=record_ref, contents=contents)
