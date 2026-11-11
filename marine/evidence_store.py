"""Encrypted evidence storage and explicit remote-replica verification."""

import hashlib
import json
import os
import secrets
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from marine.storage import canonical

MAX_EVIDENCE_BYTES = 256 * 1024


class EvidenceStore:
    def __init__(self, folder):
        self.folder = Path(folder) / "objects"
        self.folder.mkdir(parents=True, exist_ok=True)
        keypath = Path(folder) / "evidence.key"
        try:
            fd = os.open(keypath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(AESGCM.generate_key(bit_length=256))
        except FileExistsError:
            pass
        self.key = keypath.read_bytes()

    def put(self, content, batch, tenant):
        identity = str(uuid4())
        nonce = secrets.token_bytes(12)
        aad = canonical({"batchId": batch, "tenant": tenant, "evidenceId": identity})
        ciphertext = nonce + AESGCM(self.key).encrypt(nonce, content, aad)
        cipherhash = hashlib.sha256(ciphertext).hexdigest()
        dest = self.folder / cipherhash
        if not dest.exists():
            temporary = dest.with_suffix("." + secrets.token_hex(6))
            with temporary.open("xb") as file:
                file.write(ciphertext)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, dest)
        manifest = {
            "evidenceId": identity,
            "batchId": batch,
            "tenant": tenant,
            "rawSha256": hashlib.sha256(content).hexdigest(),
            "cipherSha256": cipherhash,
            "byteLength": len(content),
            "cipherCid": None,
            "encryption": "AES-256-GCM-local-master-v1",
            "salt": secrets.token_hex(16),
        }
        return manifest

    def endpoints(self):
        primary = os.getenv("IPFS_API")
        extras = json.loads(os.getenv("IPFS_REPLICA_APIS", "[]"))
        if not isinstance(extras, list) or len(extras) > 2:
            raise ValueError("At most two replica endpoints")
        from urllib.parse import urlsplit

        endpoints = ([primary] if primary else []) + extras
        if any(
            not isinstance(e, str)
            or urlsplit(e).scheme not in {"http", "https"}
            or not urlsplit(e).hostname
            or urlsplit(e).username
            or urlsplit(e).password
            for e in endpoints
        ):
            raise ValueError("Invalid IPFS endpoint")
        return list(dict.fromkeys(endpoints))

    def required_replicas(self):
        value = int(os.getenv("MARINE_IPFS_MIN_REPLICAS", "1"))
        if not 1 <= value <= 3:
            raise ValueError("Invalid replica policy")
        return value

    def pin(self, manifest):
        endpoints = self.endpoints()
        if not endpoints:
            return manifest
        if manifest.get("cipherCid") and self.remote_verify(manifest)["verified"]:
            return manifest
        data = (self.folder / manifest["cipherSha256"]).read_bytes()
        cid = manifest.get("cipherCid")
        succeeded = 0
        with httpx.Client(timeout=5, trust_env=False) as client:
            for endpoint in endpoints:
                try:
                    response = client.post(
                        endpoint.rstrip("/") + "/api/v0/add",
                        params={"cid-version": 1, "pin": "true", "wrap-with-directory": "false"},
                        files={"file": ("evidence.bin", data)},
                    )
                    response.raise_for_status()
                    added = response.json()["Hash"]
                    if cid and cid != added:
                        raise ValueError("Replica CID mismatch")
                    cid = added
                    with client.stream(
                        "POST", endpoint.rstrip("/") + "/api/v0/cat", params={"arg": cid}
                    ) as check:
                        check.raise_for_status()
                        h = hashlib.sha256()
                        size = 0
                        for chunk in check.iter_bytes():
                            size += len(chunk)
                            if size > len(data):
                                raise ValueError("IPFS size mismatch")
                            h.update(chunk)
                    if h.hexdigest() != manifest["cipherSha256"]:
                        raise ValueError("IPFS roundtrip mismatch")
                    succeeded += 1
                except httpx.HTTPError:
                    continue
        if succeeded < self.required_replicas():
            raise httpx.HTTPError("Insufficient verified replicas")
        return {**manifest, "cipherCid": cid}

    def read(self, manifest):
        size = manifest["byteLength"]
        if not isinstance(size, int) or not 0 <= size <= MAX_EVIDENCE_BYTES:
            raise ValueError("Invalid evidence size")
        # Read only the declared plaintext plus nonce/tag and one overflow byte.
        with (self.folder / manifest["cipherSha256"]).open("rb") as source:
            content = source.read(size + 29)
        if len(content) != size + 28:
            raise ValueError("Ciphertext size mismatch")
        if hashlib.sha256(content).hexdigest() != manifest["cipherSha256"]:
            raise ValueError("Ciphertext digest mismatch")
        aad = canonical({k: manifest[k] for k in ["batchId", "tenant", "evidenceId"]})
        raw = AESGCM(self.key).decrypt(content[:12], content[12:], aad)
        if hashlib.sha256(raw).hexdigest() != manifest["rawSha256"]:
            raise ValueError("Plaintext digest mismatch")
        return raw

    def remote_verify(self, manifest):
        endpoints = self.endpoints()
        cid = manifest.get("cipherCid")
        verified = 0
        node_ids = set()
        if not endpoints or not cid:
            return {
                "status": "not_configured_or_unpinned",
                "verified": False,
                "replicasVerified": 0,
            }
        for endpoint in endpoints:
            try:
                with httpx.Client(timeout=3, trust_env=False) as client:
                    node = client.post(endpoint.rstrip("/") + "/api/v0/id")
                    node.raise_for_status()
                    node_id = node.json()["ID"]
                    if node_id in node_ids:
                        continue
                    pin = client.post(
                        endpoint.rstrip("/") + "/api/v0/pin/ls",
                        params={"arg": cid, "type": "recursive"},
                    )
                    pin.raise_for_status()
                    if pin.json().get("Keys", {}).get(cid, {}).get("Type") != "recursive":
                        continue
                    with client.stream(
                        "POST", endpoint.rstrip("/") + "/api/v0/cat", params={"arg": cid}
                    ) as response:
                        response.raise_for_status()
                        h = hashlib.sha256()
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > manifest["byteLength"] + 28:
                                raise ValueError("Unexpected size")
                            h.update(chunk)
                if h.hexdigest() == manifest["cipherSha256"]:
                    node_ids.add(node_id)
                    verified += 1
            except (httpx.HTTPError, ValueError, KeyError):
                pass
        enough = verified >= self.required_replicas()
        return {
            "status": "verified" if enough else "replica_policy_unmet",
            "verified": enough,
            "replicasVerified": verified,
            "replicasRequired": self.required_replicas(),
        }

    def verify(self, manifest):
        self.read(manifest)
        return True
