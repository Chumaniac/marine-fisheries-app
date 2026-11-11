"""Bounded camera/video motion proposals and tracking for a fixed-camera pilot.

This is a baseline motion proposal detector, not a trained general fish detector.
Its counts require scene-specific field validation. Source crops and model weights
are never overwritten by runtime acquisition.
"""

import base64
import hashlib
import json
import os
import shutil
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from marine.storage import canonical


class ModelVersions:
    ALLOWED = {"model.onnx", "model-review.onnx", "report.json", "calibration.json"}

    def __init__(self, folder, public_key):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.public_key = Ed25519PublicKey.from_public_bytes(public_key)

    def install(self, bundle):
        bundle = Path(bundle)
        signed = json.loads((bundle / "signed-manifest.json").read_text())
        manifest = signed["manifest"]
        self.public_key.verify(
            base64.b64decode(signed["signature"], validate=True), canonical(manifest)
        )
        version = manifest["version"]
        if len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
            raise ValueError("Invalid model version")
        files = manifest["files"]
        if (
            not {"model.onnx", "report.json"} <= files.keys()
            or not files.keys() <= self.ALLOWED
            or manifest.get("approved") is not True
        ):
            raise ValueError("Model release not approved")
        total = 0
        for name, expected in files.items():
            path = bundle / name
            if path.is_symlink():
                raise ValueError("Symlink rejected")
            total += path.stat().st_size
            if total > 128 * 1024 * 1024:
                raise ValueError("Model bundle exceeds 128 MiB")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Model hash mismatch")
        if files["model.onnx"] != version:
            raise ValueError("Version mismatch")
        dest = self.folder / version
        if not dest.exists():
            if len([p for p in self.folder.iterdir() if p.is_dir() and not p.is_symlink()]) >= 5:
                raise ValueError("Model retention full; archive explicitly before installing")
            staging = self.folder / (".staging-" + os.urandom(8).hex())
            staging.mkdir(mode=0o700)
            for name in files:
                shutil.copyfile(bundle / name, staging / name)
            (staging / "signed-manifest.json").write_bytes(canonical(signed))
            # Validate preprocessing and graph loading before activation.
            from marine.inference import Classifier

            Classifier(staging)
            os.rename(staging, dest)
        return version

    def activate(self, version):
        dest = self.folder / version
        if not dest.is_dir() or dest.is_symlink() or dest.parent.resolve() != self.folder.resolve():
            raise ValueError("Unknown installed model")
        # Revalidate hashes/signature on rollback too; installed files may have been damaged.
        self.install(dest)
        current = self.folder / "current"
        old = os.readlink(current) if current.is_symlink() else None
        temporary = self.folder / (".link-" + os.urandom(8).hex())
        temporary.symlink_to(version, target_is_directory=True)
        os.replace(temporary, current)
        return {"active": version, "previous": old}


class Tracker:
    def __init__(self, capacity=128, ttl=30):
        self.capacity, self.ttl = capacity, ttl
        self.tracks = {}
        self.sequence = 0
        self.last = -1

    def update(self, boxes, now):
        if now < self.last:
            raise ValueError("Frame clock moved backwards")
        self.last = now
        self.tracks = {k: v for k, v in self.tracks.items() if now - v["at"] <= self.ttl}
        assigned = set()
        result = []
        for box in boxes[: self.capacity]:
            x, y, w, h = box
            if w <= 0 or h <= 0:
                continue

            def overlap(track):
                a, b, c, d = track["box"]
                intersection = max(0, min(x + w, a + c) - max(x, a)) * max(
                    0, min(y + h, b + d) - max(y, b)
                )
                return intersection / max(1, w * h + c * d - intersection)

            candidates = [(overlap(v), k) for k, v in self.tracks.items() if k not in assigned]
            score, identity = max(candidates, default=(0, None))
            if score < 0.2:
                if len(self.tracks) >= self.capacity:
                    continue
                self.sequence += 1
                identity = str(self.sequence)
            self.tracks[identity] = {"box": box, "at": now}
            assigned.add(identity)
            result.append((identity, box))
        return result


def health(outbox):
    with outbox.connect() as db:
        count, blocked = db.execute(
            "SELECT count(*),coalesce(sum(status='action_required'),0) FROM pending"
        ).fetchone()
    disk = shutil.disk_usage(Path(outbox.path).parent)
    return {
        "queued": count,
        "capacity": outbox.capacity,
        "actionRequired": blocked,
        "freeBytes": disk.free,
        "acquisitionAllowed": count < outbox.capacity
        and not blocked
        and disk.free > 256 * 1024 * 1024,
        "powerWatts": None,
        "temperatureC": None,
        "hardwareTelemetry": "unavailable",
    }


def verify_release(directory, public_key):
    directory = Path(directory)
    signed = json.loads((directory / "signed-manifest.json").read_text())
    manifest = signed["manifest"]
    Ed25519PublicKey.from_public_bytes(public_key).verify(
        base64.b64decode(signed["signature"], validate=True), canonical(manifest)
    )
    files = manifest["files"]
    if (
        not {"model.onnx", "report.json"} <= files.keys()
        or not files.keys() <= ModelVersions.ALLOWED
        or manifest.get("approved") is not True
    ):
        raise ValueError("Unapproved model bundle")
    from marine.backup import file_hash

    for name, expected in files.items():
        if (directory / name).is_symlink() or file_hash(directory / name) != expected:
            raise ValueError("Model release hash mismatch")
    if files["model.onnx"] != manifest["version"]:
        raise ValueError("Model version mismatch")
    return manifest["version"]
