"""Idempotent topic initialization for the local single-broker integration profile."""

from kafka.admin import KafkaAdminClient, NewTopic

client = KafkaAdminClient(bootstrap_servers="127.0.0.1:19092", request_timeout_ms=10000)
try:
    existing = set(client.list_topics())
    required = {
        "fish.observations.v1": 3,
        "environment.readings.v1": 3,
        "fish.windows.v1": 1,
        "late.events.v1": 1,
        "quarantine.v1": 1,
    }
    missing = [
        NewTopic(name, partitions, 1, topic_configs={"retention.ms": "604800000"})
        for name, partitions in required.items()
        if name not in existing
    ]
    if missing:
        client.create_topics(missing)
    print("Local topics ready:", ", ".join(required))
finally:
    client.close()
