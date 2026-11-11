"""Batch state machine, encrypted evidence, IPFS adapter and explicit anchoring states."""

import hashlib
import json
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import Depends, HTTPException, UploadFile, Query

from marine.storage import canonical, digest, CAPACITIES

from marine.commands import Command as Command, CreateBatch, Transition
from marine.evidence_store import EvidenceStore, MAX_EVIDENCE_BYTES
from marine.lineage import register_lineage


def register_trace(app, store, identity, operator, folder):
    from marine.logistics import LedgerSync, enqueue, organization, register_logistics

    app.state.ledger = LedgerSync(store)
    objects = EvidenceStore(folder)
    endpoints = objects.endpoints()
    if endpoints and len(endpoints) < objects.required_replicas():
        raise ValueError("Replica policy exceeds configured nodes")
    app.state.evidence_store = objects
    register_lineage(app, store, identity)
    transitions = {
        "CREATED": {"LANDED", "RECALLED"},
        "LANDED": {"PROCESSED", "IN_COLD_CHAIN", "RECALLED"},
        "PROCESSED": {"IN_COLD_CHAIN", "RECALLED"},
        "IN_COLD_CHAIN": {"DELIVERED", "RECALLED"},
        "DELIVERED": {"RECALLED"},
        "RECALLED": set(),
    }

    def execute(tenant, request_id, payload, action):
        requesthash = digest(payload)
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT hash,payload FROM receipts WHERE tenant=? AND id=?", (tenant, request_id)
            ).fetchone()
            if old:
                if old["hash"] != requesthash:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                return json.loads(old["payload"])
            result = action(db)
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?)",
                (tenant, request_id, requesthash, canonical(result).decode()),
            )
            return result

    @app.post("/v1/batches", status_code=201)
    def create(body: CreateBatch, claims=Depends(operator)):
        if organization(claims) != "Org1MSP":
            raise HTTPException(403, "Harvest organization required")

        def action(db):
            if db.execute(
                "SELECT 1 FROM batches WHERE tenant=? AND id=?", (claims["tenant"], body.batchId)
            ).fetchone():
                raise ValueError("IDEMPOTENCY_CONFLICT")
            if (
                db.execute(
                    "SELECT count(*) FROM batches WHERE tenant=?", (claims["tenant"],)
                ).fetchone()[0]
                >= CAPACITIES['batches']
            ):
                raise HTTPException(429, "Batch capacity reached")
            batch = {
                **body.model_dump(exclude={"requestId"}),
                "tenant": claims["tenant"],
                "ownerMSP": organization(claims),
                "revision": 1,
                "state": "CREATED",
                "chainStatus": "NOT_SUBMITTED",
                "history": [{"state": "CREATED", "revision": 1, "actor": claims["sub"]}],
            }
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?)",
                (claims["tenant"], body.batchId, canonical(batch).decode(), 1),
            )
            enqueue(
                db,
                claims["tenant"],
                body.requestId,
                body.batchId,
                "create",
                body.model_dump(),
                organization(claims),
            )
            return batch

        return execute(
            claims["tenant"], body.requestId, {"operation": "create", **body.model_dump()}, action
        )

    @app.post("/v1/batches/{batch_id}/transitions")
    def transition(batch_id: str, body: Transition, claims=Depends(operator)):
        def action(db):
            row = db.execute(
                "SELECT payload,revision FROM batches WHERE tenant=? AND id=?",
                (claims["tenant"], batch_id),
            ).fetchone()
            if not row:
                raise KeyError("batch")
            batch = json.loads(row["payload"])
            if batch.get("ownerMSP", "Org1MSP") != organization(claims):
                raise HTTPException(403, "Owner required")
            if len(batch["history"]) >= 1000:
                raise ValueError("Batch history capacity reached")
            if row["revision"] != body.expectedRevision:
                raise ValueError("REVISION_CONFLICT")
            if (
                body.nextState == "DELIVERED"
                and db.execute(
                    "SELECT 1 FROM cold_issues WHERE tenant=? AND batch=? AND status='OPEN'",
                    (claims["tenant"], batch_id),
                ).fetchone()
            ):
                raise HTTPException(409, "冷链异常尚未处理，不能送达")
            if body.nextState not in transitions[batch["state"]]:
                raise ValueError("INVALID_STATE")
            batch["revision"] += 1
            batch["state"] = body.nextState
            batch["history"].append(
                {"state": body.nextState, "revision": batch["revision"], "actor": claims["sub"]}
            )
            db.execute(
                "UPDATE batches SET payload=?,revision=? WHERE tenant=? AND id=?",
                (canonical(batch).decode(), batch["revision"], claims["tenant"], batch_id),
            )
            enqueue(
                db,
                claims["tenant"],
                body.requestId,
                batch_id,
                "transition",
                body.model_dump(),
                organization(claims),
            )
            return batch

        return execute(
            claims["tenant"],
            body.requestId,
            {"operation": "transition", "batchId": batch_id, **body.model_dump()},
            action,
        )

    @app.get("/v1/batches/{batch_id}/trace")
    def trace(
        batch_id: str,
        evidenceAfter: int = Query(default=0, ge=0, le=2**63 - 1),
        ledgerAfter: int = Query(default=0, ge=0, le=2**63 - 1),
        issueAfter: int = Query(default=0, ge=0, le=2**63 - 1),
        claims=Depends(identity),
    ):
        batch = store.batch(claims["tenant"], batch_id)
        scope = (claims["tenant"], batch_id)
        with store.connect() as db:
            db.execute("BEGIN")
            evidence_rows = db.execute(
                "SELECT rowid AS cursor,* FROM evidence WHERE tenant=? AND batch=? AND rowid>? ORDER BY rowid LIMIT 101",
                (*scope, evidenceAfter),
            ).fetchall()
            ledger_rows = db.execute(
                "SELECT rowid AS cursor,id,operation,status FROM ledger_jobs WHERE tenant=? AND batch=? AND status!='committed' AND rowid>? ORDER BY rowid LIMIT 101",
                (*scope, ledgerAfter),
            ).fetchall()
            issue_rows = db.execute(
                "SELECT rowid AS cursor,* FROM cold_issues WHERE tenant=? AND batch=? AND rowid>? ORDER BY rowid LIMIT 101",
                (*scope, issueAfter),
            ).fetchall()
            evidence_total, committed, submitted = db.execute(
                "SELECT count(*),sum(status='COMMITTED'),sum(status='SUBMITTED') FROM evidence WHERE tenant=? AND batch=?",
                scope,
            ).fetchone()
            pending_total = db.execute(
                "SELECT count(*) FROM ledger_jobs WHERE tenant=? AND batch=? AND status!='committed'",
                scope,
            ).fetchone()[0]
            issue_total, open_total = db.execute(
                "SELECT count(*),sum(status='OPEN') FROM cold_issues WHERE tenant=? AND batch=?",
                scope,
            ).fetchone()
        if committed:
            batch = {**batch, "chainStatus": "EVIDENCE_ANCHORED"}
        elif submitted:
            batch = {**batch, "chainStatus": "SUBMITTED"}
        evidence = [
            {
                "evidenceId": r["id"],
                "status": r["status"],
                "manifestCommitment": digest(json.loads(r["manifest"])),
                "receipt": json.loads(r["receipt"]) if r["receipt"] else None,
            }
            for r in evidence_rows[:100]
        ]
        issues = [
            {
                "issueId": r["id"],
                "status": r["status"],
                "record": json.loads(r["payload"]),
                "resolution": json.loads(r["resolution"]) if r["resolution"] else None,
            }
            for r in issue_rows[:100]
        ]

        def cursor(rows, after):
            return rows[min(len(rows), 100) - 1]["cursor"] if rows else after

        return {
            "batch": batch,
            "evidence": evidence,
            "coldIssues": issues,
            "ledgerCommands": [
                {"id": r["id"], "operation": r["operation"], "status": r["status"]}
                for r in ledger_rows[:100]
            ],
            "hasMoreEvidence": len(evidence_rows) > 100,
            "nextEvidenceCursor": cursor(evidence_rows, evidenceAfter),
            "hasMoreLedgerCommands": len(ledger_rows) > 100,
            "nextLedgerCursor": cursor(ledger_rows, ledgerAfter),
            "hasMoreColdIssues": len(issue_rows) > 100,
            "nextIssueCursor": cursor(issue_rows, issueAfter),
            "totalEvidence": evidence_total,
            "totalLedgerCommands": pending_total,
            "totalColdIssues": issue_total,
            "openColdIssues": open_total or 0,
            "stateVerification": "pending" if pending_total else "not_queried",
            "ledgerVerification": "not_queried",
        }

    @app.post("/v1/batches/{batch_id}/evidence", status_code=201)
    def evidence(batch_id: str, file: UploadFile, claims=Depends(operator)):
        store.batch(claims["tenant"], batch_id)
        data = file.file.read(MAX_EVIDENCE_BYTES + 1)
        if not data or len(data) > MAX_EVIDENCE_BYTES:
            raise HTTPException(413, "Evidence must be 1 to 256 KiB in this API")
        content_hash = hashlib.sha256(data).hexdigest()
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id,status,manifest FROM evidence WHERE tenant=? AND batch=? AND json_extract(manifest,'$.rawSha256')=?",
                (claims["tenant"], batch_id, content_hash),
            ).fetchone()
            if existing:
                try:
                    objects.verify(json.loads(existing["manifest"]))
                except (OSError, ValueError, KeyError, TypeError, InvalidTag) as error:
                    # Preserve the original manifest and any committed chain binding.
                    raise HTTPException(
                        409,
                        "Existing evidence failed integrity verification; "
                        "restore the original encrypted object before retrying",
                    ) from error
                return {"evidenceId": existing["id"], "status": existing["status"]}
            count, total = db.execute(
                "SELECT count(*),coalesce(sum(json_extract(manifest,'$.byteLength')),0) FROM evidence WHERE tenant=?",
                (claims["tenant"],),
            ).fetchone()
            if count >= 10000 or total + len(data) > 1024 * 1024 * 1024:
                raise HTTPException(429, "Evidence capacity reached; archive before accepting more")
            manifest = objects.put(data, batch_id, claims["tenant"])
            manifest["originalName"] = Path(file.filename or "evidence.bin").name[:128]
            db.execute(
                "INSERT INTO evidence(tenant,id,batch,manifest,status) VALUES(?,?,?,?,?)",
                (
                    claims["tenant"],
                    manifest["evidenceId"],
                    batch_id,
                    canonical(manifest).decode(),
                    "LOCAL_PENDING",
                ),
            )
        return {"evidenceId": manifest["evidenceId"], "status": "LOCAL_PENDING"}

    @app.get("/v1/evidence/{evidence_id}/content")
    def content(evidence_id: str, claims=Depends(identity)):
        from fastapi.responses import Response
        from urllib.parse import quote

        with store.connect() as db:
            row = db.execute(
                "SELECT manifest FROM evidence WHERE tenant=? AND id=?",
                (claims["tenant"], evidence_id),
            ).fetchone()
        if not row:
            raise KeyError("evidence")
        manifest = json.loads(row[0])
        raw = objects.read(manifest)
        name = manifest.get("originalName", "evidence-" + evidence_id + ".json")
        return Response(
            raw,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)},
        )

    @app.post("/v1/evidence/{evidence_id}/pin")
    def pin(evidence_id: str, claims=Depends(operator)):
        with store.connect() as db:
            row = db.execute(
                "SELECT * FROM evidence WHERE tenant=? AND id=?", (claims["tenant"], evidence_id)
            ).fetchone()
        if not row:
            raise KeyError("evidence")
        manifest = json.loads(row["manifest"])
        try:
            manifest = objects.pin(manifest)
        except httpx.HTTPError:
            raise HTTPException(503, "IPFS unavailable; local original retained") from None
        if not manifest["cipherCid"]:
            raise HTTPException(503, "IPFS not configured; local original retained")
        with store.connect() as db:
            db.execute(
                "UPDATE evidence SET manifest=?,status=CASE WHEN status IN ('COMMITTED','SUBMITTED') THEN status ELSE 'PIN_VERIFIED' END WHERE tenant=? AND id=?",
                (canonical(manifest).decode(), claims["tenant"], evidence_id),
            )
        return {
            "evidenceId": evidence_id,
            "status": row["status"]
            if row["status"] in {"COMMITTED", "SUBMITTED"}
            else "PIN_VERIFIED",
            "manifestCommitment": digest(manifest),
        }

    @app.post("/v1/evidence/{evidence_id}/anchor")
    def anchor(evidence_id: str, claims=Depends(operator)):
        with store.connect() as db:
            row = db.execute(
                "SELECT * FROM evidence WHERE tenant=? AND id=?", (claims["tenant"], evidence_id)
            ).fetchone()
        if not row:
            raise KeyError("evidence")
        if row["status"] == "COMMITTED":
            return json.loads(row["receipt"])
        if row["status"] not in {"PIN_VERIFIED", "SUBMITTED"}:
            raise HTTPException(409, "Pin and verify evidence before anchoring")
        from marine.fabric import Fabric

        try:
            fabric = Fabric(organization(claims))
        except (RuntimeError, KeyError):
            raise HTTPException(503, "Fabric not configured") from None
        sync_result = app.state.ledger.sync(claims["tenant"])
        if sync_result["status"] not in {"checked"}:
            raise HTTPException(503, "Batch commands must be reconciled before anchoring")
        with store.connect() as db:
            db.execute(
                "UPDATE evidence SET status='SUBMITTED' WHERE tenant=? AND id=?",
                (claims["tenant"], evidence_id),
            )
        try:
            receipt = fabric.anchor(
                store.batch(claims["tenant"], row["batch"]), json.loads(row["manifest"])
            )
        except (RuntimeError, OSError, ValueError):
            raise HTTPException(
                503, "Commit unconfirmed; retry reconciles existing receipt"
            ) from None
        with store.connect() as db:
            db.execute(
                "UPDATE evidence SET status='COMMITTED',receipt=? WHERE tenant=? AND id=?",
                (canonical(receipt).decode(), claims["tenant"], evidence_id),
            )
        return receipt

    @app.get("/v1/evidence/{evidence_id}/verify")
    def verify(evidence_id: str, claims=Depends(identity)):
        with store.connect() as db:
            row = db.execute(
                "SELECT manifest,status,receipt FROM evidence WHERE tenant=? AND id=?",
                (claims["tenant"], evidence_id),
            ).fetchone()
        if not row:
            raise KeyError("evidence")
        try:
            valid = objects.verify(json.loads(row["manifest"]))
        except Exception:
            valid = False
        chain_valid = False
        if row["status"] == "COMMITTED" and row["receipt"]:
            from marine.fabric import Fabric

            try:
                receipt = json.loads(row["receipt"])
                onchain = Fabric(organization(claims)).query(
                    "GetEvidence", receipt["chainBatchId"], evidence_id
                )
                chain_valid = bool(
                    onchain and onchain["commitment"] == digest(json.loads(row["manifest"]))
                )
            except (RuntimeError, OSError, ValueError, KeyError):
                pass
        return {
            "contentVerified": valid,
            "ipfs": objects.remote_verify(json.loads(row["manifest"])),
            "chainStatus": row["status"],
            "chainVerified": chain_valid,
        }

    register_logistics(app, store, identity, operator, execute)
