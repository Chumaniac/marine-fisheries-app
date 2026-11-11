import json
from types import SimpleNamespace

import pytest
from kafka import TopicPartition

from marine.storage import Store
from marine.worker import persist_poll


def record(count=2, watermark=9000):
    payload = {
        "tenantId": "a",
        "siteId": "s",
        "speciesId": "fish",
        "windowStart": 1000,
        "windowEnd": 2000,
        "observationCount": count,
        "trackCount": 1,
        "sourceWatermark": watermark,
    }
    return SimpleNamespace(
        value=json.dumps(payload).encode(), offset=3, topic="fish.windows.v1", partition=0
    )


def test_commit_does_not_ack_unpersisted_partition(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    commits = []
    consumer = SimpleNamespace(commit=lambda offsets: commits.append(offsets))
    p0, p1 = TopicPartition("fish.windows.v1", 0), TopicPartition("fish.windows.v1", 1)
    invalid = SimpleNamespace(value=b"bad", offset=9, topic="fish.windows.v1", partition=1)

    def fail_to_quarantine(value):
        raise RuntimeError("broker unavailable")

    with pytest.raises(RuntimeError):
        persist_poll(store, consumer, {p0: [record()], p1: [invalid]}, fail_to_quarantine)
    assert len(commits) == 1 and list(commits[0]) == [p0]
    assert commits[0][p0].offset == 4


def test_old_replays_cannot_regress_window_count_or_watermark(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    consumer = SimpleNamespace(commit=lambda offsets: None)
    partition = TopicPartition("fish.windows.v1", 0)
    for item in [record(3, 9000), record(2, 10000), record(3, 8000)]:
        persist_poll(store, consumer, {partition: [item]}, lambda value: None)
    with store.connect() as db:
        value = json.loads(db.execute("SELECT payload FROM windows").fetchone()[0])
    assert value["observationCount"] == 3 and value["sourceWatermark"] == 9000


def test_publish_retry_budget_preserves_original_and_continues_other_events(tmp_path):
    from marine.worker import publish_pending

    store = Store(tmp_path / "retry.sqlite3")
    with store.connect() as db:
        for identity in ("bad", "good"):
            db.execute(
                "INSERT INTO outbox(tenant,id,topic,payload,attempt) VALUES(?,?,?,?,?)",
                (
                    "a",
                    identity,
                    "synthetic",
                    json.dumps({"siteId": identity}),
                    7 if identity == "bad" else 0,
                ),
            )
    sent = []

    def send(topic, value, key):
        event = json.loads(value)
        sent.append(event["siteId"])
        if event["siteId"] == "bad":
            raise RuntimeError("synthetic outage")
        return SimpleNamespace(get=lambda timeout: None)

    producer = SimpleNamespace(send=send)
    publish_pending(store, producer, clock=lambda: 1000, jitter=lambda a, b: 1)
    publish_pending(store, producer, clock=lambda: 2000, jitter=lambda a, b: 1)
    assert sent == ["bad", "good"]
    with store.connect() as db:
        row = db.execute("SELECT * FROM outbox WHERE id='bad'").fetchone()
        assert row["status"] == "action_required" and row["attempt"] == 8
        assert json.loads(row["payload"]) == {"siteId": "bad"}
        assert db.execute("SELECT status FROM outbox WHERE id='good'").fetchone()[0] == "published"


def test_acknowledged_delivery_with_failed_local_commit_is_never_sent_again(tmp_path):
    from contextlib import contextmanager
    import sqlite3
    from marine.worker import publish_pending

    class FaultStore(Store):
        fail_commit = True

        @contextmanager
        def connect(self):
            with super().connect() as connection:

                class Proxy:
                    def __getattr__(self, name):
                        return getattr(connection, name)

                    def execute(self, sql, args=()):
                        if "SET status='published'" in sql and self_outer.fail_commit:
                            self_outer.fail_commit = False
                            raise sqlite3.OperationalError("SYNTHETIC_LOCAL_COMMIT_FAILED")
                        return connection.execute(sql, args)

                self_outer = self
                yield Proxy()

    store = FaultStore(tmp_path / "ack.sqlite")
    with store.connect() as db:
        db.execute(
            "INSERT INTO outbox(tenant,id,topic,payload) VALUES('a','original','synthetic','{\"siteId\":\"s\"}')"
        )
    sent = []
    producer = SimpleNamespace(
        send=lambda *args, **kwargs: (sent.append(1) or SimpleNamespace(get=lambda timeout: None))
    )
    publish_pending(store, producer, clock=lambda: 1000, jitter=lambda a, b: 1)
    publish_pending(store, producer, clock=lambda: 2000, jitter=lambda a, b: 1)
    assert len(sent) == 1
    with store.connect() as db:
        row = db.execute("SELECT * FROM outbox").fetchone()
        assert row["status"] in {"outcome_unknown", "dispatching"}
        assert row["payload"] == '{"siteId":"s"}' and row["attempt"] == 1


def test_unknown_broker_acknowledgement_is_not_an_automatic_retry(tmp_path):
    from kafka.errors import KafkaTimeoutError
    from marine.worker import publish_pending

    store = Store(tmp_path / "unknown.sqlite")
    with store.connect() as db:
        db.execute(
            "INSERT INTO outbox(tenant,id,topic,payload) VALUES('a','original','synthetic','{\"siteId\":\"s\"}')"
        )
    sent = []

    def send(*args, **kwargs):
        sent.append(1)

        def wait(timeout):
            raise KafkaTimeoutError("SYNTHETIC_PRIVATE_ERROR")

        return SimpleNamespace(get=wait)

    for timestamp in (1000, 2000):
        publish_pending(
            store, SimpleNamespace(send=send), clock=lambda: timestamp, jitter=lambda a, b: 1
        )
    assert len(sent) == 1
    with store.connect() as db:
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "outcome_unknown"


def test_failure_classes_keep_only_fixed_codes_and_bounded_retries(tmp_path, caplog):
    from kafka.errors import KafkaConnectionError, AuthorizationError
    from marine.worker import publish_pending

    for name, error, expected in [
        ("connection", KafkaConnectionError("SYNTHETIC_PRIVATE"), "pending"),
        ("permission", AuthorizationError("SYNTHETIC_PRIVATE"), "action_required"),
        ("contract", ValueError("SYNTHETIC_PRIVATE"), "action_required"),
    ]:
        store = Store(tmp_path / (name + ".sqlite"))
        with store.connect() as db:
            db.execute(
                "INSERT INTO outbox(tenant,id,topic,payload) VALUES('a','original','synthetic','{\"siteId\":\"s\"}')"
            )

        def send(*args, **kwargs):
            raise error

        publish_pending(
            store, SimpleNamespace(send=send), clock=lambda: 1000, jitter=lambda a, b: 2
        )
        with store.connect() as db:
            row = db.execute("SELECT * FROM outbox").fetchone()
            assert (
                row["status"] == expected
                and row["attempt"] == 1
                and row["payload"] == '{"siteId":"s"}'
            )
            if expected == "pending":
                assert row["next"] == 1002
    assert "SYNTHETIC_PRIVATE" not in caplog.text


def test_legacy_outbox_gains_metadata_without_replacing_original_rows(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE outbox(tenant TEXT,id TEXT,topic TEXT,payload TEXT,status TEXT DEFAULT 'pending',attempt INTEGER DEFAULT 0,next REAL DEFAULT 0,PRIMARY KEY(tenant,id))"
        )
        db.execute(
            "INSERT INTO outbox(tenant,id,topic,payload) VALUES('a','original','synthetic','original synthetic bytes')"
        )
    store = Store(path)
    with store.connect() as db:
        row = db.execute("SELECT * FROM outbox").fetchone()
        assert row["payload"] == "original synthetic bytes" and row["error_code"] is None


def test_future_contract_exception_stays_unknown_after_send():
    from marine.worker import delivery_failure

    assert delivery_failure(ValueError("synthetic"), True) == (
        "outcome_unknown",
        "DELIVERY_OUTCOME_UNKNOWN",
    )


def test_failed_record_of_unknown_outcome_retains_durable_dispatch_claim(tmp_path):
    from contextlib import contextmanager
    import sqlite3
    from marine.worker import publish_pending

    class UnwritableResult(Store):
        @contextmanager
        def connect(self):
            with super().connect() as connection:

                class Proxy:
                    def __getattr__(self, name):
                        return getattr(connection, name)

                    def execute(self, sql, args=()):
                        if (
                            "SET status='published'" in sql
                            or "SET status=?,next=?,error_code=?" in sql
                        ):
                            raise sqlite3.OperationalError("Synthetic record failure")
                        return connection.execute(sql, args)

                yield Proxy()

    store = UnwritableResult(tmp_path / "claim.sqlite")
    with store.connect() as db:
        db.execute(
            "INSERT INTO outbox(tenant,id,topic,payload) VALUES('a','original','synthetic','{\"siteId\":\"s\"}')"
        )
    sent = []
    producer = SimpleNamespace(
        send=lambda *args, **kwargs: sent.append(1) or SimpleNamespace(get=lambda timeout: None)
    )
    for timestamp in (1000, 2000):
        publish_pending(store, producer, clock=lambda: timestamp, jitter=lambda a, b: 1)
    assert len(sent) == 1
    with store.connect() as db:
        assert db.execute("SELECT status FROM outbox").fetchone()[0] == "dispatching"
