"""Inspect/sync/retry captured events; originals stay in the encrypted journal."""

import argparse
import json
from pathlib import Path
from marine.edge_samples import CaptureJournal

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["status", "sync", "retry"])
    p.add_argument("--outbox", required=True)
    p.add_argument("--endpoint")
    p.add_argument("--token-file")
    p.add_argument("--ids", nargs="*", default=[])
    a = p.parse_args()
    journal = CaptureJournal(a.outbox)
    if a.action == "retry":
        journal.outbox.resume(a.ids)
    if a.action == "sync":
        if not a.endpoint or not a.token_file:
            p.error("sync requires --endpoint and --token-file")
        print(
            json.dumps(
                {"acknowledged": journal.flush(a.endpoint, Path(a.token_file).read_text().strip())}
            )
        )
    with journal.outbox.connect() as db:
        rows = db.execute(
            "SELECT c.id,c.review,c.status,p.status,p.attempt FROM captured c LEFT JOIN pending p ON p.id=c.id ORDER BY c.created DESC LIMIT 100"
        ).fetchall()
    print(
        json.dumps(
            {
                "samples": [
                    {
                        "id": r[0],
                        "needsReview": bool(r[1]),
                        "delivery": r[2],
                        "queueState": r[3],
                        "attempts": r[4],
                    }
                    for r in rows
                ]
            }
        )
    )
