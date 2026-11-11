"""Synthetic work-order acceptance; no remote pin, ledger or real field data."""

import json
import pytest

from marine.storage import canonical, digest
from test_collection_orders import create, collect
from test_collection_orders import world as world


def ready_order(world):
    order, _ = collect(world, create(world))
    response = world["client"].post(
        f"/v1/collection-orders/{order['id']}/close",
        json={"requestId": "acceptance-close-001", "expectedRevision": order["revision"]},
    )
    assert response.status_code == 200
    return response.json()["order"]


def test_order_acceptance_preview_is_read_only_and_ready_on_its_exact_references(world):
    order = ready_order(world)
    response = world["client"].get(f"/v1/collection-orders/{order['id']}/acceptance-preview")
    assert response.status_code == 200
    assert response.json()["outcome"] == "ready"
    assert response.json()["coverage"]["orderReferencesOnly"] is True
    with world["app"].state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM collection_assessments").fetchone()[0] == 0


def submit(world, order, request="acceptance-submit-001", policy="local"):
    return world["client"].post(
        f"/v1/collection-orders/{order['id']}/acceptance-assessments",
        json={
            "requestId": request,
            "expectedRevision": order["revision"],
            "evidencePolicy": policy,
        },
    )


def decide(
    world, order, saved, decision="approve", subject="auditor", request="acceptance-decision-001"
):
    return world["client"].post(
        f"/v1/collection-orders/{order['id']}/acceptance-assessments/{saved['assessment']['id']}/decisions",
        headers=world["headers"](subject),
        json={
            "requestId": request,
            "expectedRevision": order["revision"],
            "expectedAssessmentVersion": saved["version"],
            "evidencePolicy": saved["assessment"]["evidencePolicy"],
            "decision": decision,
            "reason": "Synthetic independent review; '; DROP TABLE events; --",
        },
    )


def test_independent_review_certificate_and_idempotency_preserve_collection_semantics(world):
    order = ready_order(world)
    first = submit(world, order)
    assert first.status_code == 201
    saved = first.json()
    assert submit(world, order).json()["assessment"]["id"] == saved["assessment"]["id"]
    assert decide(world, order, saved, subject="collector").status_code == 403
    decision = decide(world, order, saved)
    assert decision.status_code == 200, decision.text
    again = decide(world, order, saved)
    assert again.json()["decision"] == decision.json()["decision"]
    assert (
        decide(
            world, order, decision.json(), decision="reject", request="other-decision-001"
        ).status_code
        == 409
    )
    path = f"/v1/collection-orders/{order['id']}/acceptance-assessments/{saved['assessment']['id']}/certificate"
    response = world["client"].get(path)
    assert response.status_code == 200
    packet = response.json()
    assert packet["sha256"] == digest(packet["certificate"]) and packet["current"]["stale"] is False
    assert response.headers["cache-control"] == "no-store"
    assert (
        world["client"].get(f"/v1/collection-orders/{order['id']}").json()["revision"]
        == order["revision"]
    )
    assert world["client"].get(f"/v1/collection-orders/{order['id']}").json()["state"] == "closed"
    for subject in ["stranger", "outside", "viewer"]:
        assert world["client"].get(path, headers=world["headers"](subject)).status_code == 404
    with world["app"].state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM collection_acceptance_decisions").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_self_review_after_role_change_is_rejected_and_revoked_reviewer_cannot_act(world):
    order = ready_order(world)
    saved = submit(world, order).json()
    world["members"]["collector"]["role"] = "auditor"
    world["memberfile"].write_text(json.dumps(world["members"]))
    assert decide(world, order, saved, subject="collector").status_code == 403
    world["members"]["auditor"]["disabled"] = True
    world["memberfile"].write_text(json.dumps(world["members"]))
    assert decide(world, order, saved).status_code in [401, 403]


