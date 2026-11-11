"""Receiver storage is real; transport and failure windows are synthetic."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import httpx
import pytest
from marine.edge import Outbox
from marine.storage import Store


@pytest.mark.parametrize("fault", ["response-lost", "local-delete"])
def test_edge_retry_reuses_event_id_after_remote_acceptance(tmp_path, monkeypatch, fault):
    import marine.edge

    class Edge(Outbox):
        fail_delete = fault == "local-delete"

        @contextmanager
        def connect(self):
            with super().connect() as db:

                class Proxy:
                    def __getattr__(self, name):
                        return getattr(db, name)

                    def execute(self, sql, args=()):
                        if sql.startswith("DELETE FROM pending") and self_outer.fail_delete:
                            self_outer.fail_delete = False
                            raise sqlite3.OperationalError("Synthetic local acknowledgement loss")
                        return db.execute(sql, args)

                self_outer = self
                yield Proxy()

    edge = Edge(tmp_path / "edge.sqlite")
    server = Store(tmp_path / "server.sqlite")
    requests = []
    event = json.loads(Path("contracts/examples.json").read_text())[0]
    edge.put(event)

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload["events"][0]["eventId"])
        accepted = server.accept("synthetic", payload["events"])
        if fault == "response-lost" and len(requests) == 1:
            raise httpx.ReadError("Synthetic response loss", request=request)
        return httpx.Response(200, json={"acceptedEventIds": accepted})

    client_type = httpx.Client
    monkeypatch.setattr(
        marine.edge.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs),
    )
    if fault == "local-delete":
        with pytest.raises(sqlite3.OperationalError):
            edge.flush_once("http://synthetic.invalid", "synthetic-token")
    else:
        assert edge.flush_once("http://synthetic.invalid", "synthetic-token") == 0
    with edge.connect() as db:
        db.execute("UPDATE pending SET next=0")
    assert edge.flush_once("http://synthetic.invalid", "synthetic-token") == 1
    assert requests == [event["eventId"]] * 2
    with edge.connect() as db:
        assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 0
    with server.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
