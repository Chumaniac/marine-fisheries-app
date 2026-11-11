"""Bounded, read-only observations about stored event metadata and integrity."""
import json
import uuid

from marine.storage import QUALITY_RECORD_BYTES, QUALITY_SITE_EXPRESSION, canonical, digest

MAX_RECORDS = 500
MAX_BYTES = 1024 * 1024
MAX_RECORD_BYTES = QUALITY_RECORD_BYTES
MAX_FINDINGS = 32
ISSUES = ('schemaInvalid', 'hashMismatch', 'identityMismatch', 'environmentIncomplete',
          'sensorSuspect', 'sensorInvalid')


def inspect_event(validator, tenant, event_id, payload, expected_hash):
    """Shared pure validation for bounded site scans and exact order references."""
    try:
        event = json.loads(payload)
        schema_ok = validator.is_valid(event)
    except (ValueError, TypeError, RecursionError):
        event, schema_ok = {}, False
    if not isinstance(event, dict):
        event, schema_ok = {}, False
    try:
        canonical(event)
    except (ValueError, TypeError, OverflowError, RecursionError):
        schema_ok = False
    codes = [] if schema_ok else ['schemaInvalid']
    try:
        matched = digest({key: value for key, value in event.items() if key != 'receivedAt'}) == expected_hash
    except (ValueError, TypeError, OverflowError, RecursionError):
        matched = False
    if not matched:
        codes.append('hashMismatch')
    if event.get('tenantId') != tenant or event.get('eventId') != event_id:
        codes.append('identityMismatch')
    data = event.get('data') if isinstance(event.get('data'), dict) else {}
    if event.get('eventType') == 'environment.reading':
        if any(data.get(key) is None for key in ('temperatureC', 'salinityPsu', 'turbidityNtu')):
            codes.append('environmentIncomplete')
        quality = data.get('sensorQuality')
        if quality in ('suspect', 'invalid'):
            codes.append('sensorSuspect' if quality == 'suspect' else 'sensorInvalid')
    return event, codes


def check_source_data(store, validator, tenant, site, species):
    result = {'schema': 'marine.data-quality/v1', 'siteId': site, 'speciesId': species,
              'state': 'empty', 'checkedRecords': 0, 'checkedBytes': 0, 'partial': False,
              'partialReason': None, 'counts': dict.fromkeys(('fishSelected', 'fishOtherSpecies', 'environment', 'other'), 0),
              'issues': dict.fromkeys(ISSUES, 0), 'findingRecords': 0, 'findings': [], 'findingsTruncated': False,
              'limits': {'records': MAX_RECORDS, 'bytes': MAX_BYTES, 'recordBytes': MAX_RECORD_BYTES, 'findings': MAX_FINDINGS},
              'coverage': {'recognizedSiteOnly': True, 'includesOtherSpecies': True,
                           'unattributableRecordsExcluded': True, 'physicalImagesChecked': False}}
    # Guard JSON extraction itself: corrupt/unbounded rows cannot be safely attributed
    # to a site. Their exclusion is part of the public coverage contract, not a pass.
    sql = f"""SELECT id,payload,hash,length(CAST(payload AS BLOB)) AS bytes FROM events
             WHERE tenant=? AND ({QUALITY_SITE_EXPRESSION})=?
             ORDER BY created DESC,id DESC LIMIT ?"""
    with store.connect() as db:
        for row in db.execute(sql, (tenant, site, MAX_RECORDS + 1)):
            if result['checkedRecords'] >= MAX_RECORDS:
                result.update(partial=True, partialReason='record-budget')
                break
            if result['checkedBytes'] + row['bytes'] > MAX_BYTES:
                result.update(partial=True, partialReason='byte-budget')
                break
            position = result['checkedRecords']
            result['checkedRecords'] += 1
            result['checkedBytes'] += row['bytes']
            event, codes = inspect_event(validator, tenant, row['id'], row['payload'], row['hash'])
            data = event.get('data') if isinstance(event.get('data'), dict) else {}
            kind = event.get('eventType')
            counter = ('fishSelected' if data.get('speciesId') == species else 'fishOtherSpecies') if kind == 'fish.observation' else 'environment' if kind == 'environment.reading' else 'other'
            result['counts'][counter] += 1
            for code in codes:
                result['issues'][code] += 1
            if codes:
                result['findingRecords'] += 1
                if len(result['findings']) < MAX_FINDINGS:
                    try:
                        reference = str(uuid.UUID(row['id']))
                        if reference != row['id']:
                            reference = None
                    except (ValueError, AttributeError):
                        reference = None
                    result['findings'].append({'position': position, 'eventId': reference, 'issues': codes})
    result['findingsTruncated'] = result['findingRecords'] > len(result['findings'])
    result['state'] = 'partial' if result['partial'] else 'needs-review' if result['findingRecords'] else 'checked' if result['checkedRecords'] else 'empty'
    return result
