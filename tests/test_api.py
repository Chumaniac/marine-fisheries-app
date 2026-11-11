import copy
import json
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marine.api import create_app
from marine.storage import canonical


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert (
            client.post("/v1/session", headers={"X-Requested-With": "marine-web"}).status_code
            == 200
        )
        client.headers["X-Marine-Scope"] = client.get("/v1/me").json()["scopeHash"]
        yield client


def test_auth_origin_and_host_boundaries(tmp_path):
    with TestClient(create_app(tmp_path)) as c:
        assert c.get("/v1/dashboard").status_code == 401
        assert c.post("/v1/session").status_code == 403
        assert (
            c.post(
                "/v1/session",
                headers={"origin": "https://evil.example", "X-Requested-With": "marine-web"},
            ).status_code
            == 403
        )
        assert c.get("/health", headers={"host": "evil.example"}).status_code == 403


def test_api_version_matches_selected_source(client):
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert client.app.version == project["project"]["version"]


def test_event_resend_not_duplicated_and_conflict_rejected(client):
    e = json.loads(Path("contracts/examples.json").read_text())[0]
    e["tenantId"] = "demo"
    e["occurredAt"] = "2026-01-01T00:00:00Z"
    assert client.post("/v1/event-batches", json={"events": [e]}).status_code == 202
    assert client.post("/v1/event-batches", json={"events": [e]}).status_code == 202
    assert len(client.app.state.store.events("demo")) == 1
    e["data"]["confidence"] = 0.2
    assert client.post("/v1/event-batches", json={"events": [e]}).status_code == 409
    e["tenantId"] = "someone-else"
    assert client.post("/v1/event-batches", json={"events": [e]}).status_code == 403


def test_batch_version_idempotency_and_tenant_isolation(client):
    body = {
        "requestId": "request-create-01",
        "batchId": "batch-01",
        "speciesId": "saithe",
        "massGrams": 1000,
        "areaCode": "A",
    }
    first = client.post("/v1/batches", json=body)
    assert first.status_code == 201
    assert client.post("/v1/batches", json=body).json() == first.json()
    bad = copy.deepcopy(body)
    bad["massGrams"] = 2000
    assert client.post("/v1/batches", json=bad).status_code == 409
    move = {"requestId": "request-move-01", "expectedRevision": 1, "nextState": "LANDED"}
    assert client.post("/v1/batches/batch-01/transitions", json=move).status_code == 200
    move["requestId"] = "request-move-02"
    assert client.post("/v1/batches/batch-01/transitions", json=move).status_code == 409
    with client.app.state.store.connect() as db:
        db.execute(
            "INSERT INTO batches VALUES(?,?,?,?)",
            ("other", "secret", canonical({"id": "secret"}).decode(), 1),
        )
    assert client.get("/v1/batches/secret/trace").status_code == 404
    bad = {**body, "requestId": "sql-injection-01", "batchId": "'; DROP TABLE batches;--"}
    assert client.post("/v1/batches", json=bad).status_code == 422
    assert client.get("/v1/batches/batch-01/trace").status_code == 200


def test_evidence_encryption_tampering_and_unavailable_pin(client):
    client.post(
        "/v1/batches",
        json={
            "requestId": "request-evidence-01",
            "batchId": "b",
            "speciesId": "saithe",
            "massGrams": 1000,
            "areaCode": "A",
        },
    )
    result = client.post(
        "/v1/batches/b/evidence", files={"file": ("sample.txt", b"private synthetic evidence")}
    )
    assert result.status_code == 201
    identity = result.json()["evidenceId"]
    again = client.post(
        "/v1/batches/b/evidence", files={"file": ("sample.txt", b"private synthetic evidence")}
    )
    assert again.json()["evidenceId"] == identity
    assert client.get(f"/v1/evidence/{identity}/verify").json()["contentVerified"] is True
    assert client.post(f"/v1/evidence/{identity}/pin").status_code == 503
    obj = next(client.app.state.evidence_store.folder.iterdir())
    assert b"private synthetic evidence" not in obj.read_bytes()
    obj.write_bytes(b"tampered")
    assert client.get(f"/v1/evidence/{identity}/verify").json()["contentVerified"] is False


def test_untrained_forecast_is_absent_not_fake(client):
    result = client.get("/v1/dashboard").json()
    assert result["prediction"] is None
    assert result["stale"] is True
    assert client.post("/v1/forecast-models").status_code == 422


