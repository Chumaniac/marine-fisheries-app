"""Content-free field-plan preflight; metadata is not execution or acceptance authority."""

import hashlib
import math
import re
from marine.storage import canonical

CONDITIONS = (
    "hardware",
    "camera",
    "site",
    "season",
    "waterConditions",
    "modelSha256",
    "datasetSha256",
)
METRICS = ("map", "countMae", "endToEndP95Ms", "rssPeakBytes", "temperatureC", "powerW")


def field_preflight(plan):
    if not isinstance(plan, dict) or set(plan) != {
        "conditions",
        "targets",
        "authorizationReference",
    }:
        raise ValueError("Invalid bounded field plan")
    conditions, targets = plan["conditions"], plan["targets"]
    if (
        not isinstance(conditions, dict)
        or set(conditions) != set(CONDITIONS)
        or not isinstance(targets, dict)
        or set(targets) != set(METRICS)
    ):
        raise ValueError("Invalid bounded field plan")
    missing = []
    for name in CONDITIONS:
        value = conditions[name]
        if value is None:
            missing.append("conditions." + name)
            continue
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or (name.endswith("Sha256") and not re.fullmatch("[a-f0-9]{64}", value))
        ):
            raise ValueError("Invalid bounded field plan")
    for name in METRICS:
        value = targets[name]
        if value is None:
            missing.append("targets." + name)
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 2**53 - 1:
            raise ValueError("Invalid bounded field targets")
        if name == "map" and not 0 <= value <= 1 or name != "temperatureC" and value < 0:
            raise ValueError("Invalid bounded field targets")
    reference = plan["authorizationReference"]
    if reference is None:
        missing.append("authorizationReference")
    elif not isinstance(reference, str) or not reference.strip() or len(reference) > 256:
        raise ValueError("Invalid authorization reference")
    return {
        "scope": "declared-field-plan-only",
        "planSha256": hashlib.sha256(canonical(plan)).hexdigest(),
        "planFieldsComplete": not missing,
        "missingFields": missing,
        "authorizationVerified": False,
        "fieldAccepted": False,
        "accuracyValidated": False,
        "productionApproved": False,
        "automaticProductionCutover": False,
    }
