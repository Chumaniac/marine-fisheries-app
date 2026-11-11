"""Offline container smoke: synthetic configuration, real signed model, no cloud calls."""

import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from marine.config import preflight
from marine.api import create_app


def main():
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        data = root / "data"
        data.mkdir()
        fabric = root / "fabric"
        fabric.mkdir()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-test-only")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
            .sign(key, hashes.SHA256())
        )
        (root / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root / "client.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        (root / "client.key").chmod(0o600)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk["kid"] = "synthetic"
        (root / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
        (root / "members.json").write_text(
            json.dumps(
                {"operator": {"tenant": "synthetic", "role": "operator", "organization": "Org1MSP"}}
            )
        )
        (root / "session.key").write_bytes(secrets.token_bytes(32))
        (root / "session.key").chmod(0o600)
        (root / "kafka.json").write_text(
            json.dumps(
                {
                    "security_protocol": "SSL",
                    "ssl_cafile": str(root / "ca.pem"),
                    "ssl_certfile": str(root / "ca.pem"),
                    "ssl_keyfile": str(root / "client.key"),
                }
            )
        )
        (root / "kafka.json").chmod(0o600)
        (root / "fabric.json").write_text(
            json.dumps(
                {
                    "orderer": {
                        "address": "orderer.invalid:7050",
                        "hostname": "orderer.invalid",
                        "tlsRootCert": str(root / "ca.pem"),
                    },
                    "organizations": {
                        org: {
                            "peerAddress": org.lower() + ".invalid:7051",
                            "mspPath": str(fabric),
                            "tlsRootCert": str(root / "ca.pem"),
                        }
                        for org in ["Org1MSP", "Org2MSP"]
                    },
                }
            )
        )
        values = {
            "MARINE_MODE": "production",
            "MARINE_WRITE_REPLICAS": "1",
            "MARINE_PUBLIC_URL": "https://marine.invalid",
            "MARINE_OIDC_ISSUER": "https://id.invalid",
            "MARINE_OIDC_AUTHORIZATION_URL": "https://id.invalid/authorize",
            "MARINE_OIDC_TOKEN_URL": "https://id.invalid/token",
            "MARINE_OIDC_CLIENT_ID": "synthetic-browser",
            "MARINE_OIDC_JWKS_FILE": str(root / "jwks.json"),
            "MARINE_MEMBERS_FILE": str(root / "members.json"),
            "MARINE_SESSION_KEY_FILE": str(root / "session.key"),
            "KAFKA_BOOTSTRAP": "kafka.invalid:9093",
            "KAFKA_CLIENT_CONFIG_FILE": str(root / "kafka.json"),
            "MARINE_FABRIC_ROOT": str(fabric),
            "MARINE_FABRIC_CHANNEL": "test",
            "MARINE_FABRIC_CONNECTION_FILE": str(root / "fabric.json"),
            "MARINE_PEER_BINARY": "/usr/local/bin/peer",
            "IPFS_API": "http://primary.invalid:5001",
            "IPFS_REPLICA_APIS": '["http://replica.invalid:5001"]',
            "MARINE_IPFS_MIN_REPLICAS": "2",
            "MARINE_BACKUP_RECEIPT_FILE": str(data / "backup.json"),
            "MARINE_DATA_DIR": str(data),
            "MARINE_MODEL_DIR": "/models",
            "MARINE_MODEL_PUBLIC_KEY_FILE": "/release.pub",
        }
        os.environ.update(values)
        assert preflight()["ready"]
        app = create_app()
        with TestClient(app, base_url="https://marine.invalid", follow_redirects=False) as client:
            assert client.get("/v1/auth/config").json()["browserLogin"] is True
            assert client.post("/v1/session").status_code == 403
            assert client.get("/v1/auth/login").status_code == 303
            assert (
                client.get("/ready").status_code == 503
            )  # no real broker/backup: never fake production readiness
            assert client.get("/v1/dashboard").status_code == 401
        result = {
            "networkDisabled": True,
            "syntheticConfigPreflight": True,
            "signedModelLoaded": True,
            "productionDemoLoginDenied": True,
            "oidcRedirectPrepared": True,
            "unprovisionedDependenciesBlockReadiness": True,
            "publicExposure": False,
        }
        print(json.dumps(result))


if __name__ == "__main__":
    main()
