"""Build an immutable private local application; no weights, keys or runtime data."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "pyproject.toml",
    "requirements.local.lock",
    "LICENSE",
    "README.md",
    "Start Marine.command",
    "Prepare Marine.command",
    "tools/runtime_environment.py",
    "tools/start_app.py",
    "tools/export_reviews.py",
    "tools/package_local_release.py",
    "docs/local-distribution.md",
    "docs/collection-workorders.md",
    "third_party/NOTICE.md",
    "web/index.html",
    "web/style.css",
    "web/app.js",
    "web/app.js.LEGAL.txt",
    "web/sw.js",
}
DIRECTORIES = ("marine/", "contracts/", "web/fonts/")


def allowed(name):
    parts = name.split("/")
    return (
        not name.startswith("/")
        and "\\" not in name
        and not any(
            p in {"", ".", "..", "node_modules", "__pycache__"} or p.startswith(".") for p in parts
        )
        and (name in FILES or name.startswith(DIRECTORIES))
    )


def assemble(files, version, revision, output):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("RELEASE_ID_INVALID")
    if not 1 <= len(files) <= 2000 or any(
        not allowed(n) or len(b) > 10_000_000 for n, b in files.items()
    ):
        raise ValueError("RELEASE_PAYLOAD_INVALID")
    if sum(map(len, files.values())) > 32 * 1024 * 1024:
        raise ValueError("RELEASE_CAPACITY")
    payload = dict(files)
    # Installed help is the complete local usage guide, with no checkout-only links.
    if "docs/local-distribution.md" in files:
        payload["README.md"] = files["docs/local-distribution.md"].replace(
            b"(collection-workorders.md)", b"(docs/collection-workorders.md)"
        )
    manifest = {
        "version": version,
        "source_sha": revision,
        "target": "local",
        "data_features": ["collection-orders/v1"],
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in sorted(payload.items())},
    }
    payload["distribution.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"marine-fisheries-v{version}-local.zip"
    with tempfile.TemporaryDirectory(prefix=".release-", dir=output) as temporary:
        staged = Path(temporary) / archive.name
        with zipfile.ZipFile(
            staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as bundle:
            for name, data in sorted(payload.items()):
                entry = zipfile.ZipInfo(
                    f"marine-fisheries-{version}/" + name, (2020, 1, 1, 0, 0, 0)
                )
                entry.external_attr = (
                    0o100755 if name in {"Start Marine.command", "Prepare Marine.command"} else 0o100644
                ) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(entry, data)
        os.link(staged, archive)
    return archive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch("[0-9a-f]{40}", args.revision):
        raise SystemExit("RELEASE_SOURCE_INVALID")
    listing = subprocess.check_output(["git", "ls-tree", "-r", args.revision], cwd=ROOT, text=True)
    files = {}
    for line in listing.splitlines():
        meta, name = line.split("\t", 1)
        if not allowed(name):
            continue
        if meta.split()[0] not in {"100644", "100755"}:
            raise SystemExit("RELEASE_LINK_INVALID")
        files[name] = subprocess.check_output(["git", "show", f"{args.revision}:{name}"], cwd=ROOT)
    for required in [
        "pyproject.toml",
        "requirements.local.lock",
        "marine/api.py",
        "contracts/events.schema.json",
        "web/app.js",
        "Start Marine.command",
        "Prepare Marine.command",
        "tools/runtime_environment.py",
        "tools/start_app.py",
        "tools/export_reviews.py",
        "docs/local-distribution.md",
        "docs/collection-workorders.md",
    ]:
        if required not in files:
            raise SystemExit("RELEASE_SOURCE_INCOMPLETE")
    version = tomllib.loads(files["pyproject.toml"].decode())["project"]["version"]
    print(assemble(files, version, args.revision, args.output))


if __name__ == "__main__":
    main()
