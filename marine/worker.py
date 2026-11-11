"""Transactional outbox -> Kafka -> durable read model.

The local profile trusts its loopback-only broker and internal service producers.
Do not grant tenants direct topic access. Production requires service ACLs/TLS;
JSON tenantId validation alone is not a broker authorization mechanism.
"""

import json
import logging
import os
import random
import time

from kafka import KafkaConsumer, KafkaProducer
from kafka.structs import OffsetAndMetadata
from kafka.errors import (
    AuthorizationError,
    SaslAuthenticationFailedError,
    KafkaConnectionError,
    KafkaTimeoutError,
)

from marine.storage import Store, canonical, digest


def persist_poll(store, consumer, polled, quarantine):
    for partition, records in polled.items():
        for record in records:
            try:
                value = json.loads(record.value)
                for key in ["tenantId", "siteId", "speciesId"]:
                    if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 128:
                        raise ValueError("Invalid window identity")
                for key in [
                    "windowStart",
                    "windowEnd",
                    "observationCount",
                    "trackCount",
                    "sourceWatermark",
                ]:
                    if type(value[key]) is not int or abs(value[key]) > 2**53 - 1:
                        raise ValueError("Invalid window number")
                if (
                    not 0 <= value["trackCount"] <= value["observationCount"]
                    or value["windowStart"] >= value["windowEnd"]
                ):
                    raise ValueError("Invalid window bounds")
                tracks = value.get("trackIds")
                if tracks is not None and (
                    not isinstance(tracks, list)
                    or len(tracks) > 5000
                    or any(not isinstance(t, str) or len(t) > 512 for t in tracks)
                    or len(set(tracks)) != value["trackCount"]
                ):
                    raise ValueError("Invalid track list")
                payload = canonical(value).decode()
            except (ValueError, KeyError, TypeError):
                quarantine(record)
                continue
            key = digest([value["siteId"], value["speciesId"], value["windowStart"]])
            with store.connect() as db:
                db.execute(
                    "INSERT INTO windows(tenant,id,payload,observations) VALUES(?,?,?,?) ON CONFLICT(tenant,id) DO UPDATE SET payload=excluded.payload,observations=excluded.observations WHERE excluded.observations>windows.observations OR (excluded.observations=windows.observations AND json_extract(excluded.payload,'$.sourceWatermark')>=json_extract(windows.payload,'$.sourceWatermark'))",
                    (value["tenantId"], key, payload, value["observationCount"]),
                )
        if records:
            # A global commit() would acknowledge other fetched partitions before they were persisted.
            consumer.commit({partition: OffsetAndMetadata(records[-1].offset + 1, "")})


MAX_PUBLISH_ATTEMPTS = 8


def delivery_failure(error, waiting_for_ack):
    if isinstance(error, (AuthorizationError, SaslAuthenticationFailedError, PermissionError)):
        return "action_required", "PERMISSION_DENIED"
    if waiting_for_ack or isinstance(error, KafkaTimeoutError):
        return "outcome_unknown", "DELIVERY_OUTCOME_UNKNOWN"
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "action_required", "CONTRACT_INVALID"
    if isinstance(error, (KafkaConnectionError,)):
        return "pending", "BROKER_UNAVAILABLE"
    return "action_required", "INTERNAL_ERROR"


