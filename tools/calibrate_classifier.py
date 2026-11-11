"""Fit temperature on validation only; report a frozen test once; preserve model weights."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from marine.inference import Classifier
from marine.storage import canonical


def probabilities(logits, temperature):
    x = logits / temperature
    y = np.exp(x - x.max(axis=1, keepdims=True))
    return y / y.sum(axis=1, keepdims=True)


def metrics(logits, labels, t):
    p = probabilities(logits, t)
    confidence = p.max(1)
    correct = p.argmax(1) == labels
    ece = 0
    for low in np.arange(0, 1, 0.1):
        mask = (confidence >= low) & (confidence < low + 0.1 + 1e-8)
        if mask.any():
            ece += float(mask.mean() * abs(correct[mask].mean() - confidence[mask].mean()))
    return {
        "nll": float(-np.log(np.maximum(p[np.arange(len(labels)), labels], 1e-12)).mean()),
        "ece10": ece,
        "accuracy": float(correct.mean()),
        "samples": len(labels),
    }


def calibrate(directory, manifest_path):
    directory = Path(directory)
    dest = directory / "calibration.json"
    if dest.exists():
        raise ValueError("Calibration exists; reuse it or create a new version directory")
    classifier = Classifier(directory)
    rows = list(csv.DictReader(open(manifest_path)))
    splits = {}
    for split in ["validation", "test"]:
        logits = []
        labels = []
        hashes = []
        for row in rows:
            if row["split"] != split:
                continue
            image = (Path(manifest_path).parent / row["path"]).read_bytes()
            result = classifier.predict(image, include_logits=True)
            logits.append(result["_logits"])
            labels.append(classifier.labels.index(row["label"]))
            hashes.append(row["sha256Pixels"])
        splits[split] = (np.array(logits), np.array(labels), hashes)
    x, y, _ = splits["validation"]
    temperatures = np.exp(np.linspace(np.log(0.2), np.log(5), 161))
    t = float(min(temperatures, key=lambda value: metrics(x, y, value)["nll"]))
    p = probabilities(x, t)
    histogram = np.histogram(p.max(1), bins=np.linspace(0, 1, 11))[0].tolist()
    report = {
        "modelVersion": classifier.version,
        "temperature": t,
        "fitSplit": "validation",
        "confidenceHistogram": histogram,
        "validationHash": hashlib.sha256(canonical(splits["validation"][2])).hexdigest(),
        "testHash": hashlib.sha256(canonical(splits["test"][2])).hexdigest(),
        "metrics": {
            split: {
                "before": metrics(values[0], values[1], 1),
                "after": metrics(values[0], values[1], t),
            }
            for split, values in splits.items()
        },
        "limitation": "Calibration uses the existing public-data validation split, also used for hyperparameters; it is not an independent field calibration set.",
    }
    test = report["metrics"]["test"]
    report["promotion"] = (
        "approved"
        if test["after"]["nll"] <= test["before"]["nll"]
        and test["after"]["ece10"] <= test["before"]["ece10"]
        else "rejected-heldout-regression"
    )
    report["activeConfidenceHistogram"] = np.histogram(
        probabilities(x, t if report["promotion"] == "approved" else 1).max(1),
        bins=np.linspace(0, 1, 11),
    )[0].tolist()
    dest.write_bytes(canonical(report))
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="artifacts/models/norfisk")
    p.add_argument("--manifest", default="data/processed/norfisk/manifest.csv")
    a = p.parse_args()
    print(json.dumps(calibrate(a.model, a.manifest), indent=2))
