"""Cold system snapshots and isolated restores; no overwrite or automatic promotion.

Application, edge journals, Kafka/Flink/IPFS/Fabric volumes and MSP/config folders
are explicit resources. All writers must be fenced before snapshot or promotion.
"""

import fcntl
import hashlib
import hmac
import json
import re
import subprocess
import tarfile
from pathlib import Path

from marine.backup import file_hash
from marine.storage import canonical

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError("Docker recovery operation failed; resources retained for inspection")
    return result.stdout


def fenced(config):
    for name in config.get("containers", []):
        if not NAME.fullmatch(name):
            raise ValueError("Invalid container name")
        state = json.loads(docker("inspect", name))[0]["State"]
        if state["Running"]:
            raise ValueError("Writer is still running: " + name)
    for name in config.get("locks", []):
        with open(name, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("Native writer must be stopped before recovery") from None


def inventory(archive):
    result = {}
    size = 0
    count = 0
    with tarfile.open(archive, "r:") as tar:
        for member in tar:
            count += 1
            path = Path(member.name)
            if (
                count > 200000
                or path.is_absolute()
                or ".." in path.parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Unsafe archive entry")
            if not member.isfile():
                continue
            size += member.size
            if size > 1024**4:
                raise ValueError("Archive exceeds 1 TiB limit")
            key = str(path)
            if key in result:
                raise ValueError("Duplicate archive path")
            stream = tar.extractfile(member)
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
            result[key] = digest.hexdigest()
    return result


def signing_key(path):
    key = Path(path).read_bytes()
    if len(key) != 32:
        raise ValueError("Recovery integrity key must be 32 bytes")
    if Path(path).stat().st_mode & 0o077:
        raise ValueError("Recovery key permissions must be 0600")
    return key


def snapshot(config, destination, key_file):
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("Snapshot destination must be new")
    key = signing_key(key_file)
    fenced(config)
    resources = config["resources"]
    if not resources or len(resources) > 32:
        raise ValueError("Explicit bounded resource inventory required")
    destination.mkdir(mode=0o700, parents=True)
    try:
        manifest = {
            "version": 1,
            "codeRevision": config.get("codeRevision"),
            "resources": [],
            "consistency": "all-declared-writers-fenced",
        }
        names = set()
        for resource in resources:
            name = resource["name"]
            if not NAME.fullmatch(name) or name in names:
                raise ValueError("Unique resource names required")
            names.add(name)
            archive = destination / (name + ".tar")
            if resource["kind"] == "directory":
                source = Path(resource["source"]).resolve()
                if not source.is_dir() or destination.is_relative_to(source):
                    raise ValueError("Invalid source/destination boundary")
                with tarfile.open(archive, "w") as tar:
                    tar.add(source, arcname=".", recursive=True)
            elif resource["kind"] == "sqlite":
                import sqlite3
                import tempfile

                source = Path(resource["source"]).resolve()
                if not source.is_file():
                    raise ValueError("SQLite source missing")
                with tempfile.TemporaryDirectory() as scratch:
                    temporary = Path(scratch) / "database.sqlite3"
                    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as original:
                        with sqlite3.connect(temporary) as output:
                            original.backup(output)
                    with tarfile.open(archive, "w") as tar:
                        tar.add(temporary, arcname="database.sqlite3")
            elif resource["kind"] == "docker_volume":
                volume = resource["source"]
                if not NAME.fullmatch(volume):
                    raise ValueError("Invalid volume name")
                docker("volume", "inspect", volume)
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "-v",
                    volume + ":/source:ro",
                    "-v",
                    str(destination) + ":/backup",
                    "alpine:3.22",
                    "tar",
                    "-C",
                    "/source",
                    "-cf",
                    "/backup/" + archive.name,
                    ".",
                )
            else:
                raise ValueError("Unsupported recovery resource")
            archive.chmod(0o600)
            files = inventory(archive)
            manifest["resources"].append(
                {
                    "name": name,
                    "kind": resource["kind"],
                    "archive": archive.name,
                    "sha256": file_hash(archive),
                    "files": files,
                }
            )
        envelope = {
            "manifest": manifest,
            "hmacSha256": hmac.new(key, canonical(manifest), hashlib.sha256).hexdigest(),
        }
        (destination / "system-manifest.json").write_bytes(canonical(envelope))
        (destination / "system-manifest.json").chmod(0o600)
        verify(destination, key_file)
        return {"phase": "SNAPSHOT_VERIFIED", "resources": len(resources)}
    except Exception:
        (destination / "INCOMPLETE").touch(mode=0o600)
        raise


def verify(bundle, key_file):
    bundle = Path(bundle)
    if (bundle / "INCOMPLETE").exists():
        raise ValueError("Incomplete system snapshot")
    envelope = json.loads((bundle / "system-manifest.json").read_text())
    manifest = envelope["manifest"]
    if not hmac.compare_digest(
        envelope["hmacSha256"],
        hmac.new(signing_key(key_file), canonical(manifest), hashlib.sha256).hexdigest(),
    ):
        raise ValueError("System manifest signature mismatch")
    if manifest.get("version") != 1:
        raise ValueError("Unknown recovery version")
    for resource in manifest["resources"]:
        name = resource["name"]
        if not NAME.fullmatch(name) or resource["archive"] != name + ".tar":
            raise ValueError("Invalid recovery path")
        path = bundle / resource["archive"]
        if (
            path.is_symlink()
            or file_hash(path) != resource["sha256"]
            or inventory(path) != resource["files"]
        ):
            raise ValueError("System snapshot content mismatch")
    return manifest


def restore(bundle, destination, key_file, prefix):
    bundle = Path(bundle).resolve()
    destination = Path(destination).resolve()
    manifest = verify(bundle, key_file)
    if destination.exists() or not NAME.fullmatch(prefix) or not prefix.startswith("marine-dr-"):
        raise ValueError("Use a new destination and marine-dr- volume prefix")
    destination.mkdir(mode=0o700, parents=True)
    targets = []
    try:
        for resource in manifest["resources"]:
            archive = bundle / resource["archive"]
            name = resource["name"]
            if resource["kind"] in {"directory", "sqlite"}:
                target = destination / name
                target.mkdir(mode=0o700)
                with tarfile.open(archive) as tar:
                    tar.extractall(target, filter="data")
                files = {
                    str(p.relative_to(target)): file_hash(p)
                    for p in target.rglob("*")
                    if p.is_file()
                }
                if files != resource["files"]:
                    raise ValueError("Restored directory mismatch")
                if name == "application" and (target / "marine.sqlite3").is_file():
                    import sqlite3

                    with sqlite3.connect(target / "marine.sqlite3") as db:
                        tables = {
                            r[0]
                            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                        }
                        for table in ["browser_sessions", "login_attempts"]:
                            if table in tables:
                                db.execute("DELETE FROM " + table)
                target_name = str(target)
            else:
                target_name = prefix + "-" + name
                existing = subprocess.run(
                    ["docker", "volume", "inspect", target_name], capture_output=True
                )
                if existing.returncode == 0:
                    raise ValueError("Restore volume already exists")
                docker("volume", "create", "--label", "marine.recovery=true", target_name)
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "-v",
                    target_name + ":/target",
                    "-v",
                    str(bundle) + ":/backup:ro",
                    "alpine:3.22",
                    "tar",
                    "-C",
                    "/target",
                    "-xf",
                    "/backup/" + archive.name,
                )
                check = destination / (name + "-check.tar")
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--read-only",
                    "-v",
                    target_name + ":/source:ro",
                    "-v",
                    str(destination) + ":/backup",
                    "alpine:3.22",
                    "tar",
                    "-C",
                    "/source",
                    "-cf",
                    "/backup/" + check.name,
                    ".",
                )
                if inventory(check) != resource["files"]:
                    raise ValueError("Restored volume mismatch")
                check.unlink()
            targets.append(
                {
                    "name": name,
                    "kind": resource["kind"],
                    "target": target_name,
                    "integrityVerified": True,
                }
            )
        report = {
            "phase": "RESTORED_ISOLATED",
            "snapshotDigest": file_hash(bundle / "system-manifest.json"),
            "codeRevision": manifest["codeRevision"],
            "targets": targets,
            "productionPromoted": False,
        }
        (destination / "recovery-state.json").write_bytes(canonical(report))
        (destination / "recovery-state.json").chmod(0o600)
        return report
    except Exception:
        (destination / "INCOMPLETE").touch(mode=0o600)
        raise


