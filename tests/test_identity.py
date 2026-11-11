import json
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from marine.api import create_app


def test_external_membership_device_binding_revocation(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test-key", alg="RS256")
    (tmp_path / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    members = {
        "admin": {"tenant": "demo", "role": "operator", "organization": "Org1MSP"},
        "sensor": {
            "tenant": "demo",
            "role": "device",
            "deviceId": "dev1",
            "organization": "Org1MSP",
        },
    }
    memberfile = tmp_path / "members.json"
    memberfile.write_text(json.dumps(members))
    for k, v in {
        "MARINE_OIDC_ISSUER": "https://identity.example",
        "MARINE_OIDC_JWKS_FILE": str(tmp_path / "jwks.json"),
        "MARINE_MEMBERS_FILE": str(memberfile),
    }.items():
        monkeypatch.setenv(k, v)

    def headers(subject, expiry=300):
        token = jwt.encode(
            {
                "sub": subject,
                "iss": "https://identity.example",
                "aud": "marine-api",
                "iat": int(time.time()),
                "exp": int(time.time()) + expiry,
                "tenant": "evil",
                "role": "operator",
            },
            key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        return {"Authorization": "Bearer " + token}

    with TestClient(create_app(tmp_path / "data", mode="identity-test")) as c:
        assert c.post("/v1/session").status_code == 403
        assert c.get("/v1/me", headers=headers("admin")).json()["tenant"] == "demo"
        assert (
            c.put("/v1/devices/dev1", json={"siteId": "A"}, headers=headers("admin")).status_code
            == 200
        )
        from pathlib import Path

        event = json.loads(Path("contracts/examples.json").read_text())[0]
        event.update(
            tenantId="demo", deviceId="dev1", siteId="A", occurredAt="2026-01-01T00:00:00Z"
        )
        assert (
            c.post(
                "/v1/event-batches", json={"events": [event]}, headers=headers("sensor")
            ).status_code
            == 202
        )
        event["siteId"] = "B"
        assert (
            c.post(
                "/v1/event-batches", json={"events": [event]}, headers=headers("sensor")
            ).status_code
            == 403
        )
        assert c.get("/v1/dashboard", headers=headers("sensor")).status_code == 403
        assert c.get("/v1/me", headers=headers("admin", -1)).status_code == 401
        members["sensor"]["disabled"] = True
        memberfile.write_text(json.dumps(members))
        assert c.get("/v1/me", headers=headers("sensor")).status_code == 401


def test_missing_production_organization_denied(tmp_path, monkeypatch):
    from marine.identity import ExternalIdentity

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test"
    (tmp_path / "jwks").write_text(json.dumps({"keys": [jwk]}))
    (tmp_path / "members").write_text(json.dumps({"u": {"role": "operator", "tenant": "t"}}))
    for k, v in {
        "MARINE_OIDC_ISSUER": "https://issuer.example",
        "MARINE_OIDC_JWKS_FILE": str(tmp_path / "jwks"),
        "MARINE_MEMBERS_FILE": str(tmp_path / "members"),
    }.items():
        monkeypatch.setenv(k, v)
    token = jwt.encode(
        {
            "sub": "u",
            "iss": "https://issuer.example",
            "aud": "marine-api",
            "iat": int(time.time()),
            "exp": int(time.time()) + 100,
        },
        key,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    import pytest

    with pytest.raises(jwt.InvalidTokenError):
        ExternalIdentity().decode(token)


def test_outbox_read_roles_preserve_tenant_visibility_and_deny_device_mutations(
    tmp_path, monkeypatch
):
    key = b"synthetic-local-reader-role-key-0000000000000000"
    keyfile = tmp_path / "local.key"
    keyfile.write_bytes(key)
    monkeypatch.setenv("MARINE_JWT_KEY_FILE", str(keyfile))
    with TestClient(create_app(tmp_path / "data")) as client:

        def auth(role):
            token = jwt.encode(
                {
                    "sub": "synthetic-reader",
                    "tenant": "a",
                    "role": role,
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 60,
                    "aud": "marine-api",
                    "iss": "marine-local",
                },
                key,
                algorithm="HS256",
            )
            return {"Authorization": "Bearer " + token}

        assert client.get("/v1/outbox").status_code == 401
        for role in ("viewer", "auditor", "operator"):
            assert client.get("/v1/outbox", headers=auth(role)).status_code == 200
        assert client.get("/v1/outbox", headers=auth("device")).status_code == 403
        for role in ("viewer", "auditor", "device"):
            assert (
                client.post(
                    "/v1/outbox/original/retry",
                    headers=auth(role),
                    json={"note": "synthetic review"},
                ).status_code
                == 403
            )
            assert (
                client.post(
                    "/v1/outbox/original/reconcile",
                    headers=auth(role),
                    json={"note": "synthetic review", "expectedAttempt": 1, "outcome": "published"},
                ).status_code
                == 403
            )
