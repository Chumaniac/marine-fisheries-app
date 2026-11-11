"""Expose B0's existing penultimate tensor without retraining or changing logits."""

import argparse
import hashlib
from pathlib import Path

import onnx
from onnx import TensorProto, helper


def export(folder):
    folder = Path(folder)
    source = folder / "model.onnx"
    model = onnx.load(source)
    gemm_inputs = {node.input[0] for node in model.graph.node if node.op_type == "Gemm"}
    tensors = [
        node.output[0]
        for node in model.graph.node
        if node.op_type == "Flatten" and node.output[0] in gemm_inputs
    ]
    if len(tensors) != 1:
        raise ValueError("Model graph is not the supported B0 classifier layout")
    model.graph.output.append(
        helper.make_tensor_value_info(tensors[0], TensorProto.FLOAT, [None, 1280])
    )
    helper.set_model_props(model, {"baseSha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    onnx.checker.check_model(model)
    onnx.save(model, folder / "model-review.onnx")
    print("Exported classifier logits and 1280-dimensional review embeddings")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", nargs="?", default="artifacts/models/norfisk")
    export(parser.parse_args().folder)
