import math

import pytest

from marine.forecast import WeightedRegression, backtest


def labels():
    return [
        {
            "originAt": i * 7200.0,
            "labelAvailableAt": i * 7200.0 + 3600,
            "cpue": 5 + i * 0.05,
            "features": {
                "observationCount": i,
                "temperatureC": 20 + math.sin(i),
                "salinityPsu": 32,
                "turbidityNtu": 5,
                "hourSin": math.sin(i),
                "hourCos": math.cos(i),
            },
        }
        for i in range(150)
    ]


def test_future_labels_do_not_change_model():
    rows = labels()
    first = WeightedRegression().fit(rows, 400000)
    rows[-1]["cpue"] = 999999
    second = WeightedRegression().fit(rows, 400000)
    assert first.dump() == second.dump()


def test_backtest_reports_metrics_and_interval_order():
    rows = labels()
    report = backtest(rows)
    assert report["samples"] > 0
    assert 0 <= report["coverage95"] <= 1
    model = WeightedRegression().fit(rows, 2000000)
    prediction = model.predict(rows[-1]["features"])
    assert 0 <= prediction["lower"] <= prediction["point"] <= prediction["upper"]


def test_insufficient_mature_labels_fail_explicitly():
    with pytest.raises(ValueError):
        WeightedRegression().fit(labels(), 10)


def test_invalid_quality_label_is_excluded_entirely():
    rows = labels()
    expected = WeightedRegression().fit(rows[:-1], 2000000).dump()
    rows[-1]["quality"] = 0
    rows[-1]["cpue"] = 99999
    actual = WeightedRegression().fit(rows, 2000000).dump()
    assert actual == expected
