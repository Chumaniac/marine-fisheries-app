import hashlib
import json
import time
from urllib.parse import urlsplit, parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from marine.api import create_app
from marine.browser_login import BrowserLogin


@pytest.fixture
def oidc(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test"
    (tmp_path / "jwks").write_text(json.dumps({"keys": [jwk]}))
    members = tmp_path / "members"
    members.write_text(
        json.dumps({"alice": {"tenant": "fleet", "role": "operator", "organization": "Org1MSP"}})
    )
    (tmp_path / "session.key").write_bytes(b"x" * 32)
    config = {
        "MARINE_OIDC_ISSUER": "https://id.example",
        "MARINE_OIDC_JWKS_FILE": str(tmp_path / "jwks"),
        "MARINE_MEMBERS_FILE": str(members),
        "MARINE_OIDC_AUTHORIZATION_URL": "https://id.example/authorize",
        "MARINE_OIDC_TOKEN_URL": "https://id.example/token",
        "MARINE_PUBLIC_URL": "https://marine.example",
        "MARINE_OIDC_CLIENT_ID": "browser-client",
        "MARINE_SESSION_KEY_FILE": str(tmp_path / "session.key"),
    }
    for k, v in config.items():
        monkeypatch.setenv(k, v)
    app = create_app(tmp_path / "data", mode="identity-test")
    return app, key, members


def test_browser_pkce_nonce_session_revocation_and_logout(oidc, monkeypatch):
    app, key, members = oidc
    with TestClient(app, base_url="https://marine.example", follow_redirects=False) as c:
        response = c.get("/v1/auth/login")
        assert response.status_code == 303
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]

        def exchange(self, code, verifier):
            import base64

            assert code == "single-use-code"
            assert (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .rstrip(b"=")
                .decode()
                == query["code_challenge"][0]
            )
            return jwt.encode(
                {
                    "sub": "alice",
                    "iss": "https://id.example",
                    "aud": "browser-client",
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 300,
                    "nonce": query["nonce"][0],
                },
                key,
                algorithm="RS256",
                headers={"kid": "test"},
            )

        monkeypatch.setattr(BrowserLogin, "exchange", exchange)
        assert (
            c.get(
                "/v1/auth/callback", params={"state": "forged", "code": "single-use-code"}
            ).status_code
            == 401
        )
        response = c.get(
            "/v1/auth/callback", params={"state": query["state"][0], "code": "single-use-code"}
        )
        assert (
            response.status_code == 303
            and "HttpOnly" in response.headers["set-cookie"]
            and "Secure" in response.headers["set-cookie"]
        )
        assert (
            c.get(
                "/v1/auth/callback", params={"state": query["state"][0], "code": "single-use-code"}
            ).status_code
            == 401
        )
        actor = c.get("/v1/me").json()
        assert actor["tenant"] == "fleet"
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        saved = c.cookies.get("marine_browser")
        assert c.delete("/v1/session").status_code == 200
        c.cookies.set("marine_browser", saved)
        assert c.get("/v1/me").status_code == 401
        # Session rows never store plaintext provider tokens.
        with app.state.store.connect() as db:
            assert db.execute("SELECT count(*) FROM browser_sessions").fetchone()[0] == 0


def test_wrong_nonce_does_not_create_session(oidc, monkeypatch):
    app, key, _ = oidc
    with TestClient(app, base_url="https://marine.example", follow_redirects=False) as c:
        query = parse_qs(urlsplit(c.get("/v1/auth/login").headers["location"]).query)
        token = jwt.encode(
            {
                "sub": "alice",
                "iss": "https://id.example",
                "aud": "browser-client",
                "iat": int(time.time()),
                "exp": int(time.time()) + 300,
                "nonce": "wrong",
            },
            key,
            algorithm="RS256",
            headers={"kid": "test"},
        )
        monkeypatch.setattr(BrowserLogin, "exchange", lambda *args: token)
        assert (
            c.get(
                "/v1/auth/callback", params={"state": query["state"][0], "code": "code"}
            ).status_code
            == 401
        )
        assert c.get("/v1/me").status_code == 401
