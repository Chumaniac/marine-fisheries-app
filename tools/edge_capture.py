"""Capture a local video/camera into the durable outbox. Explicitly baseline motion detection."""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import cv2
from marine.edge_samples import CaptureJournal
from marine.edge_runtime import Tracker, health
from marine.inference import Classifier


def run(a):
    if a.clock_uncertainty_ms > 5000:
        raise ValueError("Synchronize device clock before capture")
    if a.frames < 1 or a.frames > 100000:
        raise ValueError("Frame budget must be 1..100000")
    model = Classifier(a.model)
    journal = CaptureJournal(a.outbox)
    outbox = journal.outbox
    last_health = 0
    health_snapshot = {}
    tracker = Tracker()
    capture = cv2.VideoCapture(int(a.source) if a.source.isdigit() else a.source)
    detector = cv2.createBackgroundSubtractorMOG2(history=100, varThreshold=25, detectShadows=False)
    session = str(uuid4())
    observations = 0
    try:
        if not capture.isOpened():
            raise ValueError("Source unavailable")
        for frame_no in range(a.frames):
            if time.monotonic() - last_health >= 1:
                health_snapshot = health(outbox)
                last_health = time.monotonic()
            if not health_snapshot.get("acquisitionAllowed", False):
                break
            ok, frame = capture.read()
            if not ok:
                break
            h, w = frame.shape[:2]
            if h * w > 16_000_000:
                raise ValueError("Frame dimensions too large")
            scaled = cv2.resize(frame, (min(960, w), max(1, int(h * min(960, w) / w))))
            mask = detector.apply(scaled)
            if frame_no < 30:
                continue
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            boxes = [cv2.boundingRect(c) for c in contours if cv2.contourArea(c) > 400]
            boxes = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)[:128]
            now = time.time()
            for track, (x, y, bw, bh) in tracker.update(boxes, time.monotonic()):
                if bw < 16 or bh < 16:
                    continue
                ok, encoded = cv2.imencode(".jpg", scaled[y : y + bh, x : x + bw])
                if not ok:
                    continue
                result = model.predict(encoded.tobytes())
                at = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
                event = {
                    "schemaVersion": "1.0",
                    "eventId": str(uuid4()),
                    "eventType": "fish.observation",
                    "tenantId": a.tenant,
                    "siteId": a.site,
                    "deviceId": a.device,
                    "sequenceNo": observations,
                    "occurredAt": at,
                    "receivedAt": at,
                    "clockUncertaintyMs": a.clock_uncertainty_ms,
                    "traceId": session,
                    "data": {
                        "trackId": session + "-" + track,
                        "speciesId": result["speciesId"],
                        "confidence": result["confidence"],
                        "countMethod": "track",
                        "qualityScore": 1,
                        "exposureSeconds": 1,
                        "modelVersion": result["modelVersion"],
                        "preprocessVersion": result["preprocessVersion"],
                        "evidenceId": "unretained-" + session,
                    },
                }
                journal.capture(event, encoded.tobytes(), review=result["confidence"] < 0.8)
                observations += 1
            if a.endpoint:
                journal.flush(
                    a.endpoint.removesuffix("/v1/event-batches"),
                    Path(a.token_file).read_text().strip(),
                )
        return {
            "observationsPersisted": observations,
            "health": health(outbox),
            "detector": "MOG2 fixed-camera baseline, field accuracy unvalidated",
        }
    finally:
        capture.release()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--outbox", required=True)
    for name in ["tenant", "site", "device"]:
        p.add_argument("--" + name, required=True)
    p.add_argument("--frames", type=int, default=300)
    p.add_argument("--clock-uncertainty-ms", type=int, required=True)
    p.add_argument("--endpoint")
    p.add_argument("--token-file")
    a = p.parse_args()
    if a.endpoint and not a.token_file:
        p.error("--token-file required for upload")
    print(json.dumps(run(a)))
