"""Consistent local recovery bundles. Keys stay in a private 0700 directory."""

from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

from marine.storage import canonical


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def evidence_manifests(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = [json.loads(r[0]) for r in db.execute("SELECT manifest FROM evidence")]
    result += [json.loads(r[0])["manifest"] for r in db.execute("SELECT payload FROM reviews")]
    if "edge_evidence" in tables:
        result += [json.loads(r[0]) for r in db.execute("SELECT manifest FROM edge_evidence")]
    return result


def verify(folder):
    folder = Path(folder)
    if (folder / "INCOMPLETE").exists():
        raise ValueError("Incomplete backup rejected")
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest.get("version") != 1:
        raise ValueError("Unknown backup format")
    actual = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}
    if (
        actual != set(manifest["files"]) | {"manifest.json"}
        or "marine.sqlite3" not in manifest["files"]
    ):
        raise ValueError("Backup file inventory mismatch")
    for name, expected in manifest["files"].items():
        path = folder / name
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(folder.resolve())
            or file_hash(path) != expected
        ):
            raise ValueError("Backup digest/path mismatch")
    with closing(sqlite3.connect(f"file:{folder / 'marine.sqlite3'}?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity failure")
        manifests = evidence_manifests(db)
    if manifests:
        from marine.evidence_store import EvidenceStore

        if not (folder / "evidence.key").is_file():
            raise ValueError("Missing evidence key")
        objects = EvidenceStore(folder)
        for item in manifests:
            objects.verify(item)
    return {
        "status": "verified",
        "files": len(manifest["files"]),
        "evidenceObjects": len(manifests),
    }


def backup(source, destination, model=None, write_receipt=True):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError("Use a new directory outside the source")
    destination.mkdir(mode=0o700, parents=True)
    try:
        with closing(sqlite3.connect(f"file:{source / 'marine.sqlite3'}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination / "marine.sqlite3")) as output:
                original.backup(output)
        with closing(sqlite3.connect(destination / "marine.sqlite3")) as db, db:
            # Finish the transaction and close WAL handles before hashing the bundle.
            manifests = evidence_manifests(db)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ["browser_sessions", "login_attempts"]:
                if name in tables:
                    db.execute("DELETE FROM " + name)
        if (source / "evidence.key").exists():
            shutil.copyfile(source / "evidence.key", destination / "evidence.key")
        (destination / "objects").mkdir(mode=0o700)
        for manifest in manifests:
            name = manifest["cipherSha256"]
            if len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
                raise ValueError("Invalid object name")
            shutil.copyfile(source / "objects" / name, destination / "objects" / name)
        if model:
            (destination / "model").mkdir(mode=0o700)
            for name in [
                "model.onnx",
                "model-review.onnx",
                "report.json",
                "metadata.json",
                "calibration.json",
                "signed-manifest.json",
            ]:
                path = Path(model) / name
                if path.is_file():
                    shutil.copyfile(path, destination / "model" / name)
        files = {}
        for path in destination.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
                files[str(path.relative_to(destination))] = file_hash(path)
        (destination / "manifest.json").write_bytes(canonical({"version": 1, "files": files}))
        (destination / "manifest.json").chmod(0o600)
        result = verify(destination)
        receipt = os.getenv("MARINE_BACKUP_RECEIPT_FILE")
        if receipt and write_receipt:
            import time

            target = Path(receipt)
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(
                canonical(
                    {
                        "verified": True,
                        "verifiedAt": time.time(),
                        "manifestSha256": file_hash(destination / "manifest.json"),
                    }
                )
            )
            temporary.chmod(0o600)
            os.replace(temporary, target)
        return result
    except Exception:
        # Do not leave a partial backup looking complete; retained files aid diagnosis.
        (destination / "INCOMPLETE").touch(mode=0o600)
        raise


def restore(bundle, destination):
    bundle, destination = Path(bundle).resolve(), Path(destination).resolve()
    result = verify(bundle)
    if (bundle / "INCOMPLETE").exists() or destination.exists():
        raise ValueError("Restore requires verified bundle and a new destination")
    import tempfile

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=destination.parent))
    try:
        manifest = json.loads((bundle / "manifest.json").read_text())
        for name in manifest["files"]:
            target = staging / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(bundle / name, target)
            target.chmod(0o600)
        shutil.copyfile(bundle / "manifest.json", staging / "manifest.json")
        verify(staging)
        os.rename(staging, destination)
    except Exception:
        shutil.rmtree(staging)
        raise
    return result