@pytest.mark.parametrize("noise_site,noise_species", [("other", "saithe"), ("target", "other")])
def test_dashboard_filters_before_event_and_window_limits(client, noise_site, noise_species):
    store = client.app.state.store
    event = json.loads(Path("contracts/examples.json").read_text())[0]
    event.update(tenantId="demo", siteId="target", eventId="target-event")
    event["data"]["speciesId"] = "saithe"
    store.accept("demo", [event])
    noise = []
    for index in range(1001):
        item = copy.deepcopy(event)
        item.update(eventId=f"noise-{index}", siteId=noise_site)
        item["data"]["speciesId"] = noise_species
        noise.append(item)
    store.accept("demo", noise)
    window = {
        "siteId": "target",
        "speciesId": "saithe",
        "windowStart": 1000,
        "windowEnd": 2000,
        "observationCount": 1,
        "trackCount": 1,
    }
    with store.connect() as db:
        db.execute(
            "INSERT INTO windows VALUES(?,?,?,?)", ("demo", "a-target", json.dumps(window), 1)
        )
        db.executemany(
            "INSERT INTO windows VALUES(?,?,?,?)",
            [
                (
                    "demo",
                    f"z-noise-{index}",
                    json.dumps({**window, "siteId": noise_site, "speciesId": noise_species}),
                    1,
                )
                for index in range(1001)
            ],
        )
        db.execute(
            "INSERT INTO windows VALUES(?,?,?,?)", ("other", "other-tenant", json.dumps(window), 1)
        )
    response = client.get("/v1/dashboard", params={"siteId": "target", "speciesId": "saithe"})
    assert response.status_code == 200
    data = response.json()
    assert data["observationCount"] == 1
    assert sum(row["count"] for row in data["series"]) == 1
    assert data["streamWindows"] == 1 and len(data["heatmap"]) == 1
    assert data["lastEventAt"] is not None
    assert (
        client.get("/v1/dashboard", params={"siteId": "target' OR 1=1 --"}).json()[
            "observationCount"
        ]
        == 0
    )


def test_source_event_selection_reaches_a_quieter_site(client):
    store = client.app.state.store
    event = json.loads(Path("contracts/examples.json").read_text())[0]
    event.update(tenantId="demo", siteId="quiet", eventId="quiet-event")
    store.accept("demo", [event])
    store.accept(
        "demo", [{**event, "siteId": "busy", "eventId": f"busy-{index}"} for index in range(5001)]
    )
    result = client.get("/v1/source-events", params={"siteId": "quiet"})
    assert result.status_code == 200
    assert [row["eventId"] for row in result.json()["events"]] == ["quiet-event"]


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_duplicate_evidence_does_not_report_success_for_a_damaged_original(client, damage):
    client.post(
        "/v1/batches",
        json={
            "requestId": "duplicate-evidence",
            "batchId": "original",
            "speciesId": "saithe",
            "massGrams": 1000,
            "areaCode": "A",
        },
    )
    files = {"file": ("synthetic.txt", b"synthetic evidence")}
    original = client.post("/v1/batches/original/evidence", files=files)
    assert original.status_code == 201
    obj = next(client.app.state.evidence_store.folder.iterdir())
    if damage == "corrupt":
        obj.write_bytes(b"damaged synthetic ciphertext")
    else:
        obj.unlink()
    repeated = client.post("/v1/batches/original/evidence", files=files)
    assert repeated.status_code == 409
    assert "integrity" in repeated.json()["detail"].lower()
    with client.app.state.store.connect() as db:
        rows = db.execute("SELECT id,status FROM evidence WHERE tenant=?", ("demo",)).fetchall()
    assert len(rows) == 1 and rows[0]["id"] == original.json()["evidenceId"]
    assert rows[0]["status"] == original.json()["status"]
    if damage == "corrupt":
        assert obj.read_bytes() == b"damaged synthetic ciphertext"


