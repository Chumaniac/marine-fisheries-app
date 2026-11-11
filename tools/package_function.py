"""Build an offline Lambda artifact from installed, pinned pure-Python dependencies."""

import argparse
import importlib.metadata as metadata
import json
import zipfile
from pathlib import Path
from packaging.requirements import Requirement
from marine.backup import file_hash


def package(destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Use a new artifact path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    packages = {}
    pending = ["httpx"]
    files = {}
    while pending:
        dist = metadata.distribution(pending.pop())
        name = dist.metadata["Name"].lower()
        if name in packages:
            continue
        packages[name] = dist.version
        for entry in dist.files or []:
            if ".." in Path(str(entry)).parts or str(entry).endswith(".pyc"):
                continue
            path = Path(dist.locate_file(entry))
            if path.is_file():
                if path.suffix in {".so", ".dylib", ".pyd"}:
                    raise ValueError("Native extension requires a target-platform build")
                files[str(entry)] = path
        for raw in dist.requires or []:
            req = Requirement(raw)
            if not req.marker or req.marker.evaluate({"extra": ""}):
                pending.append(req.name)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write("deploy/serverless/handler.py", "handler.py")
        for name, path in sorted(files.items()):
            archive.write(path, name)
        archive.writestr(
            "build-manifest.json", json.dumps({"dependencies": packages}, sort_keys=True)
        )
    return {
        "artifact": destination.name,
        "sha256": file_hash(destination),
        "bytes": destination.stat().st_size,
        "dependencies": packages,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True)
    a = p.parse_args()
    print(json.dumps(package(a.output), indent=2))
