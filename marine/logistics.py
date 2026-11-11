"""Durable consortium commands, ownership, cold-chain records and ledger replay."""

import fcntl
import json
import time

from fastapi import Depends, HTTPException
from pydantic import Field, field_validator

from marine.storage import canonical, digest
from marine.commands import Command


def chain_id(tenant, batch):
    return "b_" + digest({"tenant": tenant, "batchId": batch})[:48]


def organization(claims):
    return claims.get("organization", "Org1MSP")


def enqueue(db, tenant, request, batch, operation, payload, actor):
    identity = "job_" + digest({"tenant": tenant, "request": request})[:48]
    db.execute(
        "INSERT INTO ledger_jobs(tenant,id,batch,operation,payload,actor,status) VALUES(?,?,?,?,?,?,?)",
        (tenant, identity, batch, operation, canonical(payload).decode(), actor, "pending"),
    )


class LedgerSync:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS ledger_jobs(tenant TEXT,id TEXT,batch TEXT,operation TEXT,payload TEXT,actor TEXT,status TEXT,args TEXT,receipt TEXT,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS transfers(tenant TEXT,id TEXT,batch TEXT,source TEXT,target TEXT,used INTEGER,PRIMARY KEY(tenant,id));
            CREATE INDEX IF NOT EXISTS ledger_pending ON ledger_jobs(tenant,status);
            """)

    def sync(self, tenant, factory=None):
        from marine.fabric import Fabric

        factory = factory or Fabric
        # One writer per local database; file lock releases on process death. Remote calls never hold SQL transactions.
        with open(self.store.path + ".ledger.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy", "committed": 0}
            with self.store.connect() as db:
                jobs = db.execute(
                    "SELECT * FROM ledger_jobs WHERE tenant=? AND status!='committed' ORDER BY rowid LIMIT 100",
                    (tenant,),
                ).fetchall()
            completed = 0
            blocked = set()
            failed = []
            for job in jobs:
                payload = json.loads(job["payload"])
                dependencies = {
                    job["batch"],
                    *payload.get("parents", {}),
                    *[o["batchId"] for o in payload.get("outputs", [])],
                }
                if dependencies & blocked:
                    blocked.update(dependencies)
                    continue
                try:
                    fabric = factory(job["actor"])
                    receipt = fabric.query("GetReceipt", job["id"])
                    if not receipt:
                        payload = json.loads(job["payload"])
                        args = (
                            json.loads(job["args"])
                            if job["args"]
                            else self.arguments(fabric, job, payload)
                        )
                        # Store the exact revision before submit. Unknown outcomes retry the SAME command.
                        with self.store.connect() as db:
                            db.execute(
                                "UPDATE ledger_jobs SET status='submitted',args=? WHERE tenant=? AND id=?",
                                (canonical(args).decode(), tenant, job["id"]),
                            )
                        fabric.invoke(*args)
                        receipt = fabric.query("GetReceipt", job["id"])
                    if not receipt:
                        raise RuntimeError("Unconfirmed commit")
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE ledger_jobs SET status='committed',receipt=? WHERE tenant=? AND id=?",
                            (
                                canonical({**receipt, "channelId": fabric.channel}).decode(),
                                tenant,
                                job["id"],
                            ),
                        )
                    completed += 1
                except (RuntimeError, OSError, KeyError, ValueError):
                    # Preserve each batch dependency chain while independent batches can progress.
                    blocked.update(dependencies)
                    failed.append(job["id"])
            return {
                "status": "unconfirmed" if failed else "checked",
                "committed": completed,
                "blockedJobs": failed,
            }

    def arguments(self, fabric, job, payload):
        identity = chain_id(job["tenant"], job["batch"])
        request = job["id"]
        operation = job["operation"]
        if operation == "create":
            existing = fabric.query("GetBatch", identity)
            if existing:
                raise RuntimeError("Existing chain batch requires explicit migration")
            return [
                "CreateBatch",
                request,
                identity,
                payload["speciesId"],
                payload["areaCode"],
                payload["massGrams"],
            ]
        current = fabric.query("GetBatch", identity)
        if not current:
            # Upgrade path for locally created pre-outbox batches: create only if truly absent.
            batch = self.store.batch(job["tenant"], job["batch"])
            fabric.invoke(
                "CreateBatch",
                "create_" + identity,
                identity,
                batch["speciesId"],
                batch["areaCode"],
                batch["massGrams"],
            )
            current = fabric.query("GetBatch", identity)
        if operation == "transition":
            return ["Transition", request, identity, payload["nextState"], current["revision"]]
        if operation == "propose":
            return ["ProposeTransfer", request, identity, payload["target"], current["revision"]]
        if operation == "accept":
            return ["AcceptTransfer", request, payload["chainProposal"], current["revision"]]
        if operation == "transform":
            parents = [chain_id(job["tenant"], i) for i in payload["parents"]]
            outputs = [
                {"id": chain_id(job["tenant"], o["batchId"]), "massGrams": o["massGrams"]}
                for o in payload["outputs"]
            ]
            return [
                "TransformBatch",
                request,
                json.dumps(parents),
                json.dumps(outputs),
                payload["lossGrams"],
            ]
        raise ValueError("Unknown ledger operation")


class Transfer(Command):
    expectedRevision: int = Field(ge=1)
    target: str = Field(pattern=r"^(Org1MSP|Org2MSP)$")


class Accept(Command):
    expectedRevision: int = Field(ge=1)


class OutputBatch(Command):
    batchId: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    massGrams: int = Field(gt=0, le=10**12)


class Transform(Command):
    parents: dict[str, int] = Field(min_length=1, max_length=100)
    outputs: list[OutputBatch] = Field(min_length=1, max_length=100)
    lossGrams: int = Field(ge=0, le=10**14)


class ResolveCold(Command):
    disposition: str = Field(pattern=r"^(release|recall)$")
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        value = value.strip()
        if len(value) < 3:
            raise ValueError("请填写实际处置依据")
        return value


class ColdRecord(Command):
    startAt: float
    endAt: float
    minimumC: float = Field(ge=-100, le=100, allow_inf_nan=False)
    maximumC: float = Field(ge=-100, le=100, allow_inf_nan=False)
    allowedMinimumC: float = Field(ge=-100, le=100, allow_inf_nan=False)
    allowedMaximumC: float = Field(ge=-100, le=100, allow_inf_nan=False)
    deviceId: str = Field(min_length=1, max_length=128)
    sourceEventIds: list[str] = Field(default_factory=list, max_length=100)
    forecastId: str | None = Field(default=None, max_length=128)


def register_logistics(app, store, identity, operator, execute):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS cold_issues(tenant TEXT,id TEXT,batch TEXT,payload TEXT,status TEXT,resolution TEXT,PRIMARY KEY(tenant,id))"
        )

    @app.get("/v1/transfers")
    def transfers(claims=Depends(identity)):
        with store.connect() as db:
            rows = db.execute(
                "SELECT t.*,b.revision,b.payload FROM transfers t JOIN batches b ON b.tenant=t.tenant AND b.id=t.batch WHERE t.tenant=? ORDER BY t.rowid DESC LIMIT 500",
                (claims["tenant"],),
            ).fetchall()
        return {
            "transfers": [
                {
                    "proposalId": r["id"],
                    "batchId": r["batch"],
                    "source": r["source"],
                    "target": r["target"],
                    "used": bool(r["used"]),
                    "revision": r["revision"],
                    "state": json.loads(r["payload"])["state"],
                    "ownerMSP": json.loads(r["payload"]).get("ownerMSP", "Org1MSP"),
                }
                for r in rows
            ]
        }

    def load(db, tenant, batch_id, revision, owner):
        row = db.execute(
            "SELECT payload FROM batches WHERE tenant=? AND id=?", (tenant, batch_id)
        ).fetchone()
        if not row:
            raise KeyError("batch")
        batch = json.loads(row[0])
        if batch["revision"] != revision:
            raise ValueError("REVISION_CONFLICT")
        if batch.get("ownerMSP", "Org1MSP") != owner:
            raise HTTPException(403, "Owner organization required")
        return batch

    def save(db, batch, actor, operation):
        if len(batch["history"]) >= 1000:
            raise ValueError("Batch history capacity reached")
        batch["revision"] += 1
        batch["history"].append(
            {
                "operation": operation,
                "state": batch["state"],
                "revision": batch["revision"],
                "actor": actor,
                "at": time.time(),
            }
        )
        db.execute(
            "UPDATE batches SET payload=?,revision=? WHERE tenant=? AND id=?",
            (canonical(batch).decode(), batch["revision"], batch["tenant"], batch["batchId"]),
        )

    @app.post("/v1/batches/{batch_id}/transfers")
    def propose(batch_id: str, body: Transfer, claims=Depends(operator)):
        tenant, actor = claims["tenant"], organization(claims)

        def action(db):
            batch = load(db, tenant, batch_id, body.expectedRevision, actor)
            if batch["state"] in {"CONSUMED", "RECALLED"} or actor == body.target:
                raise ValueError("INVALID_STATE")
            db.execute(
                "INSERT INTO transfers VALUES(?,?,?,?,?,0)",
                (tenant, body.requestId, batch_id, actor, body.target),
            )
            save(db, batch, claims["sub"], "transfer-proposed")
            enqueue(db, tenant, body.requestId, batch_id, "propose", {"target": body.target}, actor)
            return {"proposalId": body.requestId, "batch": batch}

        return execute(
            tenant,
            body.requestId,
            {"operation": "propose", "batch": batch_id, **body.model_dump()},
            action,
        )

    @app.post("/v1/transfers/{proposal}/accept")
    def accept(proposal: str, body: Accept, claims=Depends(operator)):
        tenant, actor = claims["tenant"], organization(claims)

        def action(db):
            p = db.execute(
                "SELECT * FROM transfers WHERE tenant=? AND id=?", (tenant, proposal)
            ).fetchone()
            if not p:
                raise KeyError("proposal")
            if p["used"] or actor != p["target"]:
                raise HTTPException(403, "Recipient required")
            batch = load(db, tenant, p["batch"], body.expectedRevision, p["source"])
            if batch["state"] in {"CONSUMED", "RECALLED"}:
                raise ValueError("INVALID_STATE")
            batch["ownerMSP"] = actor
            save(db, batch, claims["sub"], "transfer-accepted")
            db.execute("UPDATE transfers SET used=1 WHERE tenant=? AND id=?", (tenant, proposal))
            chain_proposal = "job_" + digest({"tenant": tenant, "request": proposal})[:48]
            enqueue(
                db,
                tenant,
                body.requestId,
                p["batch"],
                "accept",
                {"chainProposal": chain_proposal},
                actor,
            )
            return batch

        return execute(
            tenant,
            body.requestId,
            {"operation": "accept", "proposal": proposal, **body.model_dump()},
            action,
        )

    @app.post("/v1/batch-transforms")
    def transform(body: Transform, claims=Depends(operator)):
        tenant, actor = claims["tenant"], organization(claims)
        if actor != "Org2MSP":
            raise HTTPException(403, "Processor organization required")

        def action(db):
            parents = [load(db, tenant, i, rev, actor) for i, rev in body.parents.items()]
            if (
                any(b["state"] not in {"LANDED", "PROCESSED"} for b in parents)
                or len({b["speciesId"] for b in parents}) != 1
            ):
                raise ValueError("INVALID_STATE")
            ids = [o.batchId for o in body.outputs]
            if len(set(ids)) != len(ids) or any(i in body.parents for i in ids):
                raise ValueError("Duplicate output")
            if sum(b["massGrams"] for b in parents) != body.lossGrams + sum(
                o.massGrams for o in body.outputs
            ):
                raise ValueError("MASS_CONSERVATION")
            for i in ids:
                if db.execute(
                    "SELECT 1 FROM batches WHERE tenant=? AND id=?", (tenant, i)
                ).fetchone():
                    raise ValueError("IDEMPOTENCY_CONFLICT")
            for batch in parents:
                batch["state"] = "CONSUMED"
                save(db, batch, claims["sub"], "transform")
            for output in body.outputs:
                batch = {
                    "batchId": output.batchId,
                    "tenant": tenant,
                    "ownerMSP": actor,
                    "speciesId": parents[0]["speciesId"],
                    "areaCode": "processed",
                    "massGrams": output.massGrams,
                    "state": "PROCESSED",
                    "revision": 1,
                    "parents": list(body.parents),
                    "history": [{"operation": "transform", "revision": 1, "actor": claims["sub"]}],
                    "chainStatus": "NOT_SUBMITTED",
                }
                db.execute(
                    "INSERT INTO batches VALUES(?,?,?,?)",
                    (tenant, output.batchId, canonical(batch).decode(), 1),
                )
            enqueue(
                db,
                tenant,
                body.requestId,
                parents[0]["batchId"],
                "transform",
                body.model_dump(),
                actor,
            )
            return {"outputs": ids, "lossGrams": body.lossGrams, "massConserved": True}

        return execute(
            tenant, body.requestId, {"operation": "transform", **body.model_dump()}, action
        )

    @app.post("/v1/batches/{batch_id}/cold-chain")
    def cold(batch_id: str, body: ColdRecord, claims=Depends(operator)):
        tenant = claims["tenant"]

        def action(db):
            batch = store.batch(tenant, batch_id)
            if batch.get("ownerMSP", "Org1MSP") != organization(claims):
                raise HTTPException(403, "Owner required")
            if (
                batch["state"] != "IN_COLD_CHAIN"
                or not 0 <= body.startAt < body.endAt <= time.time()
                or body.minimumC > body.maximumC
                or body.allowedMinimumC > body.allowedMaximumC
            ):
                raise ValueError("Invalid cold-chain range/state")
            for event_id in body.sourceEventIds:
                row = db.execute(
                    "SELECT payload FROM events WHERE tenant=? AND id=?", (tenant, event_id)
                ).fetchone()
                if not row or json.loads(row[0])["deviceId"] != body.deviceId:
                    raise ValueError("Unknown device source")
            if (
                body.forecastId
                and not db.execute(
                    "SELECT 1 FROM forecasts WHERE tenant=? AND id=?", (tenant, body.forecastId)
                ).fetchone()
            ):
                raise ValueError("Unknown forecast")
            forecast = None
            if body.forecastId:
                forecast = json.loads(
                    db.execute(
                        "SELECT payload FROM forecasts WHERE tenant=? AND id=?",
                        (tenant, body.forecastId),
                    ).fetchone()[0]
                )
            record = {
                "forecastSnapshot": forecast,
                **body.model_dump(),
                "actor": claims["sub"],
                "recordedAt": time.time(),
                "excursion": body.minimumC < body.allowedMinimumC
                or body.maximumC > body.allowedMaximumC,
                "source": "linked-observations" if body.sourceEventIds else "manual-unverified",
            }
            if len(canonical(record)) > 256 * 1024:
                raise ValueError("Cold-chain evidence exceeds capacity")
            count, total = db.execute(
                "SELECT count(*),coalesce(sum(json_extract(manifest,'$.byteLength')),0) FROM evidence WHERE tenant=?",
                (tenant,),
            ).fetchone()
            if count >= 10000 or total + len(canonical(record)) > 1024 * 1024 * 1024:
                raise HTTPException(429, "Evidence capacity reached")
            manifest = app.state.evidence_store.put(canonical(record), batch_id, tenant)
            db.execute(
                "INSERT INTO evidence(tenant,id,batch,manifest,status) VALUES(?,?,?,?,?)",
                (
                    tenant,
                    manifest["evidenceId"],
                    batch_id,
                    canonical(manifest).decode(),
                    "LOCAL_PENDING",
                ),
            )
            if record["excursion"]:
                db.execute(
                    "INSERT INTO cold_issues VALUES(?,?,?,?,'OPEN',NULL)",
                    (tenant, manifest["evidenceId"], batch_id, canonical(record).decode()),
                )
            return {
                "evidenceId": manifest["evidenceId"],
                "excursion": record["excursion"],
                "issueId": manifest["evidenceId"] if record["excursion"] else None,
                "status": "LOCAL_PENDING",
            }

        return execute(
            tenant,
            body.requestId,
            {"operation": "cold", "batch": batch_id, **body.model_dump()},
            action,
        )

    @app.post("/v1/cold-issues/{issue_id}/resolve")
    def resolve_cold(issue_id: str, body: ResolveCold, claims=Depends(operator)):
        tenant = claims["tenant"]

        def action(db):
            row = db.execute(
                "SELECT * FROM cold_issues WHERE tenant=? AND id=?", (tenant, issue_id)
            ).fetchone()
            if not row:
                raise KeyError("issue")
            if row["status"] != "OPEN":
                raise ValueError("REVISION_CONFLICT")
            batch = store.batch(tenant, row["batch"])
            if batch.get("ownerMSP", "Org1MSP") != organization(claims):
                raise HTTPException(403, "Owner required")
            resolution = {
                **body.model_dump(),
                "issueId": issue_id,
                "actor": claims["sub"],
                "resolvedAt": time.time(),
            }
            if body.disposition == "recall":
                if batch["state"] in {"CONSUMED", "RECALLED"}:
                    raise ValueError("INVALID_STATE")
                batch["state"] = "RECALLED"
                save(db, batch, claims["sub"], "cold-recall")
                enqueue(
                    db,
                    tenant,
                    body.requestId,
                    batch["batchId"],
                    "transition",
                    {"nextState": "RECALLED"},
                    organization(claims),
                )
            manifest = app.state.evidence_store.put(canonical(resolution), batch["batchId"], tenant)
            db.execute(
                "INSERT INTO evidence(tenant,id,batch,manifest,status) VALUES(?,?,?,?,'LOCAL_PENDING')",
                (tenant, manifest["evidenceId"], batch["batchId"], canonical(manifest).decode()),
            )
            resolution["evidenceId"] = manifest["evidenceId"]
            db.execute(
                "UPDATE cold_issues SET status='RESOLVED',resolution=? WHERE tenant=? AND id=?",
                (canonical(resolution).decode(), tenant, issue_id),
            )
            return {
                "issueId": issue_id,
                "status": "RESOLVED",
                "disposition": body.disposition,
                "evidenceId": manifest["evidenceId"],
            }

        return execute(
            tenant,
            body.requestId,
            {"operation": "resolve-cold", "issueId": issue_id, **body.model_dump()},
            action,
        )

    @app.post("/v1/ledger/sync")
    def sync(claims=Depends(operator)):
        return app.state.ledger.sync(claims["tenant"])

    @app.get("/v1/ledger/status")
    def status(claims=Depends(identity)):
        with store.connect() as db:
            rows = db.execute(
                "SELECT id,batch,operation,status,receipt FROM ledger_jobs WHERE tenant=? ORDER BY rowid DESC LIMIT 1000",
                (claims["tenant"],),
            ).fetchall()
        return {"jobs": [dict(r) for r in rows]}

    @app.get("/v1/batches/{batch_id}/ledger-verify")
    def verify_batch(batch_id: str, claims=Depends(identity)):
        from marine.fabric import Fabric

        batch = store.batch(claims["tenant"], batch_id)
        try:
            remote = Fabric(organization(claims)).query(
                "GetBatch", chain_id(claims["tenant"], batch_id)
            )
        except (RuntimeError, OSError, KeyError, ValueError):
            return {"status": "unavailable", "verified": False}
        with store.connect() as db:
            pending = db.execute(
                "SELECT count(*) FROM ledger_jobs WHERE tenant=? AND batch=? AND status!='committed'",
                (claims["tenant"], batch_id),
            ).fetchone()[0]
        same = bool(
            remote
            and remote["state"] == batch["state"]
            and remote["ownerMSP"] == batch.get("ownerMSP", "Org1MSP")
            and remote["massGrams"] == batch["massGrams"]
        )
        return {
            "status": "verified" if same and not pending else "pending_or_mismatch",
            "verified": same and not pending,
            "chainBatch": remote,
            "pendingCommands": pending,
        }