def test_cookie_account_switch_rejects_prior_queue_scope(tmp_path, monkeypatch):
    import time
    import jwt

    key = b"synthetic-test-only-signing-key-0000000000000000"
    keyfile = tmp_path / "test-signing.key"
    keyfile.write_bytes(key)
    monkeypatch.setenv("MARINE_JWT_KEY_FILE", str(keyfile))
    with TestClient(create_app(tmp_path / "scoped")) as c:

        def token(tenant):
            now = int(time.time())
            return jwt.encode(
                {
                    "sub": "user-" + tenant,
                    "tenant": tenant,
                    "role": "operator",
                    "iss": "marine-local",
                    "aud": "marine-api",
                    "iat": now,
                    "exp": now + 1000,
                },
                key,
                algorithm="HS256",
            )

        c.cookies.set("marine_session", token("A"))
        scope = c.get("/v1/me").json()["scopeHash"]
        c.headers["X-Marine-Scope"] = scope
        body = {
            "requestId": "switch-test-001",
            "batchId": "owned-A",
            "speciesId": "saithe",
            "massGrams": 1000,
            "areaCode": "A",
        }
        assert c.post("/v1/batches", json=body).status_code == 201
        c.cookies.clear()
        c.cookies.set("marine_session", token("B"))
        assert c.post("/v1/batches", json=body).status_code == 403
        with c.app.state.store.connect() as db:
            assert db.execute("SELECT count(*) FROM batches WHERE tenant='B'").fetchone()[0] == 0


def test_field_label_cannot_reference_future_observation(client):
    from test_forecast import labels

    e = json.loads(Path("contracts/examples.json").read_text())[0]
    e.update(tenantId="demo", occurredAt="2026-01-01T00:00:00Z")
    assert client.post("/v1/event-batches", json={"events": [e]}).status_code == 202
    label = {
        **labels()[1],
        "id": "field-temporal-check",
        "siteId": e["siteId"],
        "speciesId": "saithe",
        "sourceEventIds": [e["eventId"]],
        "dataSource": "field",
    }
    assert client.post("/v1/labels", json={"labels": [label]}).status_code == 422


def test_dashboard_counts_are_not_truncated_to_latest_thousand(client):
    from datetime import datetime, timezone

    template = json.loads(Path("contracts/examples.json").read_text())[0]
    events = []
    for index in range(1105):
        event = copy.deepcopy(template)
        event.update(
            tenantId="demo",
            eventId=f"synthetic-event-{index}",
            occurredAt=datetime.now(timezone.utc).isoformat(),
        )
        event["data"].update(speciesId="saithe", trackId=f"track-{index}", countMethod="track")
        events.append(event)
    client.app.state.store.accept("demo", events)
    data = client.get(
        "/v1/dashboard", params={"siteId": template["siteId"], "speciesId": "saithe"}
    ).json()
    assert data["observationCount"] == 1105
    assert data["trackCount"] == 1105
    assert sum(row["count"] for row in data["series"]) == 1105


def test_outbox_manual_retry_is_scoped_audited_and_preserves_request(client):
    with client.app.state.store.connect() as db:
        for tenant in ("demo", "other"):
            db.execute(
                "INSERT INTO outbox(tenant,id,topic,payload,status,attempt) VALUES(?,?,?,?,?,?)",
                (tenant, f"{tenant}-event", "synthetic", '{"siteId":"s"}', "action_required", 8),
            )
    page = client.get("/v1/outbox").json()
    assert [item["id"] for item in page["items"]] == ["demo-event"]
    assert (
        client.post(
            "/v1/outbox/other-event/retry", json={"note": "Reviewed synthetic outage"}
        ).status_code
        == 409
    )
    assert client.post(
        "/v1/outbox/demo-event/retry", json={"note": "Reviewed synthetic outage"}
    ).json()["requestPreserved"]
    with client.app.state.store.connect() as db:
        row = db.execute("SELECT * FROM outbox WHERE tenant='demo'").fetchone()
        assert (
            row["status"] == "pending"
            and row["attempt"] == 0
            and row["payload"] == '{"siteId":"s"}'
        )
        assert (
            db.execute(
                "SELECT count(*) FROM operator_actions WHERE operation='retry-original-event'"
            ).fetchone()[0]
            == 1
        )


