"""System-level cold snapshot/verify/isolated restore with explicit writer fencing."""

import argparse
import json
from marine.stack_recovery import snapshot, verify, restore, promotion_check

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "action",
        choices=[
            "snapshot",
            "verify",
            "restore",
            "promotion-check",
            "encrypt",
            "decrypt",
            "sign-acceptance",
        ],
    )
    p.add_argument("--source")
    p.add_argument("--destination")
    p.add_argument("--config")
    p.add_argument("--key-file", required=True)
    p.add_argument("--volume-prefix", default="marine-dr-check")
    p.add_argument("--receipt")
    a = p.parse_args()
    if a.action in {"encrypt", "decrypt"}:
        from marine.recovery_encryption import encrypt_bundle, decrypt_bundle

        result = (encrypt_bundle if a.action == "encrypt" else decrypt_bundle)(
            a.source, a.destination, a.key_file
        )
    elif a.action == "sign-acceptance":
        from marine.stack_recovery import sign_acceptance

        result = sign_acceptance(a.source, json.load(open(a.receipt)), a.key_file)
    elif a.action == "snapshot":
        result = snapshot(json.load(open(a.config)), a.destination, a.key_file)
    elif a.action == "verify":
        result = {
            "phase": "SNAPSHOT_VERIFIED",
            "resources": len(verify(a.source, a.key_file)["resources"]),
        }
    elif a.action == "restore":
        result = restore(a.source, a.destination, a.key_file, a.volume_prefix)
    else:
        result = promotion_check(
            json.load(open(a.config)), a.source, json.load(open(a.receipt)), a.key_file
        )
    print(json.dumps(result, indent=2))
