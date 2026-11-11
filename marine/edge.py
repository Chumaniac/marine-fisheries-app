"""Durable bounded edge outbox. Lost responses cause safe idempotent resend."""

import json
import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx


class Outbox:
    def __init__(self, path, capacity=10000):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.capacity = str(path), capacity
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending(id TEXT PRIMARY KEY,payload TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,next REAL NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'pending')"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def put(self, event):
        payload = json.dumps(event, sort_keys=True, allow_nan=False)
        if len(payload.encode()) > 8192:
            raise ValueError("Event too large")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT payload FROM pending WHERE id=?", (event["eventId"],)
            ).fetchone()
            if old:
                if old[0] != payload:
                    raise ValueError("Conflicting event identity")
                return
            if db.execute("SELECT count(*) FROM pending").fetchone()[0] >= self.capacity:
                raise OverflowError("Outbox full; acquisition must stop or degrade explicitly")
            db.execute("INSERT INTO pending(id,payload) VALUES(?,?)", (event["eventId"], payload))

    def resume(self, identities):
        if len(identities) > 100:
            raise ValueError("Resume at most 100 explicit IDs")
        with self.connect() as db:
            captured = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='captured'"
            ).fetchone()
            for identity in identities:
                is_capture = (
                    captured
                    and db.execute("SELECT 1 FROM captured WHERE id=?", (identity,)).fetchone()
                )
                db.execute(
                    "UPDATE pending SET status=?,next=0,attempt=0 WHERE id=? AND status='action_required'",
                    ("evidence_pending" if is_capture else "pending", identity),
                )

    def flush_once(self, endpoint, token):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,payload,attempt FROM pending WHERE status='pending' AND next<=? ORDER BY rowid LIMIT 100",
                (time.time(),),
            ).fetchall()
        if not rows:
            return 0
        # Stable event IDs enforce receiver dedup even when batching changes after a restart.
        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                response = client.post(
                    endpoint,
                    json={"events": [json.loads(r[1]) for r in rows]},
                    headers={"Authorization": f"Bearer {token}"},
                )
            if response.status_code in {400, 401, 403, 409, 413, 422}:
                with self.connect() as db:
                    db.executemany(
                        "UPDATE pending SET status='action_required' WHERE id=?",
                        [(row[0],) for row in rows],
                    )
                return 0
            response.raise_for_status()
            accepted = set(response.json()["acceptedEventIds"])
        except (httpx.HTTPError, KeyError, ValueError):
            with self.connect() as db:
                for identity, _, attempt in rows:
                    # Persisted schedule survives process exit, no busy in-memory retries.
                    delay = random.uniform(0, min(300, 2 * 2 ** min(attempt, 8)))
                    db.execute(
                        "UPDATE pending SET attempt=attempt+1,next=? WHERE id=?",
                        (time.time() + delay, identity),
                    )
            return 0
        with self.connect() as db:
            for identity, _, _ in rows:
                if identity in accepted:
                    db.execute("DELETE FROM pending WHERE id=?", (identity,))
        return len(accepted.intersection(row[0] for row in rows))
