import numpy as np
import pytest
import torch

from marine.learning import focal
from marine.inference import Classifier
from marine.vision import enhance, uncertainty_diversity


@pytest.mark.parametrize("mode", ["original", "clahe", "dehaze"])
def test_constant_dark_image_is_finite(mode):
    output = enhance(np.zeros((30, 80, 3), dtype=np.uint8), mode)
    assert output.shape == (224, 224, 3)
    assert np.isfinite(output).all()


def test_gamma_zero_matches_cross_entropy():
    logits = torch.tensor([[2.0, -1.0], [-1.0, 3.0]], requires_grad=True)
    labels = torch.tensor([0, 1])
    loss = focal(logits, labels, torch.ones(2), 0)
    assert torch.allclose(loss, torch.nn.functional.cross_entropy(logits, labels))
    loss.backward()
    assert torch.isfinite(logits.grad).all()


def test_invalid_image_is_rejected_before_running_a_model():
    classifier = Classifier.__new__(Classifier)
    with pytest.raises(ValueError, match="Invalid image"):
        classifier.predict(b"not an image")


def test_active_learning_is_unique_bounded_and_deterministic():
    p = np.array([[0.5, 0.5], [0.6, 0.4], [0.9, 0.1], [0.2, 0.8], [0.95, 0.05]])
    e = np.eye(5)
    result = uncertainty_diversity(p, e, 4)
    assert len(set(result)) == 4
    assert result == uncertainty_diversity(p, e, 4)
    assert result[0] == 0
    assert uncertainty_diversity(p, e, 0) == []


def synthetic_classifier(outputs):
    import threading
    from types import SimpleNamespace

    classifier = Classifier.__new__(Classifier)
    classifier.labels = ["synthetic-a", "synthetic-b"]
    classifier.mode = "original"
    classifier.version = "synthetic"
    classifier.temperature = 1.0
    classifier.calibrated = False
    classifier.lock = threading.BoundedSemaphore(2)
    classifier.session = SimpleNamespace(run=lambda *args: outputs)
    return classifier


def synthetic_image():
    import io
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), "white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "outputs",
    [
        [np.array([[float("nan"), 0]], np.float32)],
        [np.zeros((2, 2), np.float32)],
        [np.array([[1, 0]], np.float32), np.zeros((1, 4097), np.float32)],
        [np.array([[1, 0]], np.float32), np.zeros((1, 0), np.float32)],
        [np.array([[1, 0]], np.float32), np.array([["SYNTHETIC_PRIVATE"]], dtype=object)],
    ],
)
def test_inference_rejects_invalid_outputs_before_serializing(outputs):
    with pytest.raises(ValueError, match="Invalid model output"):
        synthetic_classifier(outputs).predict(synthetic_image(), include_embedding=True)


def test_exact_embedding_limit_is_finite_and_serializable():
    import json

    result = synthetic_classifier(
        [np.array([[1, 0]], np.float32), np.zeros((1, 4096), np.float32)]
    ).predict(synthetic_image(), include_embedding=True, include_logits=True)
    assert len(result["_embedding"]) == 4096 and result["speciesId"] == "synthetic-a"
    json.dumps(result, allow_nan=False)