def test_stale_evidence_blocks_approval_and_export_retains_historical_binding(world):
    order = ready_order(world)
    saved = submit(world, order).json()
    approved = decide(world, order, saved).json()
    path = f"/v1/collection-orders/{order['id']}/acceptance-assessments/{saved['assessment']['id']}"
    original = world["client"].get(path + "/certificate").json()
    with world["app"].state.store.connect() as db:
        row = db.execute("SELECT id,payload FROM events").fetchone()
        event = json.loads(row["payload"])
        event["data"]["sensorQuality"] = "suspect"
        db.execute(
            "UPDATE events SET payload=?,hash=? WHERE tenant=? AND id=?",
            (
                canonical(event).decode(),
                digest({k: v for k, v in event.items() if k != "receivedAt"}),
                "demo",
                row["id"],
            ),
        )
    result = world["client"].get(path).json()
    assert result["current"]["stale"] is True
    historical = world["client"].get(path + "/certificate").json()
    assert (
        historical["sha256"] == original["sha256"]
        and historical["certificate"] == original["certificate"]
        and historical["current"]["stale"] is True
    )
    blocked = submit(world, order, "acceptance-submit-new").json()
    assert blocked["assessment"]["outcome"] == "blocked"
    assert blocked["assessment"]["issues"]["sensorSuspect"] == 1
    assert decide(world, order, blocked, request="acceptance-decide-new").status_code == 409
    assert (
        decide(
            world, order, blocked, decision="reject", request="acceptance-reject-new"
        ).status_code
        == 200
    )
    assert approved["decision"]["decision"] == "approve"


def test_missing_ended_state_scope_and_bounded_history_remain_explicit(world):
    order, _ = collect(world, create(world))
    result = submit(world, order).json()
    assert (
        result["assessment"]["outcome"] == "blocked"
        and result["assessment"]["issues"]["collectionNotEnded"] == 1
    )
    assert decide(world, order, result).status_code == 409
    wrong = world["client"].post(
        f"/v1/collection-orders/{order['id']}/acceptance-assessments",
        json={"requestId": "old-version-001", "expectedRevision": 1},
    )
    assert wrong.status_code == 409
    for index in range(1, 8):
        assert submit(world, order, f"acceptance-history-{index:03}").status_code == 201
    assert submit(world, order, "acceptance-history-full").status_code == 409
    items = (
        world["client"]
        .get(f"/v1/collection-orders/{order['id']}/acceptance-assessments")
        .json()["items"]
    )
    assert len(items) == 8
    assert submit(world, order).status_code == 201
    assert (
        world["client"]
        .get(
            f"/v1/collection-orders/{order['id']}/acceptance-assessments/{items[0]['id']}/certificate"
        )
        .status_code
        == 409
    )


def test_unrelated_quality_faults_do_not_block_exact_order_and_oversized_references_never_pass(
    world,
):
    from test_data_quality import event

    order = ready_order(world)
    bad = event(1, site="A")
    bad["data"]["sensorQuality"] = "invalid"
    world["app"].state.store.accept("demo", [bad])
    path = f"/v1/collection-orders/{order['id']}/acceptance-preview"
    assert world["client"].get(path).json()["outcome"] == "ready"
    with world["app"].state.store.connect() as db:
        source = order["sources"][0]["eventId"]
        raw = json.loads(
            db.execute(
                "SELECT payload FROM events WHERE tenant=? AND id=?", ("demo", source)
            ).fetchone()[0]
        )
        raw["data"]["oversized"] = "x" * 9000
        db.execute(
            "UPDATE events SET payload=? WHERE tenant=? AND id=?", (json.dumps(raw), "demo", source)
        )
    result = world["client"].get(path).json()
    assert result["outcome"] == "blocked" and result["coverage"]["complete"] is False
    assert result["issues"]["documentTooLarge"] >= 1


def test_stricter_policy_requires_existing_batch_evidence_without_making_remote_calls(
    world, monkeypatch
):
    order = ready_order(world)
    monkeypatch.setattr(
        world["app"].state.evidence_store,
        "pin",
        lambda *args: (_ for _ in ()).throw(AssertionError("remote pin called")),
    )
    monkeypatch.setattr(
        world["app"].state.ledger,
        "sync",
        lambda *args: (_ for _ in ()).throw(AssertionError("ledger called")),
    )
    for policy in ["pinned", "committed"]:
        result = submit(world, order, "acceptance-" + policy, policy).json()
        assert (
            result["assessment"]["outcome"] == "blocked"
            and result["assessment"]["issues"]["batchEvidenceRequired"] == 1
        )
        assert result["assessment"]["coverage"]["remoteAvailabilityChecked"] is False


