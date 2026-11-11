"""Passive health evidence and tenant-scoped incident acknowledgement/recovery."""

import json
import os
import time
from contextlib import nullcontext
from typing import Literal
from pathlib import Path
from threading import BoundedSemaphore

from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from marine.storage import digest
from marine.config import Limits
from marine.operational_workload import workload_snapshot, unavailable_workload


class Acknowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=3, max_length=500)

    @field_validator("note")
    @classmethod
    def meaningful_note(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("Reconciliation note required")
        return value.strip()


class DeliveryResolution(Acknowledge):
    outcome: Literal["published", "not-published"]
    expectedAttempt: StrictInt = Field(ge=1, le=8)


class Operations:
    def __init__(self, store):
        self.store = store
        self.limits = Limits()
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS component_status(name TEXT PRIMARY KEY,ok INTEGER,checked REAL);
            CREATE TABLE IF NOT EXISTS incidents(tenant TEXT,id TEXT,code TEXT,status TEXT,first_seen REAL,last_seen REAL,note TEXT,actor TEXT,occurrences INTEGER,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS operator_actions(tenant TEXT,id TEXT,operation TEXT,actor TEXT,note TEXT,created REAL);
            """)

    def heartbeat(self, name, ok):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO component_status VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET ok=excluded.ok,checked=excluded.checked",
                (name, int(ok), time.time()),
            )

    def incident(self, tenant, code, active):
        identity = digest({"tenant": tenant, "code": code})[:32]
        now = time.time()
        with self.store.connect() as db:
            if active:
                db.execute(
                    "INSERT INTO incidents VALUES(?,?,?,'ACTIVE',?,?,'','',1) ON CONFLICT(tenant,id) DO UPDATE SET last_seen=excluded.last_seen,occurrences=incidents.occurrences+1,status=CASE WHEN incidents.status='RESOLVED' THEN 'ACTIVE' ELSE incidents.status END",
                    (tenant, identity, code, now, now),
                )
            else:
                db.execute(
                    "UPDATE incidents SET status='RESOLVED',last_seen=? WHERE tenant=? AND id=? AND status!='RESOLVED'",
                    (now, tenant, identity),
                )

    def snapshot(self, tenant=None, production=False):
        now = time.time()
        checks = {}
        try:
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO component_status VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET checked=excluded.checked",
                    ("database-probe", 1, now),
                )
                status = {r["name"]: dict(r) for r in db.execute("SELECT * FROM component_status")}
                where = " WHERE tenant=?" if tenant else ""
                args = (tenant,) if tenant else ()
                pending = db.execute(
                    "SELECT count(*) FROM outbox"
                    + where
                    + (" AND" if tenant else " WHERE")
                    + " status='pending'",
                    args,
                ).fetchone()[0]
                action_required = db.execute(
                    "SELECT count(*) FROM outbox"
                    + where
                    + (" AND" if tenant else " WHERE")
                    + " (status IN ('action_required','outcome_unknown') OR (status='dispatching' AND next<=?))",
                    (*args, now),
                ).fetchone()[0]
                ledger = db.execute(
                    "SELECT count(*) FROM ledger_jobs"
                    + where
                    + (" AND" if tenant else " WHERE")
                    + " status!='committed'",
                    args,
                ).fetchone()[0]
                last = db.execute(
                    "SELECT max(json_extract(payload,'$.windowEnd')) FROM windows" + where, args
                ).fetchone()[0]
            checks["database"] = True
        except Exception:
            return {"ready": False, "checks": {"database": False}, "generatedAt": now}
        for component in ["forecast", "maintenance"] + (
            ["transport"] if os.getenv("KAFKA_BOOTSTRAP") else []
        ):
            row = status.get(component)
            checks[component] = bool(
                row and row["ok"] and now - row["checked"] < self.limits.heartbeat_seconds
            )
        checks["outbox"] = pending < self.limits.backlog_warning and action_required == 0
        # Unknown commits remain pending; acknowledgements never turn this check green.
        ledger_status = status.get("ledger")
        checks["ledger"] = not ledger or bool(
            ledger_status
            and ledger_status["ok"]
            and now - ledger_status["checked"] < self.limits.heartbeat_seconds
        )
        backup_age = None
        receipt = os.getenv("MARINE_BACKUP_RECEIPT_FILE")
        if receipt:
            try:
                value = json.loads(Path(receipt).read_text())
                backup_age = now - float(value["verifiedAt"])
                checks["backup"] = (
                    value.get("verified") is True
                    and 0 <= backup_age < self.limits.backup_max_hours * 3600
                )
            except (OSError, ValueError, KeyError, TypeError):
                checks["backup"] = False
        elif production:
            checks["backup"] = False
        if "backup" in status and not status["backup"]["ok"]:
            checks["backup"] = False
        if tenant:
            for code, ok in checks.items():
                self.incident(tenant, code + "_unhealthy", not ok)
        return {
            "ready": all(checks.values()),
            "checks": checks,
            "pendingEvents": pending,
            "actionRequiredEvents": action_required,
            "unconfirmedLedgerCommands": ledger,
            "streamWindowAgeSeconds": None if last is None else max(0, now - last / 1000),
            "backupAgeSeconds": backup_age,
            "generatedAt": now,
        }

    def action(self, tenant, identity, operation, actor, note, connection=None):
        with self.store.connect() if connection is None else nullcontext(connection) as db:
            db.execute(
                "INSERT INTO operator_actions VALUES(?,?,?,?,?,?)",
                (tenant, identity, operation, actor, note, time.time()),
            )
            db.execute(
                "DELETE FROM operator_actions WHERE tenant=? AND rowid NOT IN (SELECT rowid FROM operator_actions WHERE tenant=? ORDER BY rowid DESC LIMIT 1000)",
                (tenant, tenant),
            )


def register_operations(app, store, identity, operator):
    ops = app.state.operations
    workload_slots = BoundedSemaphore(2)

    @app.get("/ready")
    def ready():
        result = ops.snapshot(production=app.state.mode == "production")
        return JSONResponse({"ready": result["ready"]}, status_code=200 if result["ready"] else 503)

    @app.get("/v1/operations")
    def operations(claims=Depends(identity)):
        state = ops.snapshot(claims["tenant"], app.state.mode == "production")
        monitor = app.state.monitor.report(claims["tenant"])
        ops.incident(claims["tenant"], "model_drift", monitor["driftStatus"] == "review_required")
        with store.connect() as db:
            incidents = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM incidents WHERE tenant=? ORDER BY last_seen DESC LIMIT 100",
                    (claims["tenant"],),
                )
            ]
            actions = [
                dict(r)
                for r in db.execute(
                    "SELECT id,operation,actor,note,created FROM operator_actions WHERE tenant=? ORDER BY rowid DESC LIMIT 100",
                    (claims["tenant"],),
                )
            ]
        if claims['role'] not in {'operator', 'auditor'}:
            workload = unavailable_workload(claims['tenant'], 'role_required', 'restricted')
        elif workload_slots.acquire(blocking=False):
            try:
                workload = workload_snapshot(store, claims['tenant'], claims['role'])
            finally:
                workload_slots.release()
        else:
            workload = unavailable_workload(claims['tenant'], 'inspection_busy')
        return {**state, "incidents": incidents, "recentActions": actions, "workload": workload}

    @app.post("/v1/incidents/{incident_id}/acknowledge")
    def acknowledge(incident_id: str, body: Acknowledge, claims=Depends(operator)):
        with store.connect() as db:
            row = db.execute(
                "SELECT status FROM incidents WHERE tenant=? AND id=?",
                (claims["tenant"], incident_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Incident not found")
            if row[0] == "RESOLVED":
                raise HTTPException(409, "Incident already resolved")
            db.execute(
                "UPDATE incidents SET status='ACKNOWLEDGED',note=?,actor=? WHERE tenant=? AND id=?",
                (body.note, claims["sub"], claims["tenant"], incident_id),
            )
        ops.action(claims["tenant"], incident_id, "acknowledge", claims["sub"], body.note)
        return {"id": incident_id, "status": "ACKNOWLEDGED"}

    @app.get("/v1/outbox")
    def outbox(after: int = Query(default=0, ge=0, le=2**63 - 1), claims=Depends(identity)):
        with store.connect() as db:
            rows = db.execute(
                "SELECT rowid AS cursor,id,topic,attempt,status,error_code AS failureCode FROM outbox WHERE tenant=? AND (status IN ('action_required','outcome_unknown') OR (status='dispatching' AND next<=?)) AND rowid>? ORDER BY rowid LIMIT 101",
                (claims["tenant"], time.time(), after),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows[:100]],
            "hasMore": len(rows) > 100,
            "nextCursor": rows[min(len(rows), 100) - 1]["cursor"] if rows else after,
        }

    @app.post("/v1/outbox/{event_id}/reconcile")
    def reconcile_event(event_id: str, body: DeliveryResolution, claims=Depends(operator)):
        with store.connect() as db:
            changed = db.execute(
                "UPDATE outbox SET status=?,error_code=?,next=0 WHERE tenant=? AND id=? AND attempt=? AND (status='outcome_unknown' OR (status='dispatching' AND next<=?))",
                (
                    "published" if body.outcome == "published" else "action_required",
                    None if body.outcome == "published" else "DELIVERY_NOT_PUBLISHED_CONFIRMED",
                    claims["tenant"],
                    event_id,
                    body.expectedAttempt,
                    time.time(),
                ),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "Delivery is not awaiting this reconciliation")
            ops.action(
                claims["tenant"],
                event_id,
                "reconcile-original-delivery",
                claims["sub"],
                body.outcome + ": " + body.note,
                connection=db,
            )
        return {
            "id": event_id,
            "status": "published" if body.outcome == "published" else "action_required",
            "requestPreserved": True,
        }

    @app.post("/v1/outbox/{event_id}/retry")
    def retry_event(event_id: str, body: Acknowledge, claims=Depends(operator)):
        with store.connect() as db:
            prior = db.execute(
                "SELECT attempt FROM outbox WHERE tenant=? AND id=? AND status='action_required'",
                (claims["tenant"], event_id),
            ).fetchone()
            if prior is None:
                raise HTTPException(409, "Original event is not awaiting operator review")
            changed = db.execute(
                "UPDATE outbox SET status='pending',attempt=0,next=0,error_code=NULL WHERE tenant=? AND id=? AND status='action_required'",
                (claims["tenant"], event_id),
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "Original event is not awaiting operator review")
            ops.action(
                claims["tenant"],
                event_id,
                "retry-original-event",
                claims["sub"],
                f"Previous automatic attempts: {prior[0]}. {body.note}",
                connection=db,
            )
        return {"id": event_id, "status": "pending", "requestPreserved": True}

    @app.post("/v1/ledger/jobs/{job_id}/retry")
    def retry(job_id: str, body: Acknowledge, claims=Depends(operator)):
        with store.connect() as db:
            row = db.execute(
                "SELECT 1 FROM ledger_jobs WHERE tenant=? AND id=?", (claims["tenant"], job_id)
            ).fetchone()
        if not row:
            raise HTTPException(404, "Command not found")
        ops.action(claims["tenant"], job_id, "reconcile-original-request", claims["sub"], body.note)
        result = app.state.ledger.sync(claims["tenant"])
        ops.heartbeat("ledger", result["status"] == "checked")
        return {**result, "requestPreserved": True}


def maintenance(app):
    """One bounded maintenance pass; a broken tenant cannot stop subsequent passes."""
    ops = app.state.operations
    healthy = True
    try:
        app.state.forecasts.tick()
        ops.heartbeat("forecast", True)
    except Exception:
        healthy = False
        ops.heartbeat("forecast", False)
    try:
        if os.getenv("MARINE_FABRIC_ROOT"):
            with app.state.store.connect() as db:
                tenants = [
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT tenant FROM ledger_jobs WHERE status!='committed' LIMIT 100"
                    )
                ]
            ledger_ok = True
            busy = False
            for tenant in tenants:
                try:
                    result = app.state.ledger.sync(tenant)
                    busy = busy or result["status"] == "busy"
                    ledger_ok = ledger_ok and result["status"] in {"checked", "busy"}
                except Exception:
                    ledger_ok = False
            if not ledger_ok:
                ops.heartbeat("ledger", False)
            elif not busy:
                ops.heartbeat("ledger", True)
        with app.state.store.connect() as db:
            tenants = [
                r[0]
                for r in db.execute(
                    "SELECT tenant FROM events UNION SELECT tenant FROM edge_review_links LIMIT 1000"
                )
            ]
        for tenant in tenants:
            try:
                from marine.edge_samples import release_reviews

                release_reviews(app.state.store, tenant)
                monitor = app.state.monitor.report(tenant)
                ops.incident(tenant, "model_drift", monitor["driftStatus"] == "review_required")
                ops.snapshot(tenant, app.state.mode == "production")
                ops.incident(tenant, "maintenance_failed", False)
            except Exception:
                healthy = False
                ops.incident(tenant, "maintenance_failed", True)
    except Exception:
        healthy = False
    ops.heartbeat("maintenance", healthy)
    app.state.scheduler_error = not healthy
