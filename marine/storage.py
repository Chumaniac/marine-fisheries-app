"""Local durable SQL adapter. Transactions and parameter binding are mandatory."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

import jcs

# Shared with read-only operational observations; admission keeps the same limits.
CAPACITIES = {"events": 500000, "outbox_active": 100000, "labels": 10000,
              "batches": 10000, "reviews": 1000, "review_bytes": 256 * 1024 * 1024,
              "edge_admissions": 1000, "edge_evidence": 1000}

# Keep the indexed expression and quality query identical. Changing this budget
# requires a new index version. Oversized or malformed payloads map to NULL.
QUALITY_RECORD_BYTES = 8192
QUALITY_SITE_EXPRESSION = (
    f"CASE WHEN length(CAST(payload AS BLOB))<={QUALITY_RECORD_BYTES} "
    "THEN CASE WHEN json_valid(payload) THEN json_extract(payload,'$.siteId') END END"
)


def canonical(value):
    return jcs.canonicalize(value)


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS events(tenant TEXT,id TEXT,payload TEXT NOT NULL,hash TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS outbox(tenant TEXT,id TEXT,topic TEXT,payload TEXT,status TEXT DEFAULT 'pending',attempt INTEGER DEFAULT 0,next REAL DEFAULT 0,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS windows(tenant TEXT,id TEXT,payload TEXT,observations INTEGER,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS labels(tenant TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS scoped_models(tenant TEXT,site TEXT,species TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,site,species));
            CREATE TABLE IF NOT EXISTS models(tenant TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches(tenant TEXT,id TEXT,payload TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS receipts(tenant TEXT,id TEXT,hash TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS evidence(tenant TEXT,id TEXT,batch TEXT,manifest TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT,PRIMARY KEY(tenant,id));
            CREATE TABLE IF NOT EXISTS reviews(tenant TEXT,id TEXT,payload TEXT NOT NULL,PRIMARY KEY(tenant,id));
            CREATE INDEX IF NOT EXISTS events_time ON events(tenant,created);
            CREATE INDEX IF NOT EXISTS evidence_batch ON evidence(tenant,batch);
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS events_quality_site_time_v1 "
                f"ON events(tenant,({QUALITY_SITE_EXPRESSION}),created DESC,id DESC)"
            )
            # Add metadata without replacing legacy tables or changing payloads.
            db.execute("BEGIN IMMEDIATE")
            if "error_code" not in {row["name"] for row in db.execute("PRAGMA table_info(outbox)")}:
                db.execute("ALTER TABLE outbox ADD COLUMN error_code TEXT")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def accept(self, tenant, events, connection=None):
        with self.connect() if connection is None else nullcontext(connection) as db:
            if connection is None:
                db.execute("BEGIN IMMEDIATE")
            elif not db.in_transaction:
                raise ValueError("Caller must own a transaction")
            existing_count = db.execute(
                "SELECT count(*) FROM events WHERE tenant=?", (tenant,)
            ).fetchone()[0]
            pending_count = db.execute(
                "SELECT count(*) FROM outbox WHERE tenant=? AND status IN ('pending','action_required','dispatching','outcome_unknown')",
                (tenant,),
            ).fetchone()[0]
            for event in events:
                identity = event["eventId"]
                body = canonical(event).decode()
                h = digest({k: v for k, v in event.items() if k != "receivedAt"})
                old = db.execute(
                    "SELECT hash FROM events WHERE tenant=? AND id=?", (tenant, identity)
                ).fetchone()
                if old:
                    if old["hash"] != h:
                        raise ValueError("IDEMPOTENCY_CONFLICT")
                    continue
                if existing_count >= CAPACITIES["events"] or pending_count >= CAPACITIES["outbox_active"]:
                    raise ValueError("INGEST_CAPACITY_REACHED")
                existing_count += 1
                pending_count += 1
                db.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?)", (tenant, identity, body, h, time.time())
                )
                db.execute(
                    "INSERT INTO outbox(tenant,id,topic,payload) VALUES(?,?,?,?)",
                    (
                        tenant,
                        identity,
                        "fish.observations.v1"
                        if event["eventType"] == "fish.observation"
                        else "environment.readings.v1",
                        body,
                    ),
                )
        return [e["eventId"] for e in events]

    def events(self, tenant, limit=1000, *, site=None, species=None, since=None):
        filters = ["tenant=?"]
        values = [tenant]
        if site is not None:
            filters.append("json_extract(payload,'$.siteId')=?")
            values.append(site)
        if species is not None:
            # Environmental readings belong to the site, not an individual species.
            filters.append(
                "(json_extract(payload,'$.eventType')='environment.reading' "
                "OR json_extract(payload,'$.data.speciesId')=?)"
            )
            values.append(species)
        if since is not None:
            filters.append("unixepoch(json_extract(payload,'$.occurredAt'))>=?")
            values.append(since)
        if not 1 <= limit <= 5001:
            raise ValueError("EVENT_PAGE_LIMIT")
        values.append(limit)
        with self.connect() as db:
            return [
                json.loads(row["payload"])
                for row in db.execute(
                    "SELECT payload FROM events WHERE "
                    + " AND ".join(filters)
                    + " ORDER BY created DESC,id DESC LIMIT ?",
                    values,
                )
            ]

    def dashboard_metrics(self, tenant, site, species, clock):
        """Aggregate counters in SQL; cap display buckets without truncating totals."""
        fish = "tenant=? AND json_extract(payload,'$.siteId')=? AND json_extract(payload,'$.eventType')='fish.observation' AND json_extract(payload,'$.data.speciesId')=?"
        args = (tenant, site, species)
        with self.connect() as db:
            count = db.execute("SELECT count(*) FROM events WHERE " + fish, args).fetchone()[0]
            tracks = db.execute(
                "SELECT count(DISTINCT json_extract(payload,'$.data.trackId')) FROM events WHERE "
                + fish
                + " AND json_extract(payload,'$.data.countMethod')='track' AND unixepoch(json_extract(payload,'$.occurredAt'))>=?",
                (*args, clock - 1800),
            ).fetchone()[0]
            series = db.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M',json_extract(payload,'$.occurredAt')) AS minute,count(*) AS count FROM events WHERE "
                + fish
                + " GROUP BY minute ORDER BY minute DESC LIMIT 600",
                args,
            ).fetchall()
            latest = db.execute(
                "SELECT max(unixepoch(json_extract(payload,'$.occurredAt'))) FROM events WHERE tenant=? AND json_extract(payload,'$.siteId')=? AND (json_extract(payload,'$.eventType')='environment.reading' OR json_extract(payload,'$.data.speciesId')=?)",
                args,
            ).fetchone()[0]
            environment = db.execute(
                "SELECT payload FROM events WHERE tenant=? AND json_extract(payload,'$.siteId')=? AND json_extract(payload,'$.eventType')='environment.reading' ORDER BY unixepoch(json_extract(payload,'$.occurredAt')) DESC,id DESC LIMIT 1",
                (tenant, site),
            ).fetchone()
            sites = db.execute(
                "SELECT json_extract(payload,'$.siteId') AS site,count(*) AS count FROM events WHERE tenant=? AND json_extract(payload,'$.eventType')='fish.observation' AND json_extract(payload,'$.data.speciesId')=? GROUP BY site ORDER BY site LIMIT 1001",
                (tenant, species),
            ).fetchall()
        return {
            "observationCount": count,
            "trackCount": tracks,
            "series": dict(series),
            "latest": latest or 0,
            "environment": json.loads(environment[0])["data"] if environment else None,
            "siteCounts": dict(sites[:1000]),
            "siteCountsComplete": len(sites) <= 1000,
        }

    def labels(self, tenant):
        with self.connect() as db:
            return [
                json.loads(r["payload"])
                for r in db.execute(
                    "SELECT payload FROM labels WHERE tenant=? ORDER BY id LIMIT 10000", (tenant,)
                )
            ]

    def model(self, tenant, site="synthetic-area-A", species="saithe"):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM scoped_models WHERE tenant=? AND site=? AND species=?",
                (tenant, site, species),
            ).fetchone()
            if row:
                return json.loads(row["payload"])
            # Preserve the original single-site synthetic pilot artifact only in its original scope.
            if site == "synthetic-area-A" and species == "saithe":
                legacy = db.execute(
                    "SELECT payload FROM models WHERE tenant=?", (tenant,)
                ).fetchone()
                if legacy:
                    model = json.loads(legacy["payload"])
                    if model.get("dataSource") == "synthetic":
                        return model
            return None

    def batch(self, tenant, identity):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM batches WHERE tenant=? AND id=?", (tenant, identity)
            ).fetchone()
            if not row:
                raise KeyError("BATCH_NOT_FOUND")
            return json.loads(row["payload"])