def publish_pending(store, producer, *, clock=time.time, jitter=random.uniform):
    """Claim before send; never repeat an unconfirmed broker side effect."""
    with store.connect() as db:
        rows = db.execute(
            "SELECT tenant,id,topic,attempt,CASE WHEN length(CAST(payload AS BLOB))<=8192 THEN payload END AS payload FROM outbox WHERE status='pending' AND next<=? ORDER BY rowid LIMIT 100",
            (clock(),),
        ).fetchall()
    for row in rows:
        attempt = row["attempt"]
        waiting_for_ack = False
        acknowledged = False
        if attempt >= MAX_PUBLISH_ATTEMPTS:
            with store.connect() as db:
                db.execute(
                    "UPDATE outbox SET status='action_required',error_code='RETRY_BUDGET_EXHAUSTED' WHERE tenant=? AND id=? AND status='pending'",
                    (row["tenant"], row["id"]),
                )
            continue
        try:
            event = json.loads(row["payload"])
            if (
                not isinstance(event, dict)
                or not isinstance(event.get("siteId"), str)
                or not 1 <= len(event["siteId"]) <= 128
            ):
                raise ValueError("Invalid event contract")
            if not isinstance(row["topic"], str) or not 1 <= len(row["topic"]) <= 256:
                raise ValueError("Invalid event topic")
        except (ValueError, KeyError, TypeError):
            with store.connect() as db:
                db.execute(
                    "UPDATE outbox SET status='action_required',error_code='CONTRACT_INVALID' WHERE tenant=? AND id=? AND status='pending'",
                    (row["tenant"], row["id"]),
                )
            continue
        attempt += 1
        with store.connect() as db:
            claimed = db.execute(
                "UPDATE outbox SET status='dispatching',attempt=?,next=?,error_code=NULL WHERE tenant=? AND id=? AND status='pending' AND attempt=?",
                (attempt, clock() + 30, row["tenant"], row["id"], attempt - 1),
            )
            if claimed.rowcount != 1:
                continue
        try:
            future = producer.send(
                row["topic"],
                value=row["payload"].encode(),
                key=(row["tenant"] + ":" + event["siteId"]).encode(),
            )
            waiting_for_ack = True
            future.get(timeout=10)
            acknowledged = True
            with store.connect() as db:
                changed = db.execute(
                    "UPDATE outbox SET status='published',next=0,error_code=NULL WHERE tenant=? AND id=? AND status='dispatching' AND attempt=?",
                    (row["tenant"], row["id"], attempt),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("Delivery state changed")
        except Exception as error:
            status, code = (
                ("outcome_unknown", "ACK_LOCAL_COMMIT_FAILED")
                if acknowledged
                else delivery_failure(error, waiting_for_ack)
            )
            if status == "pending" and attempt >= MAX_PUBLISH_ATTEMPTS:
                status = "action_required"
            delay = jitter(1, min(300, 2 ** min(attempt, 8))) if status == "pending" else 0
            logging.getLogger(__name__).warning("outbox.delivery_failed category=%s", code)
            try:
                with store.connect() as db:
                    db.execute(
                        "UPDATE outbox SET status=?,next=?,error_code=? WHERE tenant=? AND id=? AND status='dispatching' AND attempt=?",
                        (status, clock() + delay, code, row["tenant"], row["id"], attempt),
                    )
            except Exception:
                # The durable dispatching claim remains visible and is never resent.
                logging.getLogger(__name__).warning(
                    "outbox.local_record_unavailable category=DELIVERY_OUTCOME_UNKNOWN"
                )


def run():
    store = Store(os.getenv("MARINE_DATA_DIR", "artifacts/database") + "/marine.sqlite3")
    from marine.operations import Operations

    operations = Operations(store)
    last_heartbeat = 0
    servers = os.environ["KAFKA_BOOTSTRAP"]
    security = {}
    if os.getenv("KAFKA_CLIENT_CONFIG_FILE"):
        from pathlib import Path

        security = json.loads(Path(os.environ["KAFKA_CLIENT_CONFIG_FILE"]).read_text())
        allowed = {
            "security_protocol",
            "ssl_cafile",
            "ssl_certfile",
            "ssl_keyfile",
            "ssl_password",
            "sasl_mechanism",
            "sasl_plain_username",
            "sasl_plain_password",
        }
        if not security.keys() <= allowed:
            raise ValueError("Unsupported Kafka security option")
    if os.getenv("MARINE_MODE") == "production" and security.get("security_protocol") not in {
        "SSL",
        "SASL_SSL",
    }:
        raise ValueError("Production Kafka requires TLS")
    producer = KafkaProducer(
        **security,
        bootstrap_servers=servers,
        acks="all",
        enable_idempotence=True,
        max_block_ms=10000,
        request_timeout_ms=10000,
    )
    consumer = KafkaConsumer(
        "fish.windows.v1",
        **security,
        bootstrap_servers=servers,
        group_id="marine-read-model-v1",
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        max_poll_records=100,
    )
    try:
        while True:
            publish_pending(store, producer)
            polled = consumer.poll(timeout_ms=500)
            if time.time() - last_heartbeat > 10:
                operations.heartbeat("transport", True)
                last_heartbeat = time.time()

            def quarantine(record):
                producer.send(
                    "quarantine.v1",
                    value=canonical(
                        {
                            "reason": "invalid_window",
                            "topic": record.topic,
                            "partition": record.partition,
                            "offset": record.offset,
                        }
                    ),
                ).get(timeout=10)

            persist_poll(store, consumer, polled, quarantine)
    finally:
        consumer.close()
        producer.close(timeout=5)


if __name__ == "__main__":
    run()
