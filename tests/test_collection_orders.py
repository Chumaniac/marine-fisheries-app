import json
import time
from uuid import NAMESPACE_URL, uuid5

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from marine.api import create_app
from marine.storage import canonical


@pytest.fixture
def world(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="collection-test", alg="RS256")
    (tmp_path / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    members = {
        "collector": {"tenant": "demo", "role": "operator", "organization": "Org1MSP"},
        "receiver": {"tenant": "demo", "role": "operator", "organization": "Org2MSP"},
        "stranger": {"tenant": "demo", "role": "operator", "organization": "Org1MSP"},
        "auditor": {"tenant": "demo", "role": "auditor", "organization": "Org1MSP"},
        "outside": {"tenant": "other", "role": "operator", "organization": "Org1MSP"},
        "viewer": {"tenant": "demo", "role": "viewer", "organization": "Org1MSP"},
    }
    memberfile = tmp_path / "members.json"
    memberfile.write_text(json.dumps(members))
    monkeypatch.setenv("MARINE_OIDC_ISSUER", "https://collection.example")
    monkeypatch.setenv("MARINE_OIDC_JWKS_FILE", str(tmp_path / "jwks.json"))
    monkeypatch.setenv("MARINE_MEMBERS_FILE", str(memberfile))

    def headers(subject):
        token = jwt.encode(
            {
                "sub": subject,
                "iss": "https://collection.example",
                "aud": "marine-api",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
                "tenant": "forged",
                "role": "operator",
            },
            key,
            algorithm="RS256",
            headers={"kid": "collection-test"},
        )
        return {"Authorization": "Bearer " + token}

    app = create_app(tmp_path / "data", mode="identity-test")
    client = TestClient(app)
    client.__enter__()
    client.headers.update(headers("collector"))
    client.put(
        "/v1/sites/A",
        json={"name": "Synthetic A", "longitude": 120, "latitude": 30, "dataSource": "synthetic"},
    ).raise_for_status()
    client.put("/v1/devices/manual", json={"siteId": "A", "enabled": True}).raise_for_status()
    now = time.time()
    body = {
        "id": "workorder-test-001",
        "siteId": "A",
        "deviceId": "manual",
        "startAt": now - 600,
        "endAt": now + 600,
    }
    value = {
        "client": client,
        "app": app,
        "folder": tmp_path / "data",
        "headers": headers,
        "members": members,
        "memberfile": memberfile,
        "body": body,
        "now": now,
    }
    yield value
    client.__exit__(None, None, None)


def create(world):
    response = world["client"].post("/v1/collection-orders", json=world["body"])
    assert response.status_code == 201, response.text
    return response.json()


def collect(world, order):
    c = world["client"]
    request = {
        "requestId": "reading-order-001",
        "deviceId": "manual",
        "occurredAt": world["now"] - 10,
        "temperatureC": 20,
        "salinityPsu": 30,
        "turbidityNtu": 5,
    }
    sent = c.post("/v1/environment-readings", json=request)
    assert sent.status_code == 200, sent.text
    record_id = sent.json()["acceptedEventIds"][0]
    sources = c.get("/v1/source-events", params={"siteId": "A"}).json()["events"]
    source = next(row for row in sources if row["eventId"] == record_id)
    attach = c.post(
        f"/v1/collection-orders/{order['id']}/sources",
        json={
            "requestId": "source-attach-001",
            "expectedRevision": order["revision"],
            "sources": [{"eventId": record_id, "revision": source["revision"]}],
        },
    )
    assert attach.status_code == 200, attach.text
    body = {
        "requestId": request["requestId"],
        "expectedRevision": attach.json()["order"]["revision"],
        "path": "/v1/environment-readings",
        "recordId": record_id,
    }
    linked = c.post(f"/v1/collection-orders/{order['id']}/records", json=body)
    assert linked.status_code == 200, linked.text
    return linked.json()["order"], body


def offer(world, order):
    response = world["client"].post(
        f"/v1/collection-orders/{order['id']}/handoffs",
        json={
            "requestId": "handoff-offer-001",
            "expectedRevision": order["revision"],
            "recipient": "receiver",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_durable_order_sync_and_distinct_second_actor_handoff(world):
    c = world["client"]
    order, record = collect(world, create(world))
    duplicate = c.post(f"/v1/collection-orders/{order['id']}/records", json=record)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    assert len(duplicate.json()["order"]["records"]) == 1
    handoff = offer(world, order)
    url = f"/v1/collection-orders/{order['id']}/handoffs/{handoff['handoffId']}/accept"
    body = {"requestId": "handoff-accept-001", "expectedRevision": handoff["order"]["revision"]}
    assert c.post(url, json=body).status_code == 403
    accepted = c.post(url, json=body, headers=world["headers"]("receiver"))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["handoffReceipt"]["acceptedBy"] == "receiver"
    assert accepted.json()["order"]["state"] == "handed-over"
    assert accepted.json()["order"]["records"][0]["state"] == "received"
    assert c.post(url, json=body, headers=world["headers"]("receiver")).json()["duplicate"]
    # Reopen the same actual database through a new application instance.
    with TestClient(create_app(world["folder"], mode="identity-test")) as restarted:
        response = restarted.get(
            f"/v1/collection-orders/{order['id']}", headers=world["headers"]("receiver")
        )
        assert response.status_code == 200
        assert response.json()["handoffs"][0]["receipt"] == accepted.json()["handoffReceipt"]
        assert len(response.json()["records"]) == 1


def test_tenant_actor_role_and_parameter_boundaries(world):
    c = world["client"]
    order = create(world)
    for person in ["outside", "stranger", "viewer"]:
        assert (
            c.get(
                f"/v1/collection-orders/{order['id']}", headers=world["headers"](person)
            ).status_code
            == 404
        )
    assert (
        c.post(
            "/v1/collection-orders",
            json={**world["body"], "id": "viewer-order"},
            headers=world["headers"]("viewer"),
        ).status_code
        == 403
    )
    assert (
        c.get(
            "/v1/collection-orders",
            params={"after": "' OR 1=1 --"},
            headers=world["headers"]("outside"),
        ).json()["items"]
        == []
    )
    assert (
        c.post(
            "/v1/collection-orders",
            json={**world["body"], "id": "injected-order", "siteId": "' OR 1=1 --"},
        ).status_code
        == 403
    )
    assert c.get("/v1/collection-orders").json()["items"][0]["createdBy"] == "collector"
    assert all(
        row["subject"] != "outside" for row in c.get("/v1/collection-members").json()["members"]
    )


def test_stale_revisions_duplicate_create_and_no_implicit_history_assignment(world):
    c = world["client"]
    order = create(world)
    assert create(world)["revision"] == 1
    assert (
        c.post("/v1/collection-orders", json={**world["body"], "deviceId": "changed"}).status_code
        == 409
    )
    order, _ = collect(world, order)
    other = c.post(
        "/v1/collection-orders", json={**world["body"], "id": "workorder-other-001"}
    ).json()
    assert other["records"] == [] and other["sources"] == []
    closed = {"requestId": "close-request-001", "expectedRevision": 1}
    assert c.post(f"/v1/collection-orders/{order['id']}/close", json=closed).status_code == 409
    assert len(c.get(f"/v1/collection-orders/{order['id']}").json()["records"]) == 1


def test_source_scope_version_and_changed_original_are_rejected(world):
    c = world["client"]
    order, _ = collect(world, create(world))
    source = order["sources"][0]
    request = {
        "requestId": "wrong-source-version",
        "expectedRevision": order["revision"],
        "sources": [{"eventId": source["eventId"], "revision": "0" * 64}],
    }
    assert c.post(f"/v1/collection-orders/{order['id']}/sources", json=request).status_code == 409
    with world["app"].state.store.connect() as db:
        row = db.execute(
            "SELECT payload FROM events WHERE tenant='demo' AND id=?", (source["eventId"],)
        ).fetchone()
        changed = json.loads(row[0])
        changed["data"]["temperatureC"] += 1
        db.execute(
            "UPDATE events SET payload=? WHERE tenant='demo' AND id=?",
            (canonical(changed).decode(), source["eventId"]),
        )
    bad = c.post(
        f"/v1/collection-orders/{order['id']}/handoffs",
        json={
            "requestId": "changed-original-offer",
            "expectedRevision": order["revision"],
            "recipient": "receiver",
        },
    )
    assert bad.status_code == 409


def test_revoke_membership_and_device_then_cancel_without_losing_receipts(world):
    c = world["client"]
    order, _ = collect(world, create(world))
    handoff = offer(world, order)
    url = f"/v1/collection-orders/{order['id']}/handoffs/{handoff['handoffId']}"
    world["members"]["receiver"]["disabled"] = True
    world["memberfile"].write_text(json.dumps(world["members"]))
    body = {"requestId": "revoked-accept-001", "expectedRevision": handoff["order"]["revision"]}
    assert (
        c.post(url + "/accept", json=body, headers=world["headers"]("receiver")).status_code == 401
    )
    cancelled = c.post(url + "/cancel", json={**body, "requestId": "cancel-request-001"})
    assert cancelled.status_code == 200 and cancelled.json()["order"]["state"] == "open"
    c.put("/v1/devices/manual", json={"siteId": "A", "enabled": False}).raise_for_status()
    assert (
        c.post(
            f"/v1/collection-orders/{order['id']}/close",
            json={
                "requestId": "revoked-device-close",
                "expectedRevision": cancelled.json()["order"]["revision"],
            },
        ).status_code
        == 403
    )
    assert len(c.get(f"/v1/collection-orders/{order['id']}").json()["records"]) == 1


def test_time_missing_sources_record_and_capacity_bounds(world):
    c = world["client"]
    assert (
        c.post(
            "/v1/collection-orders",
            json={**world["body"], "endAt": world["body"]["startAt"] + 7 * 86400 + 1},
        ).status_code
        == 422
    )
    order = create(world)
    request = {
        "requestId": "missing-record-001",
        "expectedRevision": 1,
        "path": "/v1/environment-readings",
        "recordId": str(uuid5(NAMESPACE_URL, "demo:missing-record-001")),
    }
    assert c.post(f"/v1/collection-orders/{order['id']}/records", json=request).status_code == 422
    too_many = {
        "requestId": "too-many-sources",
        "expectedRevision": 1,
        "sources": [{"eventId": "missing", "revision": "0" * 64}] * 101,
    }
    assert c.post(f"/v1/collection-orders/{order['id']}/sources", json=too_many).status_code == 422
    order, _ = collect(world, order)
    with world["app"].state.store.connect() as db:
        order["records"] = [
            {**order["records"][0], "requestId": "synthetic-" + str(i)} for i in range(128)
        ]
        # Capacity fixture only: no accepted receipts or user assets are overwritten.
        for key in ["handoffs", "closed", "serverPersisted"]:
            order.pop(key, None)
        db.execute(
            "UPDATE collection_orders SET payload=? WHERE tenant='demo' AND id=?",
            (canonical(order).decode(), order["id"]),
        )
    assert (
        c.post(
            f"/v1/collection-orders/{order['id']}/records",
            json={**request, "expectedRevision": order["revision"]},
        ).status_code
        == 409
    )


def test_migration_preserves_previous_records_and_is_idempotent(world):
    from marine.collection_orders import migrate

    store = world["app"].state.store
    with store.connect() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM registered_sites")]
    migrate(store)
    migrate(store)
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM registered_sites")] == before
        assert db.execute("SELECT count(*) FROM collection_schema").fetchone()[0] == 1


def test_local_demo_does_not_claim_two_distinct_people(tmp_path, monkeypatch):
    for key in ["MARINE_OIDC_ISSUER", "MARINE_OIDC_JWKS_FILE", "MARINE_MEMBERS_FILE"]:
        monkeypatch.delenv(key, raising=False)
    with TestClient(create_app(tmp_path)) as c:
        actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        assert c.get("/v1/collection-members").json() == {
            "available": False,
            "members": [],
            "truncated": False,
        }


def test_catch_and_batch_receipts_are_verified_from_stored_records(world):
    from datetime import datetime, timezone
    from pathlib import Path

    c = world["client"]
    order, _ = collect(world, create(world))
    fish = json.loads(Path("contracts/examples.json").read_text())[0]
    stamp = (
        datetime.fromtimestamp(world["now"] - 20, timezone.utc).isoformat().replace("+00:00", "Z")
    )
    fish.update(tenantId="demo", siteId="A", deviceId="manual", occurredAt=stamp, receivedAt=stamp)
    assert c.post("/v1/event-batches", json={"events": [fish]}).status_code == 202
    sources = c.get("/v1/source-events", params={"siteId": "A"}).json()["events"]
    attached = c.post(
        f"/v1/collection-orders/{order['id']}/sources",
        json={
            "requestId": "source-both-001",
            "expectedRevision": order["revision"],
            "sources": [
                {"eventId": row["eventId"], "revision": row["revision"]} for row in sources
            ],
        },
    )
    assert attached.status_code == 200, attached.text
    order = attached.json()["order"]
    catch = {
        "requestId": "catch-order-001",
        "siteId": "A",
        "speciesId": fish["data"]["speciesId"],
        "originAt": world["now"] - 1,
        "labelAvailableAt": world["now"],
        "catchKg": 4,
        "effortHours": 1,
        "sourceEventIds": [row["eventId"] for row in sources],
        "dataSource": "synthetic",
    }
    assert c.post("/v1/catches", json=catch).status_code == 201
    linked = c.post(
        f"/v1/collection-orders/{order['id']}/records",
        json={
            "requestId": catch["requestId"],
            "expectedRevision": order["revision"],
            "path": "/v1/catches",
            "recordId": catch["requestId"],
        },
    )
    assert linked.status_code == 200, linked.text
    order = linked.json()["order"]
    batch = {
        "requestId": "batch-order-001",
        "batchId": "batch-workorder-001",
        "speciesId": fish["data"]["speciesId"],
        "massGrams": 1000,
        "areaCode": "A",
    }
    saved = c.post("/v1/batches", json=batch)
    assert saved.status_code in {200, 201}, saved.text
    linked = c.post(
        f"/v1/collection-orders/{order['id']}/records",
        json={
            "requestId": batch["requestId"],
            "expectedRevision": order["revision"],
            "path": "/v1/batches",
            "recordId": batch["batchId"],
        },
    )
    assert linked.status_code == 200, linked.text
    assert {row["kind"] for row in linked.json()["order"]["records"]} == {
        "environment",
        "catch",
        "batch",
    }


def test_list_paginates_without_copying_record_bodies_and_owner_scope_changes(world):
    c = world["client"]
    order = create(world)
    with world["app"].state.store.connect() as db:
        original = json.loads(
            db.execute(
                "SELECT payload FROM collection_orders WHERE tenant='demo' AND id=?", (order["id"],)
            ).fetchone()[0]
        )
        for i in range(30):
            item = {**original, "id": f"order-page-{i:03d}"}
            db.execute(
                "INSERT INTO collection_orders VALUES(?,?,?,?,?,?)",
                ("demo", item["id"], "collector", str(i), canonical(item).decode(), 1),
            )
    first = c.get("/v1/collection-orders").json()
    assert len(first["items"]) == 25 and first["hasMore"]
    assert all("records" not in row and "sources" not in row for row in first["items"])
    second = c.get("/v1/collection-orders", params={"after": first["nextCursor"]}).json()
    assert len(second["items"]) == 6 and not second["hasMore"]
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    world["members"]["collector"]["organization"] = "Org2MSP"
    world["memberfile"].write_text(json.dumps(world["members"]))
    assert (
        c.post(
            f"/v1/collection-orders/{order['id']}/close",
            json={"requestId": "changed-org-close", "expectedRevision": 1},
        ).status_code
        == 403
    )


def test_full_request_history_keeps_room_to_finish_a_pending_handoff(world):
    c = world["client"]
    order, _ = collect(world, create(world))
    handed = offer(world, order)
    with world["app"].state.store.connect() as db:
        count = db.execute(
            "SELECT count(*) FROM collection_requests WHERE tenant='demo' AND order_id=?",
            (order["id"],),
        ).fetchone()[0]
        for index in range(256 - count):
            db.execute(
                "INSERT INTO collection_requests VALUES(?,?,?,?,?,?)",
                ("demo", f"capacity-fixture-{index}", order["id"], "collector", str(index), "{}"),
            )
    response = c.post(
        f"/v1/collection-orders/{order['id']}/handoffs/{handed['handoffId']}/accept",
        json={
            "requestId": "final-accept-capacity",
            "expectedRevision": handed["order"]["revision"],
        },
        headers=world["headers"]("receiver"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["order"]["state"] == "handed-over"
