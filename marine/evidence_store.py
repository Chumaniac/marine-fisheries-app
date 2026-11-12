"""Encrypted evidence storage and explicit remote-replica verification."""

import hashlib
import json
import os
import secrets
import stat
import re
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from marine.storage import canonical
from marine.evidence_recovery import transaction, active_operation, MAX_PENDING_OBJECTS

MAX_EVIDENCE_BYTES = 256 * 1024


class EvidenceStore:
    def __init__(self, folder, *, require_existing_key=False):
        base = Path(folder)
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.folder = base / "objects"
        for directory in [base, self.folder]:
            if directory.exists() or directory.is_symlink():
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or directory.is_symlink()
                        or os.name != 'nt' and (info.st_mode & 0o022 or info.st_uid != os.getuid())):
                    raise ValueError('Evidence directories require owned, non-shared write access; preserve the originals')
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        keypath = Path(folder) / "evidence.key"
        if not keypath.exists():
            if require_existing_key:
                raise ValueError('Evidence key is missing; preserve the database and restore the original key')
            with os.scandir(self.folder) as entries:
                if next(entries, None) is not None:
                    raise ValueError('Evidence key is missing; preserve existing objects and restore the original key')
        try:
            fd = os.open(keypath, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(AESGCM.generate_key(bit_length=256))
                f.flush()
                os.fsync(f.fileno())
            if os.name != "nt":
                directory = os.open(keypath.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except FileExistsError:
            pass
        info = keypath.lstat()
        if os.name != 'nt' and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise ValueError('Evidence key requires private ownership; preserve the original')
        self.key = self._read_regular(keypath, 32)
        if len(self.key) != 32:
            raise ValueError('Invalid evidence key; preserve the workspace')

    @staticmethod
    def _stamp(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    @classmethod
    def _read_regular(cls, path, maximum):
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError('Invalid evidence file; preserve the original')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as source:
            if cls._stamp(os.fstat(source.fileno())) != cls._stamp(before):
                raise ValueError('Evidence changed during inspection')
            data = source.read(maximum + 1)
            if (len(data) != before.st_size or cls._stamp(os.fstat(source.fileno())) != cls._stamp(before)
                    or cls._stamp(path.lstat()) != cls._stamp(before)):
                raise ValueError('Evidence changed during inspection')
        return data

    def _object(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity):
            raise ValueError('Invalid evidence digest')
        return self.folder / identity

    def _sync_folder(self):
        if os.name == 'nt':
            return
        fd = os.open(self.folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def transaction(self, database):
        return transaction(self, database)

    def discard_owned(self, identity, expected):
        captured = None
        scratch = None
        path = self._object(identity)
        try:
            if self._stamp(path.lstat()) != expected:
                return
            data = self._read_regular(path, MAX_EVIDENCE_BYTES + 28)
            if hashlib.sha256(data).hexdigest() != identity or self._stamp(path.lstat()) != expected:
                return
            scratch = Path(tempfile.mkdtemp(prefix='.recovery-', dir=self.folder))
            captured = scratch / 'object'
            # Capture the actual directory entry. Later publication at the original
            # name cannot be removed by this operation's private retirement step.
            os.rename(path, captured)
            info = captured.lstat()
            if self._stamp(info)[:4] != expected[:4]:
                return
            content = self._read_regular(captured, MAX_EVIDENCE_BYTES + 28)
            if hashlib.sha256(content).hexdigest() != identity:
                return
            captured.unlink()
            captured = None
            self._sync_folder()
        except (OSError, ValueError):
            pass
        finally:
            if captured is not None:
                try:
                    os.link(captured, path)  # Never replace a later user file.
                except OSError:
                    pass  # Preserve the captured material for manual reconciliation.
            if scratch is not None:
                try:
                    scratch.rmdir()  # Only empty, operation-owned scratch directories.
                except OSError:
                    pass

    def put(self, content, batch, tenant):
        if not isinstance(content, (bytes, bytearray)) or len(content) > MAX_EVIDENCE_BYTES:
            raise ValueError('Evidence exceeds configured capacity')
        operation = active_operation(self)
        if operation is not None and len(operation.objects) >= MAX_PENDING_OBJECTS:
            raise ValueError('Evidence operation capacity reached')
        identity = str(uuid4())
        nonce = secrets.token_bytes(12)
        aad = canonical({"batchId": batch, "tenant": tenant, "evidenceId": identity})
        ciphertext = nonce + AESGCM(self.key).encrypt(nonce, content, aad)
        cipherhash = hashlib.sha256(ciphertext).hexdigest()
        dest = self._object(cipherhash)
        temporary = dest.with_suffix('.' + secrets.token_hex(6))
        created = False
        temporary_identity = None
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            info = os.fstat(descriptor)
            temporary_identity = info.st_dev, info.st_ino
            with os.fdopen(descriptor, 'wb') as file:
                file.write(ciphertext)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.link(temporary, dest)
                created = True
            except FileExistsError:
                if self._read_regular(dest, MAX_EVIDENCE_BYTES + 28) != ciphertext:
                    raise ValueError('Existing evidence object does not match; preserve it')
        finally:
            if temporary_identity is not None:
                try:
                    current = temporary.lstat()
                    if (current.st_dev, current.st_ino) == temporary_identity:
                        temporary.unlink()
                except FileNotFoundError:
                    pass
        if created and operation is not None:
            operation.objects.append((cipherhash, self._stamp(dest.lstat())))
        self._sync_folder()

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
        data = self._read_regular(self._object(manifest["cipherSha256"]), MAX_EVIDENCE_BYTES + 28)
        if hashlib.sha256(data).hexdigest() != manifest["cipherSha256"]:
            raise ValueError("Ciphertext digest mismatch; no remote upload attempted")
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
        content = self._read_regular(self._object(manifest["cipherSha256"]), size + 28)
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
