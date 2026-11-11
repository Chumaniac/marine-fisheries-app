"""Prepare licensed NorFisk crops; filename blocks are provisional, NOT video identities."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np


def main(args):
    root, out = Path(args.source), Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    candidates = []
    for label in ["saithe", "salmonid"]:
        folders = list(root.rglob(label))
        folder = next(p for p in folders if (p / "img").is_dir())
        images = sorted((folder / "img").glob("*.png"))
        blocks = sorted({i // 250 for i in range(len(images))})
        rng.shuffle(blocks)
        test_count = max(1, round(len(blocks) * 0.15))
        validation_count = max(1, round(len(blocks) * 0.15))
        assignment = {
            b: "test"
            if j < test_count
            else "validation"
            if j < test_count + validation_count
            else "train"
            for j, b in enumerate(blocks)
        }
        for i, path in enumerate(images):
            # Guard against adjacent frames crossing provisional block boundaries.
            if i % 250 < 10 or i % 250 >= 240:
                continue
            candidates.append(
                (path, label, f"{label}-filename-block-{i // 250:03}", assignment[i // 250], folder)
            )
    rng.shuffle(candidates)
    if args.limit:
        candidates = candidates[: args.limit]
    rows, seen, invalid = [], set(), 0
    for path, label, group, split, folder in candidates:
        image = cv2.imread(str(path))
        annotation = folder / "annotations" / (path.stem + ".txt")
        if image is None or not annotation.exists():
            invalid += 1
            continue
        lines = list(csv.reader(annotation.read_text().splitlines()))
        boxes = []
        for fields in lines:
            try:
                x1, y1, x2, y2 = map(int, fields[2:6])
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(image.shape[1], x2), min(image.shape[0], y2)
                if x2 - x1 >= 16 and y2 - y1 >= 16:
                    boxes.append((x1, y1, x2, y2))
            except (ValueError, TypeError):
                continue
        if not boxes:
            invalid += 1
            continue
        # One crop/source prevents image-level duplicate weighting; choose largest annotated fish.
        x1, y1, x2, y2 = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        crop = image[y1:y2, x1:x2]
        digest = hashlib.sha256(crop.tobytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        dest = out / label / path.name
        dest.parent.mkdir(exist_ok=True)
        cv2.imwrite(str(dest), crop)
        rows.append(
            {
                "path": str(dest.relative_to(out)),
                "label": label,
                "group": group,
                "split": split,
                "sha256Pixels": digest,
            }
        )
    rows.sort(key=lambda r: r["path"])
    with (out / "manifest.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=["path", "label", "group", "split", "sha256Pixels"])
        writer.writeheader()
        writer.writerows(rows)
    report = {
        "source": "https://doi.org/10.18710/H5G3K5",
        "license": "CC0-1.0",
        "archiveMd5": "6ec799e74054dec2dadb6f9179e51ea5",
        "preparedImages": len(rows),
        "invalid": invalid,
        "labels": ["saithe", "salmonid"],
        "counts": {s: sum(r["split"] == s for r in rows) for s in ["train", "validation", "test"]},
        "limitation": "Filename-block split with boundary gaps; original video/individual IDs unavailable. Salmonid combines salmon and trout. No cross-voyage or eight-species claim.",
    }
    (out / "provenance.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", default="data/raw/norfisk")
    p.add_argument("--output", default="data/processed/norfisk")
    p.add_argument("--limit", type=int, default=4800)
    main(p.parse_args())
