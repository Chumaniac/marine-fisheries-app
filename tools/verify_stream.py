"""Real isolated Kafka/Flink duplicate and late-event check using synthetic events."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from kafka import KafkaConsumer, KafkaProducer, TopicPartition


def reader(topic):
    consumer = KafkaConsumer(
        bootstrap_servers="127.0.0.1:19092", enable_auto_commit=False, request_timeout_ms=10000
    )
    partitions = consumer.partitions_for_topic(topic)
    if not partitions:
        raise RuntimeError(f"{topic} is not available")
    consumer.assign([TopicPartition(topic, p) for p in partitions])
    consumer.seek_to_end()
    return consumer


def main():
    run_id = "check-" + uuid4().hex[:12]
    now = time.time()
    output = reader("fish.windows.v1")
    late = reader("late.events.v1")
    producer = KafkaProducer(bootstrap_servers="127.0.0.1:19092", acks="all")

    def event(identity, at):
        return {
            **json.loads(Path("contracts/examples.json").read_text())[0],
            "schemaVersion": "1.0",
            "eventType": "fish.observation",
            "eventId": identity,
            "tenantId": "stream-check",
            "siteId": run_id,
            "deviceId": "stream-check-device",
            "occurredAt": datetime.fromtimestamp(at, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "data": {"speciesId": "saithe", "trackId": identity, "countMethod": "track"},
        }

    def send(value):
        producer.send(
            "fish.observations.v1", key=run_id.encode(), value=json.dumps(value).encode()
        ).get(timeout=10)

    try:
        base = event(run_id, now - 300)
        send(base)
        send(base)
        send(event(run_id + "-advance", now))
        found = None
        deadline = time.time() + 180
        while time.time() < deadline and found is None:
            for records in output.poll(timeout_ms=1000).values():
                for record in records:
                    value = json.loads(record.value)
                    if (
                        value["siteId"] == run_id
                        and value["windowStart"] <= base_timestamp(base) < value["windowEnd"]
                    ):
                        found = value
        if found is None or found["observationCount"] != 1 or found["trackCount"] != 1:
            raise AssertionError("Duplicate handling failed or no actual window received")
        send(event(run_id + "-late", now - 3600))
        late_found = False
        deadline = time.time() + 180
        while time.time() < deadline and not late_found:
            for records in late.poll(timeout_ms=1000).values():
                for record in records:
                    value = json.loads(record.value)
                    if value.get("id") == run_id + "-late":
                        late_found = True
        if not late_found:
            raise AssertionError("Very late record did not reach late side output")
        result = {
            "dataSource": "synthetic",
            "actualKafkaFlink": True,
            "duplicateEventCountedOnce": True,
            "lateSideOutput": True,
            "window": found,
        }
        Path("artifacts/stream-recovery-check.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    finally:
        output.close()
        late.close()
        producer.close()


def base_timestamp(event):
    return datetime.fromisoformat(event["occurredAt"].replace("Z", "+00:00")).timestamp() * 1000


if __name__ == "__main__":
    main()
