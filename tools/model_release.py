"""Sign approved local model bundles; install/activate verifies pinned Ed25519 keys."""

import argparse
import base64
import hashlib
import json
import shutil
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from marine.edge_runtime import ModelVersions
from marine.storage import canonical


def package(source, destination, key_file):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError("Choose a new immutable release directory")
    files = {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest()
        for name in ModelVersions.ALLOWED
        if (source / name).is_file()
    }
    if not {"model.onnx", "report.json"} <= files.keys():
        raise ValueError("Missing model/report")
    manifest = {"version": files["model.onnx"], "files": files, "approved": True}
    key = Ed25519PrivateKey.from_private_bytes(Path(key_file).read_bytes())
    signature = key.sign(canonical(manifest))
    destination.mkdir(mode=0o700, parents=True)
    for name in files:
        shutil.copyfile(source / name, destination / name)
    (destination / "signed-manifest.json").write_bytes(
        canonical({"manifest": manifest, "signature": base64.b64encode(signature).decode()})
    )
    return {"version": manifest["version"], "files": len(files)}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["package", "install", "activate"])
    p.add_argument("--source", required=True)
    p.add_argument("--destination", required=True)
    p.add_argument("--key-file", required=True)
    p.add_argument("--approved", action="store_true")
    a = p.parse_args()
    if a.action == "package":
        if not a.approved:
            p.error("Packaging requires explicit --approved after evaluation")
        result = package(a.source, a.destination, a.key_file)
    else:
        versions = ModelVersions(a.destination, Path(a.key_file).read_bytes())
        result = (
            {"version": versions.install(a.source)}
            if a.action == "install"
            else versions.activate(a.source)
        )
    print(json.dumps(result))
