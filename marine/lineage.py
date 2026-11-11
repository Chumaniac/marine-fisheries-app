"""Bounded tenant-local ancestry/impact inspection; never ledger or recall actions."""
import json
import re
from collections import defaultdict, deque
from threading import BoundedSemaphore
from typing import Literal
from fastapi import Depends, HTTPException, Query

SCAN_LIMIT = 1000
RECORD_BYTES = 64 * 1024
EDGE_LIMIT = 200
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
STATES = {"CREATED", "LANDED", "PROCESSED", "CONSUMED", "IN_COLD_CHAIN", "DELIVERED", "RECALLED"}
PROJECTION = "id,revision,CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END AS payload"


def strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate metadata")
        value[key] = item
    return value


def reject_constant(_value):
    raise ValueError("Invalid metadata number")


def metadata(row, tenant):
    stub = {"batchId": row["id"], "state": "UNAVAILABLE", "revision": None, "ownerMSP": None, "available": False}
    try:
        data = json.loads(row["payload"], object_pairs_hook=strict_object, parse_constant=reject_constant)
        parents = data.get("parents", [])
        if (data.get("batchId") != row["id"] or data.get("tenant", tenant) != tenant
                or type(data.get("revision")) is not int or data["revision"] < 1 or data["revision"] != row["revision"]
                or data.get("state") not in STATES or not isinstance(parents, list) or len(parents) > 100
                or any(not isinstance(item, str) or not IDENTIFIER.fullmatch(item) for item in parents)
                or len(set(parents)) != len(parents)):
            raise ValueError("Invalid batch metadata")
        owner = data.get("ownerMSP")
        return {"node": {**stub, "state": data["state"], "revision": data["revision"],
                         "ownerMSP": owner if owner in ("Org1MSP", "Org2MSP", "Org3MSP") else None,
                         "available": True}, "parents": tuple(sorted(parents))}
    except (ValueError, TypeError, AttributeError, RecursionError):
        return {"node": stub, "parents": ()}


def read_lineage(store, tenant, root, direction="both", max_nodes=100, max_depth=8):
    reasons, cache, unresolved = set(), {}, set()
    scanned = 0
    with store.connect() as db:
        db.execute("BEGIN")
        def load(identifier):
            nonlocal scanned
            if identifier in cache:
                return cache[identifier]
            if scanned >= SCAN_LIMIT:
                reasons.add("scan_limit")
                return None
            scanned += 1
            row = db.execute("SELECT " + PROJECTION + " FROM batches WHERE tenant=? AND id=?",
                             (RECORD_BYTES, tenant, identifier)).fetchone()
            record = metadata(row, tenant) if row else None
            cache[identifier] = record
            if record and not record["node"]["available"]:
                reasons.add("metadata_unavailable")
            return record
        if load(root) is None:
            raise KeyError("Batch not found")
        if direction != "ancestors":
            cursor = db.execute("SELECT " + PROJECTION + " FROM batches WHERE tenant=? AND (id!=? OR id IS NULL) ORDER BY id LIMIT ?",
                                (RECORD_BYTES, tenant, root, SCAN_LIMIT))
            for row in cursor:
                if scanned >= SCAN_LIMIT:
                    reasons.add("scan_limit")
                    break
                scanned += 1
                if not isinstance(row["id"], str) or not IDENTIFIER.fullmatch(row["id"]):
                    reasons.add("metadata_unavailable")
                    continue
                record = metadata(row, tenant)
                cache[row["id"]] = record
                if not record["node"]["available"]:
                    reasons.add("metadata_unavailable")
        children = defaultdict(list)
        for identifier, record in cache.items():
            if record:
                for parent in record["parents"]:
                    children[parent].append(identifier)
        selected, depths = {root}, {root: 0}
        relations = defaultdict(set)
        relations[root].add("root")
        def walk(kind):
            queue, seen = deque([(root, 0)]), {root}
            while queue:
                identifier, depth = queue.popleft()
                neighbors = cache[identifier]["parents"] if kind == "ancestor" else sorted(children[identifier])
                for neighbor in neighbors:
                    if neighbor in seen:
                        continue
                    if depth >= max_depth:
                        reasons.add("depth_limit")
                        continue
                    if neighbor not in selected and len(selected) >= max_nodes:
                        reasons.add("node_limit")
                        continue
                    record = load(neighbor) if direction == "ancestors" else cache.get(neighbor)
                    if record is None:
                        unresolved.add((neighbor, identifier))
                        reasons.add("missing_links")
                        continue
                    seen.add(neighbor)
                    selected.add(neighbor)
                    depths[neighbor] = min(depths.get(neighbor, depth + 1), depth + 1)
                    if neighbor != root:
                        relations[neighbor].add(kind)
                    queue.append((neighbor, depth + 1))
        # Never walk upstream from descendants: co-inputs are not affected descendants.
        if direction in ("both", "descendants"):
            walk("descendant")
        if direction in ("both", "ancestors"):
            walk("ancestor")
        all_edges = {(parent, child) for child in selected for parent in cache[child]["parents"] if parent in selected}
        indegree = {key: 0 for key in selected}
        outgoing = defaultdict(list)
        for parent, child in all_edges:
            indegree[child] += 1
            outgoing[parent].append(child)
        ready, visited = deque(key for key, count in indegree.items() if not count), 0
        while ready:
            key = ready.popleft()
            visited += 1
            for child in outgoing[key]:
                indegree[child] -= 1
                if not indegree[child]:
                    ready.append(child)
        cycle = visited != len(selected)
        if cycle:
            reasons.add("cycle")
        if len(all_edges) > EDGE_LIMIT:
            reasons.add("edge_limit")
        nodes = [{**cache[key]["node"], "relations": sorted(relations[key]), "depth": depths[key]}
                 for key in sorted(selected, key=lambda key: (depths[key], key))]
        return {"schema": "marine.batch-lineage/v1", "rootId": root, "direction": direction,
                "complete": not reasons, "reasons": sorted(reasons), "cycleDetected": cycle,
                "nodes": nodes, "edges": [{"parent": parent, "child": child} for parent, child in sorted(all_edges)[:EDGE_LIMIT]],
                "unresolvedLinks": len(unresolved), "scanned": scanned,
                "limits": {"nodes": max_nodes, "edges": EDGE_LIMIT, "depth": max_depth, "scan": SCAN_LIMIT, "recordBytes": RECORD_BYTES},
                "evidence": "tenant-local-metadata", "ledgerVerified": False, "mutated": False}


def register_lineage(app, store, identity):
    inspections = BoundedSemaphore(2)
    @app.get("/v1/batches/{batch_id}/lineage")
    def lineage(batch_id: str, direction: Literal["both", "ancestors", "descendants"] = Query(default="both"),
                max_nodes: int = Query(default=100, ge=1, le=100), max_depth: int = Query(default=8, ge=1, le=12),
                claims=Depends(identity)):
        if not IDENTIFIER.fullmatch(batch_id):
            raise HTTPException(422, "Batch identifier is invalid")
        if not inspections.acquire(blocking=False):
            raise HTTPException(429, "Lineage inspection is busy; retry later")
        try:
            return read_lineage(store, claims["tenant"], batch_id, direction, max_nodes, max_depth)
        except KeyError:
            raise HTTPException(404, "Batch not found") from None
        finally:
            inspections.release()
