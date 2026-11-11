"""Weighted robust regression and forward-only evaluation with matured labels."""

import math

import numpy as np

FEATURES = ["observationCount", "temperatureC", "salinityPsu", "turbidityNtu", "hourSin", "hourCos"]


def vector(data):
    result = np.array([data[k] for k in FEATURES], dtype=float)
    if not np.isfinite(result).all():
        raise ValueError("Finite features are required")
    return result


class WeightedRegression:
    def fit(self, rows, now, ridge=1.0):
        eligible = [
            r
            for r in rows
            if r["labelAvailableAt"] <= now
            and r["originAt"] < r["labelAvailableAt"]
            and r.get("quality", 1) > 0
        ]
        eligible.sort(key=lambda r: r["originAt"])
        if len(eligible) < 30:
            raise ValueError("At least 30 matured labels are required")
        # Last 20% is temporal calibration; training labels must have matured before it starts.
        calibration = eligible[-max(10, len(eligible) // 5) :]
        training = [
            r
            for r in eligible[: -len(calibration)]
            if r["labelAvailableAt"] <= calibration[0]["originAt"]
        ]
        if len(training) < 15:
            raise ValueError("Insufficient purged training data")
        raw = np.stack([vector(r["features"]) for r in training])
        self.mean = raw.mean(0)
        self.scale = np.maximum(raw.std(0), 1e-6)
        x = np.column_stack([np.ones(len(raw)), (raw - self.mean) / self.scale])
        y = np.log1p([r["cpue"] for r in training])
        if not np.isfinite(y).all() or any(r["cpue"] < 0 for r in training):
            raise ValueError("Invalid CPUE labels")
        base = np.array(
            [
                min(1.0, max(0.05, r.get("quality", 1.0)))
                * math.exp(-(now - r["originAt"]) / (14 * 86400))
                for r in training
            ]
        )
        weights = base.copy()
        penalty = np.eye(x.shape[1]) * ridge
        penalty[0, 0] = 0
        for _ in range(12):
            self.beta = np.linalg.solve(x.T @ (weights[:, None] * x) + penalty, x.T @ (weights * y))
            residual = y - x @ self.beta
            scale = max(float(np.median(np.abs(residual)) / 0.6745), 1e-4)
            weights = base * np.minimum(1, 1.35 * scale / np.maximum(np.abs(residual), 1e-8))
        cx = np.stack([self.design(r["features"]) for r in calibration])
        errors = np.abs(np.log1p([r["cpue"] for r in calibration]) - cx @ self.beta)
        rank = min(len(errors), math.ceil((len(errors) + 1) * 0.95))
        self.radius = float(np.sort(errors)[rank - 1])
        self.training_count = len(training)
        self.calibration_count = len(calibration)
        self.origin = now
        return self

    def design(self, features):
        return np.r_[1.0, (vector(features) - self.mean) / self.scale]

    def predict(self, features):
        z = float(self.design(features) @ self.beta)
        if not math.isfinite(z) or z > 20:
            raise ValueError("Prediction out of supported range")
        return {
            "point": max(0.0, math.expm1(z)),
            "lower": max(0.0, math.expm1(z - self.radius)),
            "upper": max(0.0, math.expm1(min(20, z + self.radius))),
            "coverage": 0.95,
            "unit": "kg/standardizedHour",
            "pointStatistic": "log-space median",
            "intervalGuarantee": "empirical; temporal dependence may invalidate nominal coverage",
        }

    def dump(self):
        return {
            k: getattr(self, k).tolist()
            if isinstance(getattr(self, k), np.ndarray)
            else getattr(self, k)
            for k in [
                "mean",
                "scale",
                "beta",
                "radius",
                "training_count",
                "calibration_count",
                "origin",
            ]
        }

    @classmethod
    def load(cls, data):
        result = cls()
        for key, value in data.items():
            if key not in {
                "mean",
                "scale",
                "beta",
                "radius",
                "training_count",
                "calibration_count",
                "origin",
            }:
                raise ValueError("Invalid model field")
            setattr(result, key, np.array(value) if key in {"mean", "scale", "beta"} else value)
        return result


def backtest(rows):
    rows = sorted(
        [r for r in rows if r.get("quality", 1) > 0 and r["originAt"] < r["labelAvailableAt"]],
        key=lambda r: r["originAt"],
    )
    actual, predicted, lows, highs = [], [], [], []
    for index in range(50, len(rows), 10):
        origin = rows[index]["originAt"]
        past = [r for r in rows[:index] if r["labelAvailableAt"] <= origin]
        try:
            model = WeightedRegression().fit(past, origin)
        except ValueError:
            continue
        for row in rows[index : index + 10]:
            result = model.predict(row["features"])
            actual.append(row["cpue"])
            predicted.append(result["point"])
            lows.append(result["lower"])
            highs.append(result["upper"])
    if not actual:
        raise ValueError("Not enough chronological labels for backtest")
    y, p = np.array(actual), np.array(predicted)
    denominator = float(np.abs(y).sum())
    return {
        "samples": len(y),
        "mae": float(np.abs(y - p).mean()),
        "rmse": float(np.sqrt(np.mean((y - p) ** 2))),
        "wape": None if denominator == 0 else float(np.abs(y - p).sum() / denominator),
        "coverage95": float(np.mean((y >= np.array(lows)) & (y <= np.array(highs)))),
        "meanIntervalWidth": float(np.mean(np.array(highs) - np.array(lows))),
    }
