"""Bounded, version-bound collection assessment and independent review."""

import hashlib
import json
import time
from collections import Counter
from typing import Annotated, Literal

from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field, StrictInt, field_validator

from marine.collection_orders import Change, Record, MAX_RECORDS, MAX_SOURCES
from marine.data_quality import inspect_event
from marine.storage import QUALITY_RECORD_BYTES, canonical, digest

Policy = Literal["local", "pinned", "committed"]
MAX_ASSESSMENTS = 8
MAX_FINDINGS = 32
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 128 * 1024
MAX_BATCHES = 32
MAX_EVIDENCE = 16


class AssessmentRequest(Change):
    expectedRevision: Annotated[StrictInt, Field(ge=1)]
    evidencePolicy: Policy = "local"


class DecisionRequest(AssessmentRequest):
    expectedAssessmentVersion: Annotated[StrictInt, Field(ge=1)]
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Reason required")
        return value.strip()


def register_acceptance(
    app,
    store,
    identity,
    operator,
    validator,
    *,
    read_order,
    accessible,
    device_ok,
    source_ref,
    record_ref,
    contents,
):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS collection_assessments(tenant TEXT,id TEXT,order_id TEXT,request_id TEXT,actor TEXT,request_hash TEXT,payload TEXT,payload_hash TEXT,version INTEGER,created REAL,PRIMARY KEY(tenant,id),UNIQUE(tenant,request_id));
        CREATE TABLE IF NOT EXISTS collection_acceptance_decisions(tenant TEXT,id TEXT,assessment_id TEXT,order_id TEXT,request_id TEXT,actor TEXT,request_hash TEXT,payload TEXT,payload_hash TEXT,PRIMARY KEY(tenant,id),UNIQUE(tenant,assessment_id),UNIQUE(tenant,request_id));
        CREATE INDEX IF NOT EXISTS collection_assessment_order ON collection_assessments(tenant,order_id,created);
        """)

    def inspect(db, order, policy):
        if len(order["sources"]) > MAX_SOURCES or len(order["records"]) > MAX_RECORDS:
            raise HTTPException(409, "工单引用超过验收容量")
        counts = Counter()
        findings = []
        used = 0
        partial = False
        cache = {}
        pins = []

        def issue(code, reference="order"):
            counts[code] += 1
            if len(findings) < MAX_FINDINGS:
                findings.append({"reference": reference, "code": code})

        def charge(amount, reference):
            nonlocal used, partial
            if amount > 256 * 1024 or used + amount > MAX_INPUT_BYTES:
                partial = True
                issue("capacityExceeded", reference)
                return False
            used += amount
            return True

        def payload(table, identifier, maximum=256 * 1024):
            nonlocal partial
            # Table names are a fixed internal allowlist; values are parameters.
            if table not in {
                "events",
                "receipts",
                "labels",
                "batches",
                "devices",
                "registered_sites",
            }:
                raise ValueError("unsupported acceptance dataset")
            key = (table, identifier)
            if key in cache:
                return cache[key]
            extra = (
                ",hash"
                if table in {"events", "receipts"}
                else ",revision"
                if table == "batches"
                else ""
            )
            row = db.execute(
                f"SELECT length(CAST(payload AS BLOB)) AS bytes{extra} FROM {table} WHERE tenant=? AND id=?",
                (order["tenant"], identifier),
            ).fetchone()
            if row is None:
                pins.append([table, identifier, None])
                cache[key] = None
                return None
            if row["bytes"] is None:
                issue("documentInvalid", identifier)
                pins.append([table, identifier, "null-payload"])
                cache[key] = None
                return None
            if row["bytes"] > maximum or not charge(row["bytes"], identifier):
                partial = True
                issue("documentTooLarge", identifier)
                pins.append([table, identifier, row["bytes"], "over-budget"])
                cache[key] = None
                return None
            raw = db.execute(
                f"SELECT payload FROM {table} WHERE tenant=? AND id=?",
                (order["tenant"], identifier),
            ).fetchone()[0]
            if not isinstance(raw, str):
                issue("documentInvalid", identifier)
                pins.append([table, identifier, "non-text-payload"])
                cache[key] = None
                return None
            pins.append([table, identifier, hashlib.sha256(raw.encode()).hexdigest(), dict(row)])
            try:
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
            except (ValueError, TypeError, RecursionError):
                issue("documentInvalid", identifier)
                result = None
            cache[key] = result
            return result

        if order["state"] not in {"closed", "handed-over"}:
            issue("collectionNotEnded")
        if not order["sources"]:
            issue("sourcesMissing")
        if not order["records"]:
            issue("receiptsMissing")
        payload("registered_sites", order["siteId"])
        payload("devices", order["deviceId"])
        try:
            device_ok(db, order)
        except (HTTPException, ValueError, TypeError, KeyError, AttributeError):
            issue("deviceInvalid")
        for source in order["sources"]:
            event = payload("events", source["eventId"], QUALITY_RECORD_BYTES)
            if event is None:
                issue("sourceUnavailable", source["eventId"])
                continue
            row = db.execute(
                "SELECT payload,hash FROM events WHERE tenant=? AND id=?",
                (order["tenant"], source["eventId"]),
            ).fetchone()
            _, problems = inspect_event(
                validator, order["tenant"], source["eventId"], row["payload"], row["hash"]
            )
            for code in problems:
                issue(code, source["eventId"])
            try:
                source_ref(db, order, source["eventId"], source["revision"])
            except (HTTPException, ValueError, TypeError, KeyError, OverflowError):
                issue("sourceBindingChanged", source["eventId"])

        batches = {}
        source_ids = {source["eventId"] for source in order["sources"]}
        for saved in order["records"]:
            path = saved["path"]
            identifier = saved["recordId"]
            admitted = True
            if path == "/v1/environment-readings":
                admitted = payload("events", identifier, QUALITY_RECORD_BYTES) is not None
                if identifier not in source_ids:
                    issue("recordSourceUnlinked", identifier)
            elif path == "/v1/catches":
                admitted = payload("labels", saved["requestId"]) is not None
            elif path == "/v1/batches":
                receipt = payload("receipts", saved["requestId"])
                batch = payload("batches", identifier)
                admitted = receipt is not None and batch is not None
                if batch is not None:
                    batches[identifier] = batch
            else:
                issue("recordTypeUnsupported", identifier)
                continue
            if not admitted:
                issue("receiptUnavailable", identifier)
                continue
            try:
                current = record_ref(
                    db,
                    order,
                    Record(
                        requestId=saved["requestId"],
                        expectedRevision=order["revision"],
                        path=path,
                        recordId=identifier,
                    ),
                )
                if current["evidenceHash"] != saved["evidenceHash"]:
                    issue("receiptBindingChanged", identifier)
                if current["state"] != "received":
                    issue("recordNeedsReview", identifier)
            except (HTTPException, ValueError, TypeError, KeyError, OverflowError):
                issue("receiptBindingChanged", identifier)
        if len(batches) > MAX_BATCHES:
            partial = True
            issue("capacityExceeded")
        if policy != "local" and not batches:
            issue("batchEvidenceRequired")
        evidence_pins = []
        ledger_pins = []
        for batch_id, batch in list(batches.items())[:MAX_BATCHES]:
            if batch.get("state") == "RECALLED":
                issue("batchRecalled", batch_id)
            open_issues = db.execute(
                "SELECT count(*) FROM cold_issues WHERE tenant=? AND batch=? AND status='OPEN'",
                (order["tenant"], batch_id),
            ).fetchone()[0]
            pins.append(["coldOpenIssues", batch_id, open_issues])
            if open_issues:
                issue("coldIssueOpen", batch_id)
            if policy == "local":
                continue
            eligible = 0
            rows = db.execute(
                "SELECT id,status,length(CAST(manifest AS BLOB)) AS bytes,coalesce(length(CAST(receipt AS BLOB)),0) AS receipt_bytes FROM evidence WHERE tenant=? AND batch=? ORDER BY id LIMIT ?",
                (order["tenant"], batch_id, MAX_EVIDENCE + 1),
            ).fetchall()
            if len(rows) > MAX_EVIDENCE:
                partial = True
                issue("capacityExceeded", batch_id)
            for row in rows[:MAX_EVIDENCE]:
                if not charge(row["bytes"] + row["receipt_bytes"], row["id"]):
                    continue
                raw = db.execute(
                    "SELECT manifest,receipt FROM evidence WHERE tenant=? AND id=? AND batch=?",
                    (order["tenant"], row["id"], batch_id),
                ).fetchone()
                try:
                    manifest = json.loads(raw["manifest"])
                    receipt = json.loads(raw["receipt"]) if raw["receipt"] else None
                    manifest_hash = digest(manifest)
                    receipt_hash = digest(receipt)
                    bound = (
                        manifest.get("tenant") == order["tenant"]
                        and manifest.get("batchId") == batch_id
                        and manifest.get("evidenceId") == row["id"]
                    )
                    fixed = (
                        bound
                        and isinstance(manifest.get("cipherCid"), str)
                        and bool(manifest["cipherCid"])
                        and row["status"] in {"PIN_VERIFIED", "SUBMITTED", "COMMITTED"}
                    )
                    accepted = (
                        fixed
                        if policy == "pinned"
                        else fixed
                        and row["status"] == "COMMITTED"
                        and isinstance(receipt, dict)
                        and receipt.get("validationCode") == "VALID"
                    )
                    eligible += bool(accepted)
                    evidence_pins.append(
                        {
                            "id": row["id"],
                            "batchId": batch_id,
                            "state": row["status"],
                            "manifestHash": manifest_hash,
                            "receiptHash": receipt_hash,
                        }
                    )
                except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
                    issue("evidenceInvalid", row["id"])
            if not eligible:
                issue("evidenceLevelMissing", batch_id)
            if policy == "committed":
                jobs = 0
                pending = 0
                for row in db.execute(
                    "SELECT id,status,coalesce(length(CAST(receipt AS BLOB)),0) AS bytes FROM ledger_jobs WHERE tenant=? AND batch=? ORDER BY id LIMIT 1001",
                    (order["tenant"], batch_id),
                ):
                    jobs += 1
                    if jobs > 1000 or not charge(row["bytes"], row["id"]):
                        partial = True
                        issue("capacityExceeded", batch_id)
                        break
                    raw = db.execute(
                        "SELECT receipt FROM ledger_jobs WHERE tenant=? AND id=?",
                        (order["tenant"], row["id"]),
                    ).fetchone()[0]
                    receipt_hash = hashlib.sha256((raw or "").encode()).hexdigest()
                    ledger_pins.append(
                        {
                            "id": row["id"],
                            "batchId": batch_id,
                            "status": row["status"],
                            "receiptHash": receipt_hash,
                        }
                    )
                    if row["status"] != "committed" or not raw:
                        pending += 1
                if not jobs or pending:
                    issue("ledgerUnconfirmed", batch_id)
        binding = {
            "orderHash": digest(
                {"revision": order["revision"], "state": order["state"], **contents(order)}
            ),
            "policy": policy,
            "metadata": pins,
            "evidence": evidence_pins,
            "ledger": ledger_pins,
        }
        result = {
            "schema": "marine.collection-assessment/v1",
            "orderId": order["id"],
            "orderRevision": order["revision"],
            "evidencePolicy": policy,
            "bindingSha256": digest(binding),
            "outcome": "blocked" if counts or partial else "ready",
            "issues": dict(counts),
            "findings": findings,
            "findingsTruncated": sum(counts.values()) > len(findings),
            "coverage": {
                "sources": len(order["sources"]),
                "records": len(order["records"]),
                "batches": len(batches),
                "selectedPayloadBytes": used,
                "complete": not partial,
                "orderReferencesOnly": True,
                "physicalImagesChecked": False,
                "remoteAvailabilityChecked": False,
            },
            "limits": {
                "sources": MAX_SOURCES,
                "records": MAX_RECORDS,
                "batches": MAX_BATCHES,
                "evidencePerBatch": MAX_EVIDENCE,
                "inputBytes": MAX_INPUT_BYTES,
                "findings": MAX_FINDINGS,
                "assessmentsPerOrder": MAX_ASSESSMENTS,
            },
        }
        if len(canonical(result)) > MAX_METADATA_BYTES:
            raise HTTPException(413, "验收元数据超过容量")
        return result

    def checked(row):
        if row is None:
            raise HTTPException(404, "验收记录不存在或不可访问")
        if len(row["payload"].encode()) > MAX_METADATA_BYTES:
            raise HTTPException(409, "验收记录超过容量")
        data = json.loads(row["payload"])
        if digest(data) != row["payload_hash"]:
            raise HTTPException(409, "验收记录完整性不符")
        return data

    def assessment(db, order, assessment_id):
        return db.execute(
            "SELECT * FROM collection_assessments WHERE tenant=? AND order_id=? AND id=?",
            (order["tenant"], order["id"], assessment_id),
        ).fetchone()

    def envelope(db, order, row):
        saved = checked(row)
        current = inspect(db, order, saved["evidencePolicy"])
        decision_row = db.execute(
            "SELECT * FROM collection_acceptance_decisions WHERE tenant=? AND assessment_id=?",
            (order["tenant"], saved["id"]),
        ).fetchone()
        return {
            "assessment": saved,
            "version": row["version"],
            "decision": checked(decision_row) if decision_row else None,
            "current": {
                "stale": current["bindingSha256"] != saved["bindingSha256"],
                "bindingSha256": current["bindingSha256"],
                "outcome": current["outcome"],
                "checkedAt": time.time(),
            },
        }

    def opened(db, claims, order_id):
        order = read_order(db, claims["tenant"], order_id)
        accessible(db, order, claims)
        return order

    @app.get("/v1/collection-orders/{order_id}/acceptance-preview")
    def preview(order_id: str, evidencePolicy: Policy = "local", claims=Depends(identity)):
        with store.connect() as db:
            db.execute("BEGIN")
            order = opened(db, claims, order_id)
            return JSONResponse(
                inspect(db, order, evidencePolicy), headers={"Cache-Control": "no-store"}
            )

    @app.get("/v1/collection-orders/{order_id}/acceptance-assessments")
    def listing(order_id: str, claims=Depends(identity)):
        with store.connect() as db:
            db.execute("BEGIN")
            order = opened(db, claims, order_id)
            rows = db.execute(
                "SELECT * FROM collection_assessments WHERE tenant=? AND order_id=? ORDER BY created DESC,id DESC LIMIT 8",
                (claims["tenant"], order_id),
            ).fetchall()
            items = []
            for row in rows:
                saved = checked(row)
                decision = db.execute(
                    "SELECT payload,payload_hash FROM collection_acceptance_decisions WHERE tenant=? AND assessment_id=?",
                    (claims["tenant"], row["id"]),
                ).fetchone()
                items.append(
                    {
                        key: saved[key]
                        for key in (
                            "id",
                            "orderRevision",
                            "createdBy",
                            "createdAt",
                            "outcome",
                            "evidencePolicy",
                        )
                    }
                )
                items[-1].update(
                    version=row["version"],
                    decision=checked(decision)["decision"] if decision else None,
                )
            return {"items": items, "limit": MAX_ASSESSMENTS, "orderRevision": order["revision"]}

    @app.get("/v1/collection-orders/{order_id}/acceptance-assessments/{assessment_id}")
    def read(order_id: str, assessment_id: str, claims=Depends(identity)):
        with store.connect() as db:
            db.execute("BEGIN")
            order = opened(db, claims, order_id)
            return JSONResponse(
                envelope(db, order, assessment(db, order, assessment_id)),
                headers={"Cache-Control": "no-store"},
            )

    @app.post("/v1/collection-orders/{order_id}/acceptance-assessments", status_code=201)
    def submit(order_id: str, body: AssessmentRequest, claims=Depends(operator)):
        request_hash = digest(
            {
                "actor": claims["sub"],
                "organization": claims.get("organization"),
                "order": order_id,
                "body": body.model_dump(),
            }
        )
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = opened(db, claims, order_id)
            if order["createdBy"] != claims["sub"] or order["organization"] != claims.get(
                "organization"
            ):
                raise HTTPException(403, "只有工单创建人可以提交验收")
            old = db.execute(
                "SELECT * FROM collection_assessments WHERE tenant=? AND request_id=?",
                (claims["tenant"], body.requestId),
            ).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise HTTPException(409, "请求编号已用于其他验收")
                return envelope(db, order, old)
            if order["revision"] != body.expectedRevision:
                raise HTTPException(409, "工单版本已变化，请重新检查")
            if (
                db.execute(
                    "SELECT count(*) FROM collection_assessments WHERE tenant=? AND order_id=?",
                    (claims["tenant"], order_id),
                ).fetchone()[0]
                >= MAX_ASSESSMENTS
            ):
                raise HTTPException(409, "工单验收历史已达8份上限；原记录保留")
            saved = inspect(db, order, body.evidencePolicy)
            saved.update(
                id="assessment_"
                + digest({"tenant": claims["tenant"], "request": body.requestId})[:32],
                createdBy=claims["sub"],
                organization=claims.get("organization"),
                createdAt=time.time(),
            )
            raw = canonical(saved).decode()
            count, total = db.execute(
                "SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) FROM collection_assessments WHERE tenant=?",
                (claims["tenant"],),
            ).fetchone()
            if count >= 10000 or total + len(raw.encode()) > 64 * 1024 * 1024:
                raise HTTPException(409, "租户验收证据容量已满；不会自动删除历史")
            db.execute(
                "INSERT INTO collection_assessments VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    claims["tenant"],
                    saved["id"],
                    order_id,
                    body.requestId,
                    claims["sub"],
                    request_hash,
                    raw,
                    digest(saved),
                    1,
                    saved["createdAt"],
                ),
            )
            return envelope(db, order, assessment(db, order, saved["id"]))

    @app.post("/v1/collection-orders/{order_id}/acceptance-assessments/{assessment_id}/decisions")
    def decide(order_id: str, assessment_id: str, body: DecisionRequest, claims=Depends(identity)):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            order = opened(db, claims, order_id)
            row = assessment(db, order, assessment_id)
            saved = checked(row)
            if claims["role"] != "auditor" or claims["sub"] in {
                saved["createdBy"],
                order["createdBy"],
            }:
                raise HTTPException(403, "需要另一名已授权审计员独立验收")
            request_hash = digest(
                {
                    "actor": claims["sub"],
                    "organization": claims.get("organization"),
                    "assessment": assessment_id,
                    "body": body.model_dump(),
                }
            )
            old = db.execute(
                "SELECT * FROM collection_acceptance_decisions WHERE tenant=? AND request_id=?",
                (claims["tenant"], body.requestId),
            ).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise HTTPException(409, "请求编号已用于其他决定")
                return envelope(db, order, row)
            if (
                row["version"] != body.expectedAssessmentVersion
                or order["revision"] != body.expectedRevision
                or body.evidencePolicy != saved["evidencePolicy"]
            ):
                raise HTTPException(409, "工单、验收记录版本或证据等级已变化")
            current = inspect(db, order, saved["evidencePolicy"])
            if current["bindingSha256"] != saved["bindingSha256"]:
                raise HTTPException(409, "来源、回执或当前状态已变化，请重新提交验收")
            if body.decision == "approve" and current["outcome"] != "ready":
                raise HTTPException(409, "仍有未解决项，不能确认验收通过")
            if db.execute(
                "SELECT 1 FROM collection_acceptance_decisions WHERE tenant=? AND assessment_id=?",
                (claims["tenant"], assessment_id),
            ).fetchone():
                raise HTTPException(409, "该验收已有独立决定，原件不覆盖")
            decision = {
                "schema": "marine.collection-decision/v1",
                "id": "decision_"
                + digest({"tenant": claims["tenant"], "request": body.requestId})[:32],
                "assessmentId": assessment_id,
                "orderId": order_id,
                "orderRevision": order["revision"],
                "assessmentVersion": row["version"],
                "decision": body.decision,
                "reason": body.reason,
                "reviewedBy": claims["sub"],
                "organization": claims.get("organization"),
                "reviewedAt": time.time(),
                "bindingSha256": saved["bindingSha256"],
            }
            db.execute(
                "INSERT INTO collection_acceptance_decisions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    claims["tenant"],
                    decision["id"],
                    assessment_id,
                    order_id,
                    body.requestId,
                    claims["sub"],
                    request_hash,
                    canonical(decision).decode(),
                    digest(decision),
                ),
            )
            db.execute(
                "UPDATE collection_assessments SET version=version+1 WHERE tenant=? AND id=? AND version=?",
                (claims["tenant"], assessment_id, row["version"]),
            )
            return envelope(db, order, assessment(db, order, assessment_id))

    @app.get("/v1/collection-orders/{order_id}/acceptance-assessments/{assessment_id}/certificate")
    def certificate(order_id: str, assessment_id: str, claims=Depends(identity)):
        with store.connect() as db:
            db.execute("BEGIN")
            order = opened(db, claims, order_id)
            result = envelope(db, order, assessment(db, order, assessment_id))
            if not result["decision"] or result["decision"]["decision"] != "approve":
                raise HTTPException(409, "尚无独立验收通过的凭证")
            frozen = {"assessment": result["assessment"], "decision": result["decision"]}
            return JSONResponse(
                {
                    "schema": "marine.collection-certificate/v1",
                    "certificate": frozen,
                    "sha256": digest(frozen),
                    "current": result["current"],
                    "limitations": [
                        "记录的是验收时的本机来源与已有回执。",
                        "未实时查询远端可用性；哈希不认证导出文件作者。",
                        "验收通过不等于结束物流交付或现场效果验收。",
                    ],
                },
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": f'attachment; filename="marine-acceptance-{result["assessment"]["id"]}.json"',
                },
            )
