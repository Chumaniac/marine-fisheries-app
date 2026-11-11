"""Bounded ONNX inference; unavailable model is an explicit error, never a fake label."""

import hashlib
import json
import threading
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from marine.vision import VERSION, enhance

ort.disable_telemetry_events()
MAX_CLASSES = 256
MAX_EMBEDDING_DIMENSIONS = 4096


def checked_outputs(outputs, class_count):
    if not isinstance(outputs, (list, tuple)) or not 1 <= len(outputs) <= 2:
        raise ValueError("Invalid model output contract")
    for index, output in enumerate(outputs):
        maximum = MAX_CLASSES if index == 0 else MAX_EMBEDDING_DIMENSIONS
        if (
            not isinstance(output, np.ndarray)
            or not np.issubdtype(output.dtype, np.floating)
            or output.dtype.itemsize > 8
            or output.ndim != 2
            or output.shape[0] != 1
            or not 1 <= output.shape[1] <= maximum
            or not np.isfinite(output).all()
        ):
            raise ValueError("Invalid model output contract")
    if outputs[0].shape[1] != class_count:
        raise ValueError("Invalid model output class count")
    return outputs


class Classifier:
    def __init__(self, directory):
        directory = Path(directory)
        report = json.loads((directory / "report.json").read_text())
        if report["preprocessVersion"] != VERSION:
            raise ValueError("Incompatible preprocessing version")
        self.labels = report["labels"]
        if (
            not isinstance(self.labels, list)
            or not 2 <= len(self.labels) <= MAX_CLASSES
            or any(
                not isinstance(label, str) or not 1 <= len(label) <= 128 for label in self.labels
            )
            or len(set(self.labels)) != len(self.labels)
        ):
            raise ValueError("Invalid model labels")
        self.mode = report["preprocessing"]
        with (directory / "model.onnx").open("rb") as stream:
            base_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        self.version = base_hash[:16]
        self.temperature = 1.0
        self.calibrated = False
        if (directory / "calibration.json").is_file():
            calibration = json.loads((directory / "calibration.json").read_text())
            if (
                calibration["modelVersion"] != self.version
                or not 0.2 <= calibration["temperature"] <= 5
            ):
                raise ValueError("Invalid calibration artifact")
            if calibration.get("promotion") == "approved":
                self.temperature = calibration["temperature"]
                self.calibrated = True
        model_path = directory / (
            "model-review.onnx" if (directory / "model-review.onnx").exists() else "model.onnx"
        )
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        self.session = ort.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        if (
            model_path.name == "model-review.onnx"
            and self.session.get_modelmeta().custom_metadata_map.get("baseSha256") != base_hash
        ):
            raise ValueError("Review graph does not match classifier")
        self.lock = threading.BoundedSemaphore(2)

    def predict(self, image_bytes, include_embedding=False, include_logits=False):
        if len(image_bytes) > 8 * 1024 * 1024:
            raise ValueError("Image exceeds 8 MiB")
        # Header inspection precedes decode to reject decompression bombs.
        from PIL import Image
        import io

        try:
            with Image.open(io.BytesIO(image_bytes)) as header:
                if (
                    header.width * header.height > 16_000_000
                    or header.width < 16
                    or header.height < 16
                ):
                    raise ValueError("Invalid image dimensions")
                if header.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Unsupported image format")
        except (OSError, Image.DecompressionBombError) as error:
            raise ValueError("Invalid image") from error
        with self.lock:
            image = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("Invalid image")
            pixels_hash = hashlib.sha256(memoryview(image)).hexdigest()
            rgb = enhance(cv2.cvtColor(image, cv2.COLOR_BGR2RGB), self.mode)
            tensor = rgb.transpose(2, 0, 1).astype(np.float32) / 255
            tensor = (
                tensor - np.array([0.485, 0.456, 0.406], np.float32)[:, None, None]
            ) / np.array([0.229, 0.224, 0.225], np.float32)[:, None, None]
            outputs = checked_outputs(
                self.session.run(None, {"image": tensor[None]}), len(self.labels)
            )
            logits = outputs[0][0]
            probabilities = np.exp((logits - logits.max()) / self.temperature)
            probabilities /= probabilities.sum()
        choice = int(probabilities.argmax())
        result = {
            "speciesId": self.labels[choice],
            "confidence": float(probabilities[choice]),
            "probabilities": dict(zip(self.labels, map(float, probabilities))),
            "modelVersion": self.version,
            "preprocessVersion": VERSION,
            "needsReview": bool(probabilities[choice] < 0.8),
            "calibration": "validation-temperature" if self.calibrated else "uncalibrated",
            "scope": "NorFisk two-label public-data classifier; not general fish identification",
        }

        if include_logits:
            result["_logits"] = logits.tolist()
        if include_embedding and len(outputs) > 1:
            result["_embedding"] = outputs[1][0].tolist()
            result["_pixelsSha256"] = pixels_hash
        return result
