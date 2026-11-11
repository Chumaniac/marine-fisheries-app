"""Validate the design's synthetic examples, not a running production system."""

import copy
import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[1]
schema = json.loads((ROOT / "contracts/events.schema.json").read_text())
examples = json.loads((ROOT / "contracts/examples.json").read_text())
Draft202012Validator.check_schema(schema)
validator = Draft202012Validator(schema, format_checker=FormatChecker())


def validate(event):
    validator.validate(event)
    if event["eventType"] == "forecast.updated":
        data = event["data"]
        if not data["lower"] <= data["point"] <= data["upper"]:
            raise ValueError("Forecast interval order is invalid")
        origin = datetime.fromisoformat(data["originAt"].replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(data["validUntil"].replace("Z", "+00:00"))
        watermark = datetime.fromisoformat(data["sourceWatermark"].replace("Z", "+00:00"))
        if not watermark <= origin < expiry:
            raise ValueError("Forecast time relation is invalid")


for example in examples:
    validate(example)

negative_cases = []
for index, path, value in [
    (0, ("data", "confidence"), 1.1),
    (0, ("eventType",), "unknown.event"),
    (0, ("occurredAt",), "not-a-timestamp"),
    (0, ("occurredAt",), "2026-10-09T10:00:00+08:00"),
    (2, ("data", "lower"), 20),
    (2, ("data", "validUntil"), "2026-10-08T02:00:00Z"),
    (3, ("data", "validationCode"), "MVCC_READ_CONFLICT"),
    (3, ("data", "manifestCommitment"), "placeholder"),
]:
    event = copy.deepcopy(examples[index])
    destination = event
    for part in path[:-1]:
        destination = destination[part]
    destination[path[-1]] = value
    negative_cases.append(event)

missing = copy.deepcopy(examples[0])
del missing["tenantId"]
negative_cases.append(missing)

for event in negative_cases:
    try:
        validate(event)
    except (ValueError, ValidationError):
        continue
    raise AssertionError("Invalid example was accepted")

print(f"PASS: {len(examples)} positive examples and {len(negative_cases)} invalid cases")
