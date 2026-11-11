"""Tenant-scoped metadata observations. Never repair, resend or delete records."""
import sqlite3
import time

from marine.storage import CAPACITIES
from marine.evidence_store import MAX_EVIDENCE_BYTES

STAGES = ('received', 'review_pending', 'published', 'review_rejected', 'unknown')
ITEM_LIMIT = 20
REVIEW_RECORD_BYTES = 65536
MAX_SAFE_COUNT = 2**53 - 1
TABLES = {'events': 'events', 'outbox_active': 'outbox', 'labels': 'labels',
          'batches': 'batches', 'reviews': 'reviews', 'review_bytes': 'reviews',
          'edge_admissions': 'edge_admissions', 'edge_evidence': 'edge_evidence'}


def metric(key, used=None, reason=None):
    limit = CAPACITIES[key]
    if type(used) is not int or not 0 <= used <= MAX_SAFE_COUNT:
        used, reason = None, reason or 'record_unavailable'
    status = 'unknown' if used is None else 'full' if used >= limit else 'near' if used * 5 >= limit * 4 else 'ok'
    return {'key': key, 'used': used, 'limit': limit, 'unit': 'bytes' if key == 'review_bytes' else 'records',
            'status': status, 'reason': reason if used is None else None}


def unavailable_workload(tenant, reason='query_unavailable', access='allowed'):
    return {'schema': 'marine.operational-workload/v1', 'tenant': tenant, 'readOnly': True,
            'access': access, 'observedAt': time.time(), 'complete': False,
            'capacity': [] if access == 'restricted' else [metric(key, reason=reason) for key in CAPACITIES],
            'admissions': {'available': False, 'counts': None, 'items': [], 'truncated': False, 'reason': reason}}


def identifier(value):
    return isinstance(value, str) and 0 < len(value.encode('utf-8')) <= 512 and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)


def review_bytes(db, tenant):
    # Parse at most1001 bounded metadata rows inside SQLite; Python gets no body.
    row = db.execute('''
        SELECT count(*) AS rows,count(n) AS valid,coalesce(sum(n),0) AS bytes FROM (
          SELECT CASE WHEN body IS NOT NULL AND json_valid(body) THEN
            CASE WHEN json_type(body,'$.manifest.byteLength')='integer'
                 AND json_extract(body,'$.manifest.byteLength') BETWEEN 1 AND ?
            THEN json_extract(body,'$.manifest.byteLength') END END AS n FROM (
              SELECT CASE WHEN length(CAST(payload AS BLOB))<=? THEN payload END AS body
              FROM reviews WHERE tenant=? ORDER BY id LIMIT ?))''',
        (MAX_EVIDENCE_BYTES, REVIEW_RECORD_BYTES, tenant, CAPACITIES['reviews'] + 1)).fetchone()
    if row['rows'] > CAPACITIES['reviews']:
        return metric('review_bytes', reason='scan_limit')
    if row['rows'] != row['valid']:
        return metric('review_bytes', reason='record_unavailable')
    return metric('review_bytes', row['bytes'])


def workload_snapshot(store, tenant, role):
    if role not in {'operator', 'auditor'}:
        return unavailable_workload(tenant, 'role_required', 'restricted')
    result = unavailable_workload(tenant)
    try:
        with store.connect() as db:
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            capacity = []
            for key, table in TABLES.items():
                if table not in tables:
                    capacity.append(metric(key, reason='table_unavailable'))
                elif key == 'review_bytes':
                    capacity.append(review_bytes(db, tenant))
                else:
                    # Table names are a fixed private map, never request parameters.
                    suffix = " AND status IN ('pending','action_required','dispatching','outcome_unknown')" if key == 'outbox_active' else ''
                    count = db.execute(f'SELECT count(*) FROM {table} WHERE tenant=?' + suffix, (tenant,)).fetchone()[0]
                    capacity.append(metric(key, count))
            result['capacity'] = capacity
            if 'edge_admissions' not in tables:
                result['admissions']['reason'] = 'table_unavailable'
            else:
                counts = dict.fromkeys(STAGES, 0)
                grouped = db.execute("SELECT CASE WHEN status IN ('received','review_pending','published','review_rejected') THEN status ELSE 'unknown' END AS stage,count(*) AS n FROM edge_admissions WHERE tenant=? GROUP BY stage", (tenant,))
                for row in grouped:
                    if type(row['n']) is not int or not 0 <= row['n'] <= MAX_SAFE_COUNT:
                        raise ValueError('invalid count')
                    counts[row['stage']] = row['n']
                rows = db.execute('''SELECT CASE WHEN length(CAST(id AS BLOB))<=512 THEN id END AS id,
                    CASE WHEN length(CAST(device AS BLOB))<=512 THEN device END AS device,
                    CASE WHEN status IN ('received','review_pending','published','review_rejected') THEN status ELSE 'unknown' END AS stage
                    FROM edge_admissions WHERE tenant=? AND (status NOT IN ('published','review_rejected') OR status IS NULL)
                    ORDER BY id LIMIT ?''', (tenant, ITEM_LIMIT + 1)).fetchall()
                items, invalid = [], False
                for row in rows[:ITEM_LIMIT]:
                    if not identifier(row['id']) or not identifier(row['device']):
                        invalid = True
                        continue
                    action = {'received': 'check_original_request', 'review_pending': 'review_existing_sample'}.get(row['stage'], 'manual_check')
                    items.append({'sampleId': row['id'], 'deviceId': row['device'], 'status': row['stage'],
                                  'recordedAt': None, 'cause': 'not_recorded', 'nextAction': action})
                result['admissions'] = {'available': True, 'counts': counts, 'items': items,
                                        'truncated': len(rows) > ITEM_LIMIT or invalid,
                                        'reason': 'record_unavailable' if invalid else None}
            result['complete'] = (all(row['status'] != 'unknown' for row in capacity)
                                  and result['admissions']['available'] and not result['admissions']['truncated']
                                  and result['admissions']['counts']['unknown'] == 0)
        return result
    except (sqlite3.Error, ValueError, TypeError, OverflowError):
        return unavailable_workload(tenant)
