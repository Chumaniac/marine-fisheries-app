import json
from pathlib import Path
import cv2
import numpy as np
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from marine.api import create_app
from marine.edge_samples import CaptureJournal


def test_capture_restart_sequence_and_low_confidence_review_roundtrip(tmp_path):
    sample = json.loads(Path("contracts/examples.json").read_text())[0]
    sample.update(
        tenantId="demo", deviceId="edge-device", siteId="A", occurredAt="2026-01-01T00:00:00Z"
    )
    sample["data"]["confidence"] = 0.5
    _, encoded = cv2.imencode(".png", np.zeros((24, 24, 3), np.uint8))
    raw = encoded.tobytes()
    journal = CaptureJournal(tmp_path / "edge.db")
    first = journal.capture(sample, raw, review=True)
    second_sample = {**sample, "eventId": "2176e55d-6043-4ae6-93f0-bf7c7a6e8241"}
    second = CaptureJournal(tmp_path / "edge.db").capture(second_sample, raw)
    assert second["sequenceNo"] == first["sequenceNo"] + 1
    validator = Draft202012Validator(
        json.loads(Path("contracts/events.schema.json").read_text()), format_checker=FormatChecker()
    )
    assert not list(validator.iter_errors(first))

    class Classifier:
        def predict(self, *args, **kwargs):
            return {
                "speciesId": "saithe",
                "confidence": 0.5,
                "probabilities": {"saithe": 0.5, "salmonid": 0.5},
                "modelVersion": "synthetic",
                "_embedding": [1.0, 0.0],
            }

    app = create_app(tmp_path / "server")
    app.state.classifier = Classifier()
    with TestClient(app) as c:
        actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        data = {"event": json.dumps(first), "needs_review": "true"}
        result = c.post(
            "/v1/edge-samples", data=data, files={"image": ("crop.png", raw, "image/png")}
        )
        assert result.status_code == 200, result.text
        assert not app.state.store.events("demo")
        review = result.json()["reviewId"]
        label = {
            "requestId": "human-test-label",
            "label": "saithe",
            "groupId": "synthetic-group",
            "expectedRevision": 1,
        }
        assert c.post("/v1/reviews/" + review + "/label", json=label).status_code == 200
        assert len(app.state.store.events("demo")) == 1
        download = c.get("/v1/observations/" + first["eventId"] + "/evidence")
        assert download.content == raw
        assert download.headers["content-type"] == "image/png"
        assert "observation-image.png" in download.headers["content-disposition"]
        assert (
            c.post(
                "/v1/edge-samples", data=data, files={"image": ("crop.png", raw, "image/png")}
            ).status_code
            == 200
        )
        assert len(app.state.store.events("demo")) == 1

        status = c.get("/v1/edge-samples/" + first["eventId"]).json()
        assert status["status"] == "published"
        forged = {
            **first,
            "eventId": "9c052d95-1f0e-47a7-9f0d-b6ed40ba87f4",
            "deviceId": "another-device",
        }
        assert (
            c.post(
                "/v1/edge-samples",
                data={"event": json.dumps(forged)},
                files={"image": ("crop.png", raw, "image/png")},
            ).status_code
            == 409
        )
        assert (
            c.post(
                "/v1/reviews/" + review + "/label",
                json={
                    **label,
                    "requestId": "changed-label",
                    "expectedRevision": 2,
                    "label": "salmonid",
                },
            ).status_code
            == 409
        )