def promotion_check(config, restored, receipt, key_file):
    """Return a reviewable readiness decision; never starts writers or switches traffic."""
    fenced(config)
    folder = Path(restored)
    if (folder / "INCOMPLETE").exists():
        raise ValueError("Incomplete restore")
    state = json.loads((folder / "recovery-state.json").read_text())
    expected = hmac.new(
        signing_key(key_file),
        b"acceptance-v1" + canonical(receipt.get("payload", {})),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(receipt.get("signature", ""), expected):
        raise ValueError("Unsigned acceptance receipt")
    receipt = receipt["payload"]
    if receipt.get("snapshotDigest") != state.get("snapshotDigest") or receipt.get(
        "codeRevision"
    ) != state.get("codeRevision"):
        raise ValueError("Acceptance belongs to a different restore")
    required = {
        "applicationReadiness",
        "ledgerReconciled",
        "streamCheckpointChecked",
        "ipfsEvidenceVerified",
        "operatorApproved",
    }
    if state["phase"] != "RESTORED_ISOLATED" or not all(receipt.get(k) is True for k in required):
        raise ValueError("Recovery acceptance receipts incomplete")
    return {
        "phase": "READY_FOR_CONTROLLED_CUTOVER",
        "productionPromoted": False,
        "oldWriterFenced": True,
    }


def sign_acceptance(restored, checks, key_file):
    state = json.loads((Path(restored) / "recovery-state.json").read_text())
    payload = {
        **checks,
        "snapshotDigest": state["snapshotDigest"],
        "codeRevision": state["codeRevision"],
    }
    return {
        "payload": payload,
        "signature": hmac.new(
            signing_key(key_file), b"acceptance-v1" + canonical(payload), hashlib.sha256
        ).hexdigest(),
    }