def test_trace_pages_all_collections_and_keeps_global_status(client):
    body = {
        "requestId": "trace-page-create",
        "batchId": "paged",
        "speciesId": "saithe",
        "massGrams": 1000,
        "areaCode": "A",
    }
    assert client.post("/v1/batches", json=body).status_code == 201
    with client.app.state.store.connect() as db:
        for i in range(205):
            db.execute(
                "INSERT INTO evidence VALUES(?,?,?,?,?,?)",
                ("demo", f"e-{i}", "paged", "{}", "COMMITTED" if i == 204 else "UPLOADED", None),
            )
            db.execute(
                "INSERT INTO cold_issues VALUES(?,?,?,?,?,?)",
                ("demo", f"c-{i}", "paged", "{}", "OPEN", None),
            )
            db.execute(
                "INSERT INTO ledger_jobs(tenant,id,batch,operation,status) VALUES(?,?,?,?,?)",
                ("demo", f"l-{i}", "paged", "synthetic", "pending"),
            )
    first = client.get("/v1/batches/paged/trace").json()
    assert first["batch"]["chainStatus"] == "EVIDENCE_ANCHORED"
    assert first["totalEvidence"] == 205 and first["openColdIssues"] == 205
    for key, query, cursor, more in [
        ("evidence", "evidenceAfter", "nextEvidenceCursor", "hasMoreEvidence"),
        ("coldIssues", "issueAfter", "nextIssueCursor", "hasMoreColdIssues"),
        ("ledgerCommands", "ledgerAfter", "nextLedgerCursor", "hasMoreLedgerCommands"),
    ]:
        page = first
        seen = list(page[key])
        while page[more]:
            page = client.get("/v1/batches/paged/trace", params={query: page[cursor]}).json()
            assert len(page[key]) <= 100
            seen.extend(page[key])
        assert len(seen) == (206 if key == "ledgerCommands" else 205)
    assert client.get("/v1/batches/paged/trace", params={"evidenceAfter": -1}).status_code == 422


def test_reviewed_history_does_not_hide_new_queued_sample(client):
    with client.app.state.store.connect() as db:
        for i in range(1005):
            db.execute(
                "INSERT INTO reviews VALUES(?,?,?)",
                ("demo", f"old-{i}", json.dumps({"id": f"old-{i}", "state": "reviewed"})),
            )
        queued = {
            "id": "new-sample",
            "state": "queued",
            "revision": 1,
            "prediction": {"probabilities": {"saithe": 0.5, "salmonid": 0.5}},
            "embedding": [1.0, 0.0],
        }
        db.execute("INSERT INTO reviews VALUES(?,?,?)", ("demo", "new-sample", json.dumps(queued)))
    data = client.get("/v1/reviews").json()
    assert data["queued"] == 1 and data["reviewed"] == 1005
    assert data["items"][0]["id"] == "new-sample"


def test_source_events_have_scoped_pages(client):
    template = json.loads(Path("contracts/examples.json").read_text())[0]
    events = []
    for i in range(505):
        event = copy.deepcopy(template)
        event.update(tenantId="demo", eventId=f"source-page-{i}")
        events.append(event)
    client.app.state.store.accept("demo", events)
    first = client.get("/v1/source-events", params={"siteId": template["siteId"]}).json()
    second = client.get(
        "/v1/source-events", params={"siteId": template["siteId"], "before": first["nextCursor"]}
    ).json()
    assert len(first["events"]) == 500 and first["hasMore"]
    assert len(second["events"]) == 5 and not second["hasMore"]
    assert len({e["eventId"] for e in first["events"] + second["events"]}) == 505


def test_dashboard_batch_pagination_reaches_all_rows_and_goes_back(client):
    with client.app.state.store.connect() as db:
        for index in range(201):
            batch = {
                "batchId": f"paged-{index:03d}",
                "state": "CREATED",
                "revision": 1,
                "speciesId": "saithe",
                "massGrams": 1000,
                "ownerMSP": "Org1MSP",
            }
            db.execute(
                "INSERT INTO batches VALUES(?,?,?,?)",
                ("demo", batch["batchId"], canonical(batch).decode(), 1),
            )
        db.execute(
            "INSERT INTO batches VALUES(?,?,?,?)",
            ("other", "paged-secret", canonical({"batchId": "PRIVATE_OTHER_TENANT"}).decode(), 1),
        )
    first = client.get("/v1/dashboard").json()
    assert len(first["batches"]) == 100 and first["previousBatchCursor"] is None
    second = client.get("/v1/dashboard", params={"batchAfter": first["nextBatchCursor"]}).json()
    third = client.get("/v1/dashboard", params={"batchAfter": second["nextBatchCursor"]}).json()
    assert len(second["batches"]) == 100 and len(third["batches"]) == 1
    assert third["nextBatchCursor"] is None and not third["hasMoreBatches"]
    ids = [batch["batchId"] for page in [first, second, third] for batch in page["batches"]]
    assert ids == [f"paged-{index:03d}" for index in range(201)]
    assert "PRIVATE_OTHER_TENANT" not in json.dumps([first, second, third])
    back = client.get("/v1/dashboard", params={"batchBefore": third["previousBatchCursor"]}).json()
    assert back["batches"] == second["batches"]
    with client.app.state.store.connect() as db:
        batch = {**first["batches"][0], "batchId": "aaa-new"}
        db.execute(
            "INSERT INTO batches VALUES(?,?,?,?)", ("demo", "aaa-new", canonical(batch).decode(), 1)
        )
    stable = client.get("/v1/dashboard", params={"batchAfter": first["nextBatchCursor"]}).json()
    assert stable["batches"] == second["batches"]


