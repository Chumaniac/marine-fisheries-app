import io
import json

from fastapi.testclient import TestClient
from PIL import Image

from marine.api import create_app


def test_review_dedup_blind_queue_and_explicit_label(tmp_path):
    app = create_app(tmp_path)
    image = io.BytesIO()
    Image.new("RGB", (20, 20), "blue").save(image, format="PNG")
    prediction = {
        "probabilities": {"saithe": 0.5, "salmonid": 0.5},
        "confidence": 0.5,
        "modelVersion": "synthetic-test",
        "_embedding": [1.0, 0.0],
    }
    first = app.state.reviews.add("demo", image.getvalue(), dict(prediction))
    second = app.state.reviews.add("demo", image.getvalue(), dict(prediction))
    assert first["reviewId"] == second["reviewId"]
    with TestClient(app) as client:
        client.post("/v1/session", headers={"X-Requested-With": "marine-web"})
        client.headers["X-Marine-Scope"] = client.get("/v1/me").json()["scopeHash"]
        queue = client.get("/v1/reviews").json()
        assert queue["queued"] == 1
        assert "prediction" not in queue["items"][0]
        identity = first["reviewId"]
        assert client.get(f"/v1/reviews/{identity}/image").status_code == 200
        payload = {
            "requestId": "synthetic-label-test",
            "label": "saithe",
            "groupId": "synthetic-test-group",
            "expectedRevision": 1,
        }
        first = client.post(f"/v1/reviews/{identity}/label", json=payload)
        assert first.status_code == 200
        assert client.post(f"/v1/reviews/{identity}/label", json=payload).json() == first.json()
        assert client.get("/v1/reviews").json()["queued"] == 0
        assert client.get("/v1/review-training-export?check=true").json()["samples"] == 1
        import zipfile
        import csv

        exported = client.get("/v1/review-training-export")
        assert exported.status_code == 200
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            rows = list(csv.DictReader(io.StringIO(archive.read("manifest.csv").decode())))
            assert (
                len(rows) == 1
                and rows[0]["split"] == "train"
                and rows[0]["group"] == "synthetic-test-group"
            )
            assert archive.read(rows[0]["path"]) == image.getvalue()
    with app.state.store.connect() as db:
        record = json.loads(db.execute("SELECT payload FROM reviews").fetchone()[0])
        assert record["label"] == "saithe" and len(record["history"]) == 1