def test_pinned_and_committed_policies_use_existing_scoped_confirmations(world, monkeypatch):
    order, _ = collect(world, create(world))
    client = world["client"]
    batch_id = "acceptance-batch-001"
    body = {
        "requestId": "acceptance-batch-request",
        "batchId": batch_id,
        "speciesId": "saithe",
        "areaCode": "A",
        "massGrams": 1000,
    }
    assert client.post("/v1/batches", json=body).status_code == 201
    linked = client.post(
        f"/v1/collection-orders/{order['id']}/records",
        json={
            "requestId": body["requestId"],
            "expectedRevision": order["revision"],
            "path": "/v1/batches",
            "recordId": batch_id,
        },
    )
    assert linked.status_code == 200
    order = linked.json()["order"]
    order = client.post(
        f"/v1/collection-orders/{order['id']}/close",
        json={"requestId": "acceptance-close-batch", "expectedRevision": order["revision"]},
    ).json()["order"]
    uploaded = client.post(
        f"/v1/batches/{batch_id}/evidence",
        files={"file": ("synthetic.json", b'{"synthetic":"evidence"}', "application/json")},
    )
    assert uploaded.status_code in [200, 201], uploaded.text
    evidence_id = uploaded.json()["evidenceId"]
    monkeypatch.setattr(
        world["app"].state.evidence_store,
        "pin",
        lambda *a: (_ for _ in ()).throw(AssertionError("remote called")),
    )
    monkeypatch.setattr(
        world["app"].state.ledger,
        "sync",
        lambda *a: (_ for _ in ()).throw(AssertionError("ledger called")),
    )
    assert (
        submit(world, order, "acceptance-local-batch", "local").json()["assessment"]["outcome"]
        == "ready"
    )
    assert (
        submit(world, order, "acceptance-pinned-missing", "pinned").json()["assessment"]["outcome"]
        == "blocked"
    )
    with world["app"].state.store.connect() as db:
        manifest = json.loads(
            db.execute(
                "SELECT manifest FROM evidence WHERE tenant=? AND id=?", ("demo", evidence_id)
            ).fetchone()[0]
        )
        manifest["cipherCid"] = "synthetic-persisted-pin"
        db.execute(
            "UPDATE evidence SET manifest=?,status='PIN_VERIFIED' WHERE tenant=? AND id=?",
            (canonical(manifest).decode(), "demo", evidence_id),
        )
    pinned = submit(world, order, "acceptance-pinned-ready", "pinned").json()
    assert pinned["assessment"]["outcome"] == "ready"
    assert (
        submit(world, order, "acceptance-commit-missing", "committed").json()["assessment"][
            "outcome"
        ]
        == "blocked"
    )
    with world["app"].state.store.connect() as db:
        db.execute(
            "UPDATE evidence SET status='COMMITTED',receipt=? WHERE tenant=? AND id=?",
            (
                canonical({"validationCode": "VALID", "txId": "synthetic-receipt"}).decode(),
                "demo",
                evidence_id,
            ),
        )
        db.execute(
            "UPDATE ledger_jobs SET status='committed',receipt=? WHERE tenant=? AND batch=?",
            (canonical({"txId": "synthetic-batch-receipt"}).decode(), "demo", batch_id),
        )
    committed = submit(world, order, "acceptance-commit-ready", "committed").json()
    assert committed["assessment"]["outcome"] == "ready"
    assert decide(world, order, committed).status_code == 200
    # Persisted-state acceptance does not claim current remote availability.
    assert committed["assessment"]["coverage"]["remoteAvailabilityChecked"] is False


@pytest.mark.parametrize("table,identifier", [("registered_sites", "A"), ("devices", "manual")])
def test_null_legacy_registration_is_a_bounded_blocker_and_history_remains_readable(
    world, table, identifier
):
    order = ready_order(world)
    saved = submit(world, order).json()
    pending = submit(world, order, "acceptance-pending-null").json()
    assert decide(world, order, saved).status_code == 200
    path = f"/v1/collection-orders/{order['id']}"
    certificate = (
        world["client"]
        .get(path + "/acceptance-assessments/" + saved["assessment"]["id"] + "/certificate")
        .json()
    )
    with world["app"].state.store.connect() as db:
        db.execute(f"UPDATE {table} SET payload=NULL WHERE tenant=? AND id=?", ("demo", identifier))
    preview = world["client"].get(path + "/acceptance-preview")
    assert preview.status_code == 200 and preview.json()["outcome"] == "blocked"
    assert preview.json()["issues"]["documentInvalid"] == 1
    assert decide(world, order, pending, request="acceptance-null-review").status_code == 409
    new = submit(world, order, "acceptance-null-submission")
    assert new.status_code == 201 and new.json()["assessment"]["outcome"] == "blocked"
    retained = world["client"].get(
        path + "/acceptance-assessments/" + saved["assessment"]["id"] + "/certificate"
    )
    assert (
        retained.status_code == 200
        and retained.json()["sha256"] == certificate["sha256"]
        and retained.json()["current"]["stale"] is True
    )
