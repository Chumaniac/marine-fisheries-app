import pytest
from marine.edge_runtime import Tracker, health
from marine.edge import Outbox
from marine.monitoring import psi


def test_tracker_bounded_clock_and_expiry(tmp_path):
    tracker = Tracker(2, 5)
    first = tracker.update([(0, 0, 10, 10), (100, 100, 10, 10), (200, 200, 10, 10)], 1)
    assert len(first) == 2
    assert tracker.update([(1, 1, 10, 10)], 2)[0][0] == first[0][0]
    with pytest.raises(ValueError):
        tracker.update([], 0)
    assert tracker.update([], 8) == [] and not tracker.tracks
    box = Outbox(tmp_path / "edge.db", capacity=1)
    box.put({"eventId": "a"})
    assert not health(box)["acquisitionAllowed"]
    with pytest.raises(OverflowError):
        box.put({"eventId": "b"})
    assert psi([10, 10], [10, 10]) == 0
    assert psi([100, 1], [1, 100]) > 0.2
