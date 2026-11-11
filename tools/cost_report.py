"""Estimate cost from supplied usage and rates; never invent cloud bills or savings."""

import argparse
import json
import math


def estimate(usage, rates, baseline=None):
    if not usage or set(usage) != set(rates):
        raise ValueError("Usage/rate keys must match")
    if any(
        not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
        for v in [*usage.values(), *rates.values()]
    ):
        raise ValueError("Finite nonnegative amounts required")
    items = {k: usage[k] * rates[k] for k in usage}
    total = sum(items.values())
    if baseline is not None and (not math.isfinite(baseline) or baseline <= 0):
        raise ValueError("Positive comparison baseline required")
    return {
        "items": items,
        "estimatedTotal": total,
        "savingsFraction": None if baseline is None else 1 - total / baseline,
        "basis": "user-supplied usage and rates; not a provider invoice",
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--usage", required=True)
    p.add_argument("--rates", required=True)
    p.add_argument("--baseline", type=float)
    a = p.parse_args()
    print(
        json.dumps(
            estimate(json.load(open(a.usage)), json.load(open(a.rates)), a.baseline), indent=2
        )
    )
