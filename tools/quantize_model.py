"""Static INT8 candidate calibrated on training images, evaluated on validation only."""

import csv
import json
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from onnxruntime.quantization import CalibrationDataReader, QuantFormat, QuantType, quantize_static
from sklearn.metrics import f1_score

from marine.learning import Images


ROOT = Path(__file__).resolve().parents[1]
folder = ROOT / "artifacts/models/norfisk"
report = json.loads((folder / "report.json").read_text())
rows = list(csv.DictReader((ROOT / "data/processed/norfisk/manifest.csv").open()))
train = [r for r in rows if r["split"] == "train"]
np.random.default_rng(42).shuffle(train)


class Reader(CalibrationDataReader):
    def __init__(self):
        self.data = Images(
            train[:300], ROOT / "data/processed/norfisk", report["labels"], report["preprocessing"]
        )
        self.index = 0

    def get_next(self):
        if self.index >= len(self.data):
            return None
        value, _ = self.data[self.index]
        self.index += 1
        return {"image": value.numpy()[None]}


candidate = folder / "model-int8-perchannel.onnx"
if not candidate.exists():
    quantize_static(
        str(folder / "model.onnx"),
        str(candidate),
        Reader(),
        quant_format=QuantFormat.QDQ,
        per_channel=True,
        activation_type=QuantType.QUInt8,
        weight_type=QuantType.QInt8,
        op_types_to_quantize=["Conv", "Gemm"],
    )
validation = Images(
    [r for r in rows if r["split"] == "validation"],
    ROOT / "data/processed/norfisk",
    report["labels"],
    report["preprocessing"],
)
options = ort.SessionOptions()
options.intra_op_num_threads = 2
results = {}
for name in ["model.onnx", "model-int8-perchannel.onnx"]:
    session = ort.InferenceSession(str(folder / name), options, providers=["CPUExecutionProvider"])
    actual = []
    predictions = []
    latency = []
    for i in range(len(validation)):
        image, label = validation[i]
        started = time.perf_counter()
        output = session.run(None, {"image": image.numpy()[None]})[0]
        if i > 2:
            latency.append((time.perf_counter() - started) * 1000)
        actual.append(label)
        predictions.append(int(output.argmax()))
    results[name] = {
        "validationMacroF1": float(f1_score(actual, predictions, average="macro")),
        "bytes": (folder / name).stat().st_size,
        "warmInferenceP95Ms": float(np.percentile(latency, 95)),
        "validationSamples": len(validation),
    }
results["calibrationTrainingImages"] = 300
results["promote"] = (
    results["model-int8-perchannel.onnx"]["validationMacroF1"]
    >= results["model.onnx"]["validationMacroF1"] - 0.01
    and results["model-int8-perchannel.onnx"]["warmInferenceP95Ms"]
    < results["model.onnx"]["warmInferenceP95Ms"]
)
results["scope"] = (
    "CPU-only local validation; no underwater edge hardware or energy benchmark; primary artifact preserved"
)
(folder / "quantization-perchannel-report.json").write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
