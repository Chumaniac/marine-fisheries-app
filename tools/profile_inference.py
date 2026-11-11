"""No-model preprocessing/output baseline; never represents device or field inference."""

import gc
import io
import json
import math
import os
import resource
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import numpy as np
from PIL import Image
from marine.inference import Classifier


def rss():
    return (
        int(
            subprocess.check_output(["ps", "-o", "rss=", "-p", str(os.getpid())], text=True).strip()
        )
        * 1024
    )


def main():
    with Image.new("RGB", (1280, 720), "gray") as image:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
    classifier = Classifier.__new__(Classifier)
    classifier.labels = ["synthetic-a", "synthetic-b"]
    classifier.mode = "clahe"
    classifier.version = "synthetic-no-model"
    classifier.temperature = 1.0
    classifier.calibrated = False
    classifier.lock = threading.BoundedSemaphore(2)
    classifier.session = SimpleNamespace(
        run=lambda *args: [np.array([[1, 0]], np.float32), np.zeros((1, 1280), np.float32)]
    )

    def run():
        start = time.perf_counter()
        result = classifier.predict(data, include_embedding=True)
        assert len(result["_embedding"]) == 1280
        return (time.perf_counter() - start) * 1000

    gc.collect()
    before = rss()
    cold = run()
    warm = [run() for _ in range(20)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        parallel = list(pool.map(lambda _: run(), range(10)))
    after = rss()
    classifier = None
    gc.collect()
    released = rss()

    def percentile(rows):
        return sorted(rows)[math.ceil(len(rows) * 0.95) - 1]

    print(
        json.dumps(
            {
                "scope": "1280x720 synthetic PNG, clahe preprocessing + bounded output serialization, fake session, no weights/model loaded",
                "coldMs": cold,
                "warmSamples": 20,
                "warmP95Ms": percentile(warm),
                "concurrentSamples": 10,
                "concurrency": 2,
                "concurrentP95Ms": percentile(parallel),
                "rssBeforeBytes": before,
                "rssAfterBytes": after,
                "rssAfterReleaseBytes": released,
                "processPeakRssBytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * (1 if sys.platform == "darwin" else 1024),
                "fieldAccepted": False,
                "modelAccuracyValidated": False,
                "hardwarePowerOrTemperatureMeasured": False,
                "limits": [
                    "Whole-process RSS snapshots include native allocator/OpenCV; no CPU before/after comparison",
                    "No real engine, field/device thermal/power or target-environment p95 conclusion",
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
