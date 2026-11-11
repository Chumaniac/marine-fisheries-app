"""OIDC code + S256 PKCE; browser receives opaque cookies, never provider tokens."""

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse


def hash_token(value):
    return hashlib.sha256(value.encode()).hexdigest()


def https_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError("Configured auth URL must be fixed HTTPS without credentials/query")
    return value


class BrowserLogin:
    def __init__(self, store, identity):
        self.store, self.identity = store, identity
        self.authorization = https_url(os.environ["MARINE_OIDC_AUTHORIZATION_URL"])
        self.token_url = https_url(os.environ["MARINE_OIDC_TOKEN_URL"])
        self.public_url = https_url(os.environ["MARINE_PUBLIC_URL"]).rstrip("/")
        self.callback = self.public_url + "/v1/auth/callback"
        self.client_id = os.environ["MARINE_OIDC_CLIENT_ID"]
        self.key = Path(os.environ["MARINE_SESSION_KEY_FILE"]).read_bytes()
        if len(self.key) != 32:
            raise ValueError("Session encryption key must be 32 bytes")
        self.secret_file = os.getenv("MARINE_OIDC_CLIENT_SECRET_FILE")
        with store.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS login_attempts(id TEXT PRIMARY KEY,payload BLOB,expires REAL);
            CREATE TABLE IF NOT EXISTS browser_sessions(id TEXT PRIMARY KEY,payload BLOB,expires REAL);
            """)

    def seal(self, value):
        nonce = secrets.token_bytes(12)
        return nonce + AESGCM(self.key).encrypt(
            nonce, json.dumps(value).encode(), b"marine-browser-v1"
        )

    def unseal(self, value):
        return json.loads(AESGCM(self.key).decrypt(value[:12], value[12:], b"marine-browser-v1"))

    def start(self):
        state, verifier, nonce = [secrets.token_urlsafe(32) for _ in range(3)]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_attempts WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM browser_sessions WHERE expires<?", (time.time(),))
            if db.execute("SELECT count(*) FROM login_attempts").fetchone()[0] >= 1000:
                raise HTTPException(429, "Login capacity reached")
            db.execute(
                "INSERT INTO login_attempts VALUES(?,?,?)",
                (
                    hash_token(state),
                    self.seal({"verifier": verifier, "nonce": nonce}),
                    time.time() + 300,
                ),
            )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        query = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.callback,
            "scope": "openid",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        response = RedirectResponse(self.authorization + "?" + urlencode(query), status_code=303)
        response.set_cookie(
            "marine_login",
            state,
            max_age=300,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/v1/auth",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def exchange(self, code, verifier):
        form = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": code,
            "redirect_uri": self.callback,
            "code_verifier": verifier,
        }
        auth = (
            (self.client_id, Path(self.secret_file).read_text().strip())
            if self.secret_file
            else None
        )
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            with client.stream("POST", self.token_url, data=form, auth=auth) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError("Token response too large")
                return json.loads(raw)["id_token"]

    def finish(self, request, state, code):
        cookie = request.cookies.get("marine_login", "")
        if (
            not state
            or len(state) > 128
            or not secrets.compare_digest(state, cookie)
            or not code
            or len(code) > 4096
        ):
            raise HTTPException(401, "Login could not be verified")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT payload FROM login_attempts WHERE id=? AND expires>?",
                (hash_token(state), time.time()),
            ).fetchone()
            if not row:
                raise HTTPException(401, "Login expired or already used")
            db.execute("DELETE FROM login_attempts WHERE id=?", (hash_token(state),))
        try:
            attempt = self.unseal(row[0])
            token = self.exchange(code, attempt["verifier"])
            claims = self.identity.decode(token, audience=self.client_id)
            if (
                not secrets.compare_digest(claims.get("nonce", ""), attempt["nonce"])
                or claims["role"] == "device"
            ):
                raise ValueError("Invalid browser identity")
            if (
                isinstance(claims.get("aud"), list)
                and len(claims["aud"]) > 1
                and claims.get("azp") != self.client_id
            ) or (claims.get("azp") and claims["azp"] != self.client_id):
                raise ValueError("Invalid authorized party")
        except (jwt.PyJWTError, httpx.HTTPError, KeyError, ValueError, OSError, TypeError):
            raise HTTPException(401, "Identity provider login failed; start again") from None
        sid = secrets.token_urlsafe(32)
        expires = min(float(claims["exp"]), time.time() + 3600)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT count(*) FROM browser_sessions").fetchone()[0] >= 10000:
                raise HTTPException(429, "Session capacity reached")
            db.execute(
                "INSERT INTO browser_sessions VALUES(?,?,?)",
                (hash_token(sid), self.seal({"idToken": token}), expires),
            )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            "marine_browser",
            sid,
            max_age=max(0, int(expires - time.time())),
            secure=True,
            httponly=True,
            samesite="strict",
        )
        response.delete_cookie(
            "marine_login", path="/v1/auth", secure=True, httponly=True, samesite="lax"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def decode(self, sid):
        if not sid or len(sid) > 128:
            raise jwt.InvalidTokenError("Session missing")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT payload,expires FROM browser_sessions WHERE id=? AND expires>?",
                (hash_token(sid), time.time()),
            ).fetchone()
        if not row:
            raise jwt.InvalidTokenError("Session expired")
        try:
            decoded = self.unseal(row["payload"])
        except Exception:
            raise jwt.InvalidTokenError("Session cannot be decoded") from None
        claims = self.identity.decode(decoded["idToken"], audience=self.client_id)
        claims["exp"] = min(claims["exp"], row["expires"])
        return claims

    def logout(self, sid):
        if sid:
            with self.store.connect() as db:
                db.execute("DELETE FROM browser_sessions WHERE id=?", (hash_token(sid),))


def register_login(app, store, external_identity):
    login = (
        BrowserLogin(store, external_identity)
        if external_identity and os.getenv("MARINE_OIDC_CLIENT_ID")
        else None
    )

    @app.get("/v1/auth/config")
    def config():
        return {
            "mode": app.state.mode,
            "browserLogin": bool(login),
            "loginPath": "/v1/auth/login" if login else None,
        }

    @app.get("/v1/auth/login")
    def start():
        if not login:
            raise HTTPException(503, "Browser identity provider is not configured")
        return login.start()

    @app.get("/v1/auth/callback")
    def callback(request: Request, state: str = "", code: str = ""):
        if not login:
            raise HTTPException(503, "Browser identity provider is not configured")
        return login.finish(request, state, code)

    return login
