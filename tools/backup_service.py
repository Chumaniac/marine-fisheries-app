"""Encrypted, bounded application backups. Full-component cold snapshots remain explicit."""

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from marine.backup import backup, file_hash
from marine.stack_recovery import snapshot
from marine.recovery_encryption import encrypt_bundle, decrypt_bundle
from marine.operations import Operations
from marine.storage import Store, canonical


def once():
    root = Path(os.environ["MARINE_BACKUP_DIRECTORY"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path(os.environ["MARINE_DATA_DIR"])
    key = os.environ["MARINE_RECOVERY_KEY_FILE"]
    limit = int(os.getenv("MARINE_BACKUP_MAX_BUNDLES", "30"))
    if not 1 <= limit <= 1000 or len(list(root.glob("backup-*"))) >= limit:
        raise ValueError("Backup capacity reached; archive explicitly")
    if shutil.disk_usage(root).free < 512 * 1024 * 1024:
        raise ValueError("Backup disk reserve reached")
    identity = "backup-" + str(time.time_ns())
    target = root / (identity + ".enc")
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=root) as scratch:
        scratch = Path(scratch)
        backup(source, scratch / "application", os.getenv("MARINE_MODEL_DIR"), write_receipt=False)
        snapshot(
            {
                "resources": [
                    {
                        "kind": "directory",
                        "name": "application",
                        "source": str(scratch / "application"),
                    }
                ],
                "codeRevision": os.getenv("MARINE_RELEASE_REVISION", "unspecified"),
            },
            scratch / "system",
            key,
        )
        encrypt_bundle(scratch / "system", target, key)
        decrypt_bundle(target, scratch / "verify", key)
    receipt = {
        "verified": True,
        "verifiedAt": time.time(),
        "encrypted": True,
        "artifactSha256": file_hash(target),
        "scope": "application SQL, referenced evidence, model; not entire distributed stack",
    }
    path = Path(os.environ["MARINE_BACKUP_RECEIPT_FILE"])
    temp = path.with_suffix(".tmp")
    temp.write_bytes(canonical(receipt))
    temp.chmod(0o600)
    os.replace(temp, path)
    return receipt


def main(single=False):
    ops = Operations(Store(Path(os.environ["MARINE_DATA_DIR"]) / "marine.sqlite3"))
    interval = int(os.getenv("MARINE_BACKUP_INTERVAL_SECONDS", "86400"))
    if interval < 3600:
        raise ValueError("Minimum backup interval is one hour")
    while True:
        try:
            result = once()
            ops.heartbeat("backup", True)
            print(
                json.dumps(
                    {"status": "verified", "encrypted": True, "verifiedAt": result["verifiedAt"]}
                ),
                flush=True,
            )
        except Exception:
            ops.heartbeat("backup", False)
            if single:
                raise
            print(
                json.dumps(
                    {
                        "status": "backup_failed",
                        "action": "check storage capacity and mounted configuration",
                    }
                ),
                flush=True,
            )
        if single:
            return
        time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    main(p.parse_args().once)
