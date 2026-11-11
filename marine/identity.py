"""Offline-verifiable OIDC JWTs with operator-managed membership and device bindings.

JWKS is provisioned out of band; no token-controlled URL is ever fetched. Reloading
files permits key rotation and immediate membership revocation without a restart.
"""

import json
import os
from pathlib import Path

import jwt
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from marine.storage import canonical


class ExternalIdentity:
    def __init__(self):
        self.issuer = os.environ["MARINE_OIDC_ISSUER"]
        self.audience = os.environ.get("MARINE_OIDC_AUDIENCE", "marine-api")
        self.jwks = Path(os.environ["MARINE_OIDC_JWKS_FILE"])
        self.members = Path(os.environ["MARINE_MEMBERS_FILE"])
        if not self.issuer.startswith("https://"):
            raise ValueError("OIDC issuer must use HTTPS")
        self.keys()  # fail at boot, without exposing key data

    def keys(self):
        data = json.loads(self.jwks.read_text())
        if not 1 <= len(data["keys"]) <= 10 or any(
            "d" in k or k.get("kty") != "RSA" for k in data["keys"]
        ):
            raise ValueError("Public RSA JWKS required")
        return jwt.PyJWKSet.from_dict(data)

    def decode(self, token, audience=None):
        header = jwt.get_unverified_header(token)
        keys = [k for k in self.keys().keys if k.key_id == header.get("kid")]
        if len(keys) != 1:
            raise jwt.InvalidTokenError("Unknown key")
        claims = jwt.decode(
            token,
            keys[0].key,
            algorithms=["RS256"],
            issuer=self.issuer,
            audience=audience or self.audience,
            options={"require": ["sub", "iat", "exp"]},
        )
        members = json.loads(self.members.read_text())
        member = members.get(claims["sub"])
        if not member or member.get("disabled"):
            raise jwt.InvalidTokenError("Membership revoked")
        if member.get("role") not in {"operator", "viewer", "auditor", "device"} or not member.get(
            "tenant"
        ):
            raise jwt.InvalidTokenError("Invalid membership")
        if member.get("organization") not in {"Org1MSP", "Org2MSP"}:
            raise jwt.InvalidTokenError("Explicit organization membership required")
        # Roles, tenant and device identity come from local trusted membership, not JWT custom claims.
        return {
            **claims,
            **{k: member[k] for k in ["tenant", "role"]},
            "deviceId": member.get("deviceId"),
            "organization": member["organization"],
        }


class Device(BaseModel):
    model_config = ConfigDict(extra="forbid")
    siteId: str = Field(min_length=1, max_length=128)
    enabled: bool = True


def register_devices(app, store, operator):
    with store.connect() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS devices(tenant TEXT,id TEXT,payload TEXT,PRIMARY KEY(tenant,id))"
        )

    @app.put("/v1/devices/{device_id}")
    def device(device_id: str, body: Device, claims=Depends(operator)):
        if not 1 <= len(device_id) <= 128:
            raise HTTPException(422, "Invalid device")
        with store.connect() as db:
            db.execute(
                "INSERT INTO devices VALUES(?,?,?) ON CONFLICT(tenant,id) DO UPDATE SET payload=excluded.payload",
                (claims["tenant"], device_id, canonical(body.model_dump()).decode()),
            )
        return {"deviceId": device_id, **body.model_dump()}


def check_device(store, claims, event, required):
    with store.connect() as db:
        row = db.execute(
            "SELECT payload FROM devices WHERE tenant=? AND id=?",
            (claims["tenant"], event.get("deviceId")),
        ).fetchone()
    if not row:
        if required or claims["role"] == "device":
            raise HTTPException(403, "Unregistered device")
        return  # explicitly local demonstration mode only
    binding = json.loads(row[0])
    if not binding["enabled"] or binding["siteId"] != event.get("siteId"):
        raise HTTPException(403, "Device revoked or site mismatch")
    if claims["role"] == "device" and claims.get("deviceId") != event.get("deviceId"):
        raise HTTPException(403, "Device identity mismatch")
