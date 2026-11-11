import time
import jwt
from fastapi.testclient import TestClient
from marine.api import create_app
from marine.logistics import LedgerSync


def test_local_two_party_lifecycle_conservation_and_cold_chain(tmp_path, monkeypatch):
    key = b"test-signing-key-for-logistics-00000000000000"
    path = tmp_path / "key"
    path.write_bytes(key)
    monkeypatch.setenv("MARINE_JWT_KEY_FILE", str(path))

    def auth(org):
        return {
            "Authorization": "Bearer "
            + jwt.encode(
                {
                    "sub": org,
                    "tenant": "demo",
                    "role": "operator",
                    "organization": org,
                    "iss": "marine-local",
                    "aud": "marine-api",
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 300,
                },
                key,
                algorithm="HS256",
            )
        }

    with TestClient(create_app(tmp_path / "data")) as c:
        c.headers.update(auth("Org1MSP"))
        body = {
            "requestId": "create-0001",
            "batchId": "parent",
            "speciesId": "saithe",
            "massGrams": 1000,
            "areaCode": "A",
        }
        assert c.post("/v1/batches", json=body).status_code == 201
        assert (
            c.post(
                "/v1/batches/parent/transitions",
                json={"requestId": "land-0001", "expectedRevision": 1, "nextState": "LANDED"},
            ).status_code
            == 200
        )
        assert (
            c.post(
                "/v1/batches/parent/transfers",
                json={"requestId": "proposal-0001", "expectedRevision": 2, "target": "Org2MSP"},
            ).status_code
            == 200
        )
        accept = {"requestId": "accept-0001", "expectedRevision": 3}
        assert c.post("/v1/transfers/proposal-0001/accept", json=accept).status_code == 403
        c.headers.update(auth("Org2MSP"))
        assert c.post("/v1/transfers/proposal-0001/accept", json=accept).status_code == 200
        transform = {
            "requestId": "transform-0001",
            "parents": {"parent": 4},
            "outputs": [{"requestId": "output-0001", "batchId": "child", "massGrams": 900}],
            "lossGrams": 99,
        }
        assert c.post("/v1/batch-transforms", json=transform).status_code == 422
        transform["lossGrams"] = 100
        assert c.post("/v1/batch-transforms", json=transform).json()["massConserved"]
        assert c.post("/v1/batch-transforms", json=transform).status_code == 200
        assert (
            c.post(
                "/v1/batches/child/transitions",
                json={
                    "requestId": "cool-0001",
                    "expectedRevision": 1,
                    "nextState": "IN_COLD_CHAIN",
                },
            ).status_code
            == 200
        )
        cold = {
            "requestId": "cold-0001",
            "startAt": time.time() - 100,
            "endAt": time.time() - 1,
            "minimumC": -2,
            "maximumC": 8,
            "allowedMinimumC": -3,
            "allowedMaximumC": 4,
            "deviceId": "manual",
        }
        result = c.post("/v1/batches/child/cold-chain", json=cold)
        assert result.status_code == 200 and result.json()["excursion"]
        assert c.post("/v1/batches/child/cold-chain", json=cold).json() == result.json()
        c.headers.update(auth("Org1MSP"))
        assert (
            c.post(
                "/v1/batches/child/transitions",
                json={"requestId": "deliver-0001", "expectedRevision": 2, "nextState": "DELIVERED"},
            ).status_code
            == 403
        )
        assert len(c.get("/v1/ledger/status").json()["jobs"]) == 6


def test_unknown_commit_reconciles_without_resubmit(tmp_path):
    from marine.storage import Store
    from marine.logistics import enqueue

    store = Store(tmp_path / "db")
    service = LedgerSync(store)
    with store.connect() as db:
        enqueue(
            db,
            "t",
            "request-0001",
            "b",
            "create",
            {"speciesId": "s", "areaCode": "A", "massGrams": 1},
            "Org1MSP",
        )

    class Fake:
        channel = "test"
        calls = 0
        receipts = {}

        def __init__(self, org):
            pass

        def query(self, function, *args):
            return self.receipts.get(args[0]) if function == "GetReceipt" else None

        def invoke(self, *args):
            Fake.calls += 1
            self.receipts[args[1]] = {"txId": "committed-before-timeout"}
            raise RuntimeError("lost response")

    assert service.sync("t", Fake)["status"] == "unconfirmed"
    assert service.sync("t", Fake)["committed"] == 1
    assert Fake.calls == 1


def test_fabric_missing_state_is_distinct_from_network_failure(tmp_path, monkeypatch):
    import subprocess
    from marine.fabric import Fabric
    import pytest

    monkeypatch.setenv("MARINE_FABRIC_ROOT", str(tmp_path))
    monkeypatch.setenv("MARINE_PEER_BINARY", str(tmp_path / "peer"))
    f = Fabric()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, b"", b'Error: endorsement failure response: status:500 message:"not found"'
        ),
    )
    assert f.query("GetReceipt", "missing") is None
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b"Error: connection refused"),
    )
    with pytest.raises(RuntimeError):
        f.query("GetReceipt", "unknown")