def test_dashboard_batch_pagination_validation_and_literal_cursor(client):
    both = client.get("/v1/dashboard", params={"batchAfter": "a", "batchBefore": "b"})
    assert both.status_code == 422
    assert client.get("/v1/dashboard", params={"batchAfter": "x" * 129}).status_code == 422
    response = client.get("/v1/dashboard", params={"batchAfter": "'; DROP TABLE batches;--"})
    assert response.status_code == 200 and response.json()["batches"] == []
    assert client.get("/v1/dashboard").status_code == 200


def test_unknown_outbox_delivery_requires_scoped_versioned_reconciliation(client):
    with client.app.state.store.connect() as db:
        for tenant in ("demo", "other"):
            db.execute(
                "INSERT INTO outbox(tenant,id,topic,payload,status,attempt,error_code) VALUES(?,?,?,'{\"siteId\":\"s\"}','outcome_unknown',1,'ACK_LOCAL_COMMIT_FAILED')",
                (tenant, tenant + "-unknown", "synthetic"),
            )
    item = next(i for i in client.get("/v1/outbox").json()["items"] if i["id"] == "demo-unknown")
    assert (
        item["status"] == "outcome_unknown"
        and item["failureCode"] == "ACK_LOCAL_COMMIT_FAILED"
        and "payload" not in item
    )
    assert (
        client.post("/v1/outbox/demo-unknown/retry", json={"note": "Synthetic check"}).status_code
        == 409
    )
    body = {
        "note": "Checked synthetic receiver record",
        "outcome": "published",
        "expectedAttempt": 1,
    }
    assert client.post("/v1/outbox/other-unknown/reconcile", json=body).status_code == 409
    assert (
        client.post(
            "/v1/outbox/demo-unknown/reconcile", json={**body, "expectedAttempt": 2}
        ).status_code
        == 409
    )
    assert (
        client.post("/v1/outbox/demo-unknown/reconcile", json=body).json()["status"] == "published"
    )
    assert client.post("/v1/outbox/demo-unknown/reconcile", json=body).status_code == 409
    with client.app.state.store.connect() as db:
        row = db.execute("SELECT * FROM outbox WHERE tenant='demo'").fetchone()
        assert row["payload"] == '{"siteId":"s"}' and row["status"] == "published"
        db.execute("UPDATE outbox SET status='outcome_unknown',attempt=2 WHERE tenant='demo'")
    absent = {**body, "outcome": "not-published", "expectedAttempt": 2}
    assert (
        client.post("/v1/outbox/demo-unknown/reconcile", json=absent).json()["status"]
        == "action_required"
    )
    assert client.post(
        "/v1/outbox/demo-unknown/retry",
        json={"note": "Authorized original retry after receiver check"},
    ).json()["requestPreserved"]


def test_blank_operator_retry_note_is_rejected_without_mutating_request(client):
    with client.app.state.store.connect() as db:
        db.execute(
            "INSERT INTO outbox(tenant,id,topic,payload,status,attempt) VALUES('demo','blank-note','synthetic','original payload','action_required',8)"
        )
    assert client.post("/v1/outbox/blank-note/retry", json={"note": "   "}).status_code == 422
    with client.app.state.store.connect() as db:
        row = db.execute("SELECT * FROM outbox WHERE id='blank-note'").fetchone()
        assert row["status"] == "action_required" and row["payload"] == "original payload"
