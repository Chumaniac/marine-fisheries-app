"""Streaming AES-256-GCM envelope for verified system recovery bundles."""

import hashlib
import hmac
import os
import tarfile
import tempfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from marine.stack_recovery import signing_key, verify, inventory

HEADER = b"MARINE-DR-2\x00"


def key(path):
    return hmac.new(signing_key(path), b"marine-recovery-encryption-v2", hashlib.sha256).digest()


def encrypt_bundle(source, destination, key_file):
    source = Path(source)
    destination = Path(destination)
    manifest = verify(source, key_file)
    if destination.exists():
        raise ValueError("Encrypted destination must be new")
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / "bundle.tar"
        with tarfile.open(archive, "w") as tar:
            for name in ["system-manifest.json"] + [r["archive"] for r in manifest["resources"]]:
                tar.add(source / name, arcname=name)
        nonce = os.urandom(12)
        encryptor = Cipher(algorithms.AES(key(key_file)), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(HEADER)
        with archive.open("rb") as incoming, destination.open("xb") as outgoing:
            destination.chmod(0o600)
            outgoing.write(HEADER + nonce)
            for block in iter(lambda: incoming.read(1024 * 1024), b""):
                outgoing.write(encryptor.update(block))
            outgoing.write(encryptor.finalize())
            outgoing.write(encryptor.tag)
    return {
        "encrypted": True,
        "resources": len(manifest["resources"]),
        "bytes": destination.stat().st_size,
    }


def decrypt_bundle(source, destination, key_file):
    source = Path(source)
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Decrypted destination must be new")
    if source.stat().st_size < len(HEADER) + 28:
        raise ValueError("Truncated envelope")
    if source.stat().st_size > 1024**4:
        raise ValueError("Archive exceeds 1 TiB")
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / "bundle.tar"
        with source.open("rb") as incoming:
            if incoming.read(len(HEADER)) != HEADER:
                raise ValueError("Invalid recovery envelope")
            nonce = incoming.read(12)
            incoming.seek(-16, 2)
            tag = incoming.read(16)
            incoming.seek(len(HEADER) + 12)
            decryptor = Cipher(algorithms.AES(key(key_file)), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(HEADER)
            remaining = source.stat().st_size - len(HEADER) - 28
            with archive.open("xb") as output:
                archive.chmod(0o600)
                while remaining:
                    block = incoming.read(min(1024 * 1024, remaining))
                    remaining -= len(block)
                    if not block:
                        raise ValueError("Truncated envelope")
                    output.write(decryptor.update(block))
                output.write(decryptor.finalize())
        inventory(archive)  # reject path traversal, links, devices before extraction
        destination.mkdir(mode=0o700, parents=True)
        try:
            with tarfile.open(archive) as tar:
                tar.extractall(destination, filter="data")
            manifest = verify(destination, key_file)
        except Exception:
            (destination / "INCOMPLETE").touch(mode=0o600)
            raise
    return {"decryptedAndVerified": True, "resources": len(manifest["resources"])}
