from marine.api import create_app
from fastapi.testclient import TestClient
from marine.config import preflight


def test_incident_acknowledgement_does_not_hide_failure(tmp_path, monkeypatch):
    from threading import Event
    from marine import operations

    initialized = Event()
    maintenance = operations.maintenance

    def observe_initial_pass(app):
        try:
            return maintenance(app)
        finally:
            initialized.set()

    monkeypatch.setattr(operations, "maintenance", observe_initial_pass)
    app = create_app(tmp_path)
    with TestClient(app) as c:
        # Do not race the healthy startup pass against the injected failed heartbeat.
        assert initialized.wait(timeout=5), "initial health pass did not complete"
        actor = c.post("/v1/session", headers={"X-Requested-With": "marine-web"}).json()
        c.headers["X-Marine-Scope"] = actor["scopeHash"]
        app.state.operations.heartbeat("forecast", False)
        assert c.get("/ready").status_code == 503
        report = c.get("/v1/operations").json()
        item = next(r for r in report["incidents"] if r["code"] == "forecast_unhealthy")
        assert (
            c.post(
                "/v1/incidents/" + item["id"] + "/acknowledge",
                json={"note": "Investigating failed scheduler"},
            ).status_code
            == 200
        )
        assert c.get("/ready").status_code == 503
        app.state.operations.heartbeat("forecast", True)
        result = c.get("/v1/operations").json()
        assert next(r for r in result["incidents"] if r["id"] == item["id"])["status"] == "RESOLVED"
        assert c.get("/ready").status_code == 200
        assert (
            c.post("/v1/ledger/jobs/unknown/retry", json={"note": "check receipt"}).status_code
            == 404
        )


def test_preflight_fails_closed_and_never_echoes_values():
    result = preflight(
        {
            "MARINE_MODE": "production",
            "MARINE_PUBLIC_URL": "http://secret-user:secret-pass@example.test",
        }
    )
    assert result["ready"] is False
    assert "secret-pass" not in str(result)
    assert any(r["setting"] == "deployment.single_writer" and not r["ok"] for r in result["checks"])


def test_second_writer_is_fenced_and_released(tmp_path):
    import pytest
    from marine.writer_lease import WriterLease

    first = WriterLease(tmp_path)
    with pytest.raises(RuntimeError):
        WriterLease(tmp_path)
    first.close()
    second = WriterLease(tmp_path)
    second.close()


def test_busy_ledger_does_not_refresh_success_and_tenant_failure_recovers(tmp_path, monkeypatch):
    import time
    from marine.operations import maintenance
    from marine import edge_samples

    app = create_app(tmp_path)
    monkeypatch.setenv("MARINE_FABRIC_ROOT", "synthetic-config-only")
    with app.state.store.connect() as db:
        db.execute(
            "INSERT INTO ledger_jobs(tenant,id,batch,operation,payload,actor,status) VALUES('demo','job','batch','create','{}','Org1MSP','pending')"
        )
        db.execute("INSERT INTO component_status VALUES('ledger',1,?)", (time.time() - 400,))
        db.execute("INSERT INTO events VALUES('demo','event','{}','hash',?)", (time.time(),))
    app.state.ledger.sync = lambda tenant: {"status": "busy"}
    with app.state.store.connect() as db:
        before = db.execute("SELECT checked FROM component_status WHERE name='ledger'").fetchone()[
            0
        ]
    original = edge_samples.release_reviews

    def fail(*args):
        raise RuntimeError("synthetic tenant error")

    monkeypatch.setattr(edge_samples, "release_reviews", fail)
    maintenance(app)
    assert app.state.scheduler_error is True
    with app.state.store.connect() as db:
        assert (
            db.execute("SELECT checked FROM component_status WHERE name='ledger'").fetchone()[0]
            == before
        )
    assert not app.state.operations.snapshot("demo")["checks"]["ledger"]
    monkeypatch.setattr(edge_samples, "release_reviews", original)
    maintenance(app)
    assert app.state.scheduler_error is False


def test_unconfirmed_dispatch_is_visible_to_health_and_never_silently_retried(tmp_path):
    import time

    app = create_app(tmp_path)
    with app.state.store.connect() as db:
        db.execute(
            "INSERT INTO outbox(tenant,id,topic,payload,status,attempt,next) VALUES('demo','stale','synthetic','{}','dispatching',1,?)",
            (time.time() - 1,),
        )
    report = app.state.operations.snapshot("demo")
    assert report["actionRequiredEvents"] == 1 and report["checks"]["outbox"] is False
