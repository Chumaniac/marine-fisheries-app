"""Export explicitly reviewed labels; never copy model predictions into training labels."""

import argparse
import csv
import json
from pathlib import Path

from marine.storage import Store
from marine.trace import EvidenceStore


def export(database, tenant, output):
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Choose an empty destination; existing training assets are immutable")
    output.mkdir(parents=True, exist_ok=True)
    store = Store(Path(database) / "marine.sqlite3")
    objects = EvidenceStore(database)
    with store.connect() as db:
        records = [
            json.loads(row["payload"])
            for row in db.execute(
                "SELECT payload FROM reviews WHERE tenant=? LIMIT 1000", (tenant,)
            )
        ]
    rows = []
    for record in records:
        if record["state"] != "reviewed" or record["label"] == "unknown":
            continue
        group = record["history"][-1]["groupId"]
        image = objects.read(record["manifest"])
        name = record["id"] + ".image"
        (output / name).write_bytes(image)
        import cv2
        import numpy as np
        import hashlib

        decoded = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        if decoded is None:
            raise ValueError("Invalid reviewed image")
        pixels_hash = hashlib.sha256(memoryview(decoded)).hexdigest()
        rows.append(
            {
                "path": name,
                "label": record["label"],
                "group": group,
                "split": "train",
                "sha256Pixels": pixels_hash,
            }
        )
    with (output / "manifest.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=["path", "label", "group", "split", "sha256Pixels"])
        writer.writeheader()
        writer.writerows(rows)
    print(
        f"Exported {len(rows)} reviewed training samples; independent validation/test manifests remain required"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="artifacts/database")
    parser.add_argument("--tenant", default="demo")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    export(args.database, args.tenant, args.output)
