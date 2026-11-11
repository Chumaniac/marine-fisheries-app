import json
import httpx
import tools.commission as commission


def test_synthetic_dependency_disconnect_and_restore_never_grants_acceptance(tmp_path, monkeypatch):
    state = {"available": True}

    class Broker:
        def __init__(self, **kwargs):
            pass

        def list_topics(self):
            if not state["available"]:
                raise ConnectionError("SYNTHETIC_PRIVATE_DEPENDENCY")
            return commission.REQUIRED_TOPICS

        def close(self):
            pass

    class Ledger:
        def query(self, *args):
            return {}

    def handler(request):
        if request.url.path == "/ready":
            return httpx.Response(200 if state["available"] else 503)
        if request.url.path == "/jobs/overview":
            return httpx.Response(
                200, json={"jobs": [{"name": "marine-event-time-windows", "state": "RUNNING"}]}
            )
        return httpx.Response(200, json={"ID": f"synthetic-node-{request.url.port}"})

    client_type = httpx.Client
    monkeypatch.setattr(
        commission.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(commission, "KafkaAdminClient", Broker)
    monkeypatch.setattr(commission, "Fabric", Ledger)
    config = tmp_path / "kafka.json"
    config.write_text("{}")
    for key, value in {
        "MARINE_MODE": "identity-test",
        "KAFKA_CLIENT_CONFIG_FILE": str(config),
        "KAFKA_BOOTSTRAP": "127.0.0.1:1",
        "IPFS_API": "http://127.0.0.1:1",
        "IPFS_REPLICA_APIS": '["http://127.0.0.1:2"]',
        "MARINE_IPFS_MIN_REPLICAS": "2",
    }.items():
        monkeypatch.setenv(key, value)
    for available in (True, False, True):
        state["available"] = available
        report = commission.verify_dependencies("http://127.0.0.1:1", "http://127.0.0.1:2")
        assert report["ready"] is available and report["dependencyChecksPassed"] is available
        assert report["scope"] == "dependency-connectivity-only"
        for key in (
            "businessAcceptanceVerified",
            "fieldAccepted",
            "automaticProductionCutover",
            "productionDeploymentExecuted",
        ):
            assert report[key] is False
        assert "SYNTHETIC_PRIVATE_DEPENDENCY" not in json.dumps(report)
