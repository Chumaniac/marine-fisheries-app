"""Central production preflight; diagnostics contain names/reasons, never values."""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Limits:
    backlog_warning: int = 1000
    heartbeat_seconds: int = 180
    backup_max_hours: int = 48


def preflight(env=None):
    env = os.environ if env is None else env
    checks = []

    def check(name, ok, reason):
        checks.append({"setting": name, "ok": bool(ok), "reason": "valid" if ok else reason})

    def required(name):
        value = env.get(name, "")
        check(name, bool(value), "required")
        return value

    def file(name, private=False):
        value = required(name)
        if not value:
            return None
        p = Path(value)
        check(name + ".file", p.is_file(), "file missing")
        if not p.is_file():
            return None
        check(
            name + ".readable", os.access(p, os.R_OK), "file must be readable by service identity"
        )
        if private:
            check(
                name + ".permissions",
                p.stat().st_mode & 0o077 == 0,
                "must not be group/world accessible",
            )
        return p

    def endpoint(name):
        value = required(name)
        p = urlsplit(value)
        check(
            name + ".https",
            p.scheme == "https"
            and p.hostname
            and not p.username
            and not p.password
            and not p.query
            and not p.fragment,
            "fixed HTTPS URL required",
        )

    if env.get("MARINE_MODE") != "production":
        return {"ready": True, "profile": "local", "checks": []}
    for name in [
        "MARINE_PUBLIC_URL",
        "MARINE_OIDC_ISSUER",
        "MARINE_OIDC_AUTHORIZATION_URL",
        "MARINE_OIDC_TOKEN_URL",
    ]:
        endpoint(name)
    for name in ["MARINE_OIDC_CLIENT_ID", "KAFKA_BOOTSTRAP", "MARINE_FABRIC_CHANNEL"]:
        required(name)
    for name in ["MARINE_OIDC_JWKS_FILE", "MARINE_MEMBERS_FILE"]:
        path = file(name)
        if path:
            try:
                value = json.loads(path.read_text())
                valid = isinstance(value, dict) and bool(value)
                if name == "MARINE_OIDC_JWKS_FILE":
                    import jwt

                    valid = (
                        valid
                        and isinstance(value.get("keys"), list)
                        and 1 <= len(value["keys"]) <= 10
                        and all(k.get("kty") == "RSA" and "d" not in k for k in value["keys"])
                    )
                    if valid:
                        jwt.PyJWKSet.from_dict(value)
                else:
                    valid = valid and all(
                        isinstance(m, dict)
                        and m.get("tenant")
                        and m.get("role") in {"viewer", "operator", "auditor", "device"}
                        and m.get("organization") in {"Org1MSP", "Org2MSP"}
                        for m in value.values()
                    )
                check(name + ".json", valid, "invalid public keys or membership structure")
            except Exception:
                check(name + ".json", False, "invalid public keys or membership structure")
    key = file("MARINE_SESSION_KEY_FILE", True)
    if key:
        check("MARINE_SESSION_KEY_FILE.length", key.stat().st_size == 32, "32 bytes required")
    if env.get("MARINE_OIDC_CLIENT_SECRET_FILE"):
        file("MARINE_OIDC_CLIENT_SECRET_FILE", True)
    security = file("KAFKA_CLIENT_CONFIG_FILE", True)
    if security:
        try:
            value = json.loads(security.read_text())
            check(
                "Kafka.TLS", value.get("security_protocol") in {"SSL", "SASL_SSL"}, "TLS required"
            )
            check("Kafka.CA", Path(value.get("ssl_cafile", "")).is_file(), "CA file required")
            if value.get("security_protocol") == "SSL":
                check(
                    "Kafka.client_identity",
                    all(Path(value.get(k, "")).is_file() for k in ["ssl_certfile", "ssl_keyfile"]),
                    "mTLS client certificate/key required",
                )
            elif value.get("security_protocol") == "SASL_SSL":
                check(
                    "Kafka.sasl",
                    bool(value.get("sasl_mechanism"))
                    and bool(value.get("sasl_plain_username"))
                    and bool(value.get("sasl_plain_password")),
                    "SASL identity required",
                )
        except (ValueError, OSError, AttributeError, TypeError):
            check("Kafka.TLS", False, "invalid security file")
    model = required("MARINE_MODEL_DIR")
    check(
        "model.files",
        bool(model) and all((Path(model) / n).is_file() for n in ["model.onnx", "report.json"]),
        "installed inference model required",
    )
    public_key = file("MARINE_MODEL_PUBLIC_KEY_FILE")
    if model and public_key:
        try:
            from marine.edge_runtime import verify_release

            verify_release(model, public_key.read_bytes())
            check("model.signature", True, "")
        except Exception:
            check("model.signature", False, "approved signed bundle required")
    data = required("MARINE_DATA_DIR")
    check(
        "data.directory",
        bool(data) and Path(data).is_dir() and os.access(data, os.W_OK),
        "writable persistent directory required",
    )
    check(
        "IPFS.endpoint",
        urlsplit(env.get("IPFS_API", "")).scheme in {"http", "https"},
        "private IPFS endpoint required",
    )
    file("MARINE_FABRIC_CONNECTION_FILE")
    try:
        replicas = json.loads(env.get("IPFS_REPLICA_APIS", "[]"))
        minimum = int(env.get("MARINE_IPFS_MIN_REPLICAS", "0"))
        check(
            "IPFS.replication",
            2 <= minimum <= 3 and len(set(replicas + [env.get("IPFS_API", "")])) >= minimum,
            "at least two independently configured replicas required",
        )
    except (ValueError, TypeError):
        check("IPFS.replication", False, "invalid replica policy")
    root = required("MARINE_FABRIC_ROOT")
    check("Fabric.root", bool(root) and Path(root).is_dir(), "MSP/network directory required")
    peer = file("MARINE_PEER_BINARY")
    if peer:
        check("Fabric.peer", os.access(peer, os.X_OK), "peer binary must be executable")
    backup = required("MARINE_BACKUP_RECEIPT_FILE")
    check(
        "backup.receipt_parent",
        bool(backup) and Path(backup).parent.is_dir(),
        "persistent backup receipt location required",
    )
    check(
        "deployment.single_writer",
        env.get("MARINE_WRITE_REPLICAS") == "1",
        "SQLite profile requires exactly one fenced writer",
    )
    return {"ready": all(c["ok"] for c in checks), "profile": "production", "checks": checks}


def require_production():
    result = preflight({**os.environ, "MARINE_MODE": "production"})
    if not result["ready"]:
        raise RuntimeError(
            "Production preflight rejected: "
            + ", ".join(c["setting"] for c in result["checks"] if not c["ok"])
        )
    return result
