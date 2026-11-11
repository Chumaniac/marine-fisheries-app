"""Explicit reuse of settled receipts; no repeated business admission."""

import copy
from test_collection_orders import world as world
from test_collection_orders import create, collect


def target(world, identifier="workorder-reuse-002"):
    response = world["client"].post(
        "/v1/collection-orders", json={**world["body"], "id": identifier}
    )
    assert response.status_code == 201
    return response.json()


def request(source, record, order, **changes):
    return {
        "requestId": "reuse-link-request-001",
        "expectedRevision": order["revision"],
        "sourceOrderId": source["id"],
        "sourceRequestId": record["requestId"],
        "path": record["path"],
        "recordId": record["recordId"],
        "evidenceHash": source["records"][0]["evidenceHash"],
        "includeSources": True,
        **changes,
    }


def test_existing_receipt_can_be_reused_with_a_new_association_key_and_originals_stay_immutable(
    world,
):
    c = world["client"]
    source, record = collect(world, create(world))
    original = copy.deepcopy(source)
    order = target(world)
    # The legacy key is a business receipt ID and already names the old association.
    assert (
        c.post(
            f"/v1/collection-orders/{order['id']}/records", json={**record, "expectedRevision": 1}
        ).status_code
        == 409
    )
    projection = c.get(f"/v1/collection-orders/{source['id']}/record-receipts")
    assert projection.status_code == 200
    assert "handoffs" not in projection.json() and projection.json()["records"] == source["records"]
    payload = request(source, record, order)
    url = f"/v1/collection-orders/{order['id']}/records/reuse"
    response = c.post(url, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    saved = result["order"]
    assert len(saved["records"]) == 1 and len(saved["sources"]) == 1
    assert saved["records"][0]["requestId"] == record["requestId"]
    assert saved["records"][0]["associationRequestId"] == payload["requestId"]
    assert saved["records"][0]["reusedFromOrderId"] == source["id"]
    assert c.post(url, json=payload).json()["duplicate"] is True
    assert c.get(f"/v1/collection-orders/{source['id']}").json() == original
    with world["app"].state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    closed = c.post(
        f"/v1/collection-orders/{saved['id']}/close",
        json={"requestId": "reuse-close-request", "expectedRevision": saved["revision"]},
    )
    assert closed.status_code == 200
    assert (
        c.get(f"/v1/collection-orders/{saved['id']}/acceptance-preview").json()["outcome"]
        == "ready"
    )


def test_reuse_rechecks_permissions_source_binding_and_target_state(world):
    c = world["client"]
    source, record = collect(world, create(world))
    order = target(world)
    url = f"/v1/collection-orders/{order['id']}/records/reuse"
    payload = request(source, record, order)
    for user in ["outside", "viewer", "stranger"]:
        assert c.post(url, json=payload, headers=world["headers"](user)).status_code in [403, 404]
    before = c.get(f"/v1/collection-orders/{order['id']}").json()
    for change in [
        {"evidenceHash": "f" * 64},
        {"sourceOrderId": order["id"]},
        {"sourceRequestId": "unknown-receipt"},
        {"recordId": "wrong-record"},
    ]:
        assert c.post(url, json={**payload, **change}).status_code in [409, 422]
        assert c.get(f"/v1/collection-orders/{order['id']}").json() == before
    c.post(
        f"/v1/collection-orders/{order['id']}/close",
        json={"requestId": "close-empty-target", "expectedRevision": 1},
    ).raise_for_status()
    assert c.post(url, json={**payload, "expectedRevision": 2}).status_code == 409


def test_reuse_without_source_copy_requires_already_linked_sources_and_keeps_legacy_retries(world):
    c = world["client"]
    source, record = collect(world, create(world))
    order = target(world)
    payload = request(source, record, order, includeSources=False)
    assert (
        c.post(f"/v1/collection-orders/{order['id']}/records/reuse", json=payload).status_code
        == 422
    )
    duplicate = c.post(f"/v1/collection-orders/{source['id']}/records", json=record)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]


def test_changed_source_receipt_is_rejected_and_copied_references_roll_back(world):
    import json
    from marine.storage import canonical, digest

    c = world["client"]
    source, record = collect(world, create(world))
    order = target(world)
    with world["app"].state.store.connect() as db:
        raw = db.execute("SELECT payload FROM events WHERE id=?", (record["recordId"],)).fetchone()
        event = json.loads(raw[0])
        event["data"]["temperatureC"] = 21
        db.execute(
            "UPDATE events SET payload=?,hash=? WHERE id=?",
            (
                canonical(event).decode(),
                digest({key: value for key, value in event.items() if key != "receivedAt"}),
                record["recordId"],
            ),
        )
    before = c.get(f"/v1/collection-orders/{order['id']}").json()
    response = c.post(
        f"/v1/collection-orders/{order['id']}/records/reuse", json=request(source, record, order)
    )
    assert response.status_code == 409
    assert c.get(f"/v1/collection-orders/{order['id']}").json() == before


def test_reuse_keeps_per_order_uniqueness_and_request_identity_guards(world):
    c = world["client"]
    source, record = collect(world, create(world))
    order = target(world)
    payload = request(source, record, order)
    url = f"/v1/collection-orders/{order['id']}/records/reuse"
    response = c.post(url, json=payload)
    assert response.status_code == 200
    assert c.post(url, json={**payload, "includeSources": False}).status_code == 409
    assert (
        c.post(
            url, json={**payload, "requestId": "second-link-request", "expectedRevision": 2}
        ).status_code
        == 409
    )
    assert len(c.get(f"/v1/collection-orders/{order['id']}").json()["records"]) == 1
