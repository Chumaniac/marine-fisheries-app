"""Bounded metadata-only quality checks; synthetic records, no model calls."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

from fastapi.testclient import TestClient
import pytest

from marine.api import create_app
from marine.storage import Store, canonical, digest


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.post('/v1/session', headers={'X-Requested-With': 'marine-web'}).status_code == 200
        client.headers['X-Marine-Scope'] = client.get('/v1/me').json()['scopeHash']
        yield client


def event(kind=0, *, site='quality-site', species='saithe', tenant='demo'):
    value = copy.deepcopy(json.loads(Path('contracts/examples.json').read_text())[kind])
    value.update(eventId=str(uuid.uuid4()), tenantId=tenant, siteId=site)
    if value['eventType'] == 'fish.observation':
        value['data']['speciesId'] = species
    return value


def quality(client, **params):
    response = client.get('/v1/data-quality', params={'siteId': 'quality-site', 'speciesId': 'saithe', **params})
    assert response.status_code == 200, response.text
    assert response.headers.get('cache-control') == 'no-store'
    return response.json()


def test_empty_and_valid_scope_have_explicit_coverage(client):
    empty = quality(client)
    assert empty['state'] == 'empty' and empty['checkedRecords'] == 0
    source = event()
    client.app.state.store.accept('demo', [source])
    result = quality(client)
    assert result['state'] == 'checked'
    assert result['counts']['fishSelected'] == 1
    assert result['coverage']['physicalImagesChecked'] is False
    assert result['coverage']['recognizedSiteOnly'] is True
    assert source['data']['evidenceId'] not in json.dumps(result)
    assert 'payload' not in json.dumps(result)


def test_environment_nulls_and_quality_are_actionable_not_schema_errors(client):
    sources = [event(1) for _ in range(3)]
    for source, status in zip(sources, ['valid', 'suspect', 'invalid']):
        source['data']['sensorQuality'] = status
        source['data']['temperatureC'] = None
    client.app.state.store.accept('demo', sources)
    result = quality(client)
    assert result['state'] == 'needs-review'
    assert result['issues']['schemaInvalid'] == 0
    assert result['issues']['environmentIncomplete'] == 3
    assert result['issues']['sensorSuspect'] == 1 and result['issues']['sensorInvalid'] == 1


def test_schema_hash_and_record_binding_are_checked(client):
    source = event()
    client.app.state.store.accept('demo', [source])
    source['data']['confidence'] = 2
    source['tenantId'] = 'mismatched-payload-tenant'
    with client.app.state.store.connect() as db:
        db.execute('UPDATE events SET payload=? WHERE tenant=?', (canonical(source).decode(), 'demo'))
    result = quality(client)
    assert result['issues']['schemaInvalid'] == 1
    assert result['issues']['hashMismatch'] == 1
    assert result['issues']['identityMismatch'] == 1
    assert result['findingRecords'] == 1
    assert len(result['findings']) == 1
    assert 'mismatched-payload-tenant' not in json.dumps(result)


def test_received_time_is_excluded_from_stored_content_hash(client):
    source = event()
    client.app.state.store.accept('demo', [source])
    source['receivedAt'] = '2026-01-02T00:00:00Z'
    with client.app.state.store.connect() as db:
        db.execute('UPDATE events SET payload=? WHERE tenant=?', (canonical(source).decode(), 'demo'))
    assert quality(client)['issues']['hashMismatch'] == 0


def test_tenant_site_species_and_sql_parameters_are_isolated(client):
    store = client.app.state.store
    store.accept('other', [event(tenant='other')])
    store.accept('demo', [event(site='other-site'), event(species='salmonid'), event()])
    result = quality(client)
    assert result['checkedRecords'] == 2
    assert result['counts']['fishSelected'] == 1 and result['counts']['fishOtherSpecies'] == 1
    assert result['state'] == 'checked' and not result['findings']
    assert quality(client, siteId="quality-site' OR 1=1 --")['checkedRecords'] == 0
    assert quality(client, speciesId="saithe' OR 1=1 --")['counts']['fishSelected'] == 0


def test_bad_unattributable_json_and_oversized_rows_do_not_crash_or_claim_coverage(client):
    store = client.app.state.store
    store.accept('demo', [event()])
    with store.connect() as db:
        for identity, payload in [('broken', '{bad'), ('oversized', '{"siteId":"quality-site","x":"'+'x'*9000+'"}')]:
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)', ('demo', identity, payload, 'bad', 0))
    result = quality(client)
    assert result['checkedRecords'] == 1
    assert result['coverage']['unattributableRecordsExcluded'] is True


def test_record_and_reference_budgets_are_visible(client):
    sources = [event() for _ in range(501)]
    for source in sources:
        source['data']['confidence'] = 2
    client.app.state.store.accept('demo', sources)
    result = quality(client)
    assert result['checkedRecords'] == 500 and result['partial']
    assert result['partialReason'] == 'record-budget' and result['state'] == 'partial'
    assert result['issues']['schemaInvalid'] == 500
    assert result['findingRecords'] == 500 and len(result['findings']) == 32 and result['findingsTruncated']


def test_byte_budget_stops_before_next_payload_is_checked(client):
    store = client.app.state.store
    with store.connect() as db:
        for index in range(300):
            source = event()
            source['padding'] = 'x' * 6000
            body = canonical(source).decode()
            db.execute('INSERT INTO events VALUES(?,?,?,?,?)',
                       ('demo', source['eventId'], body, digest({k: v for k, v in source.items() if k != 'receivedAt'}), index))
    result = quality(client)
    assert result['partialReason'] == 'byte-budget' and result['partial']
    assert result['checkedRecords'] < 300 and result['checkedBytes'] <= 1024 * 1024


def test_readonly_endpoint_requires_session_and_current_scope(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/v1/data-quality').status_code == 401
        client.post('/v1/session', headers={'X-Requested-With': 'marine-web'})
        assert client.get('/v1/data-quality', headers={'X-Marine-Scope': 'wrong'}).status_code == 403


def test_quality_check_keeps_source_tables_unchanged(client):
    store = client.app.state.store
    store.accept('demo', [event(), event(1)])
    def snapshot():
        with store.connect() as db:
            return {table: [tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in ['events', 'outbox', 'scoped_models', 'labels', 'collection_orders']}
    before = snapshot()
    quality(client)
    assert snapshot() == before


def test_malformed_environment_types_stay_diagnostic(client):
    source = event(1)
    source['data']['sensorQuality'] = ['invalid-type']
    client.app.state.store.accept('demo', [source])
    result = quality(client)
    assert result['issues']['schemaInvalid'] == 1


def test_sparse_site_query_does_not_scan_unrelated_tenant_history(client, monkeypatch):
    store = client.app.state.store
    store.accept('demo', [event()])
    with store.connect() as db:
        db.execute('UPDATE events SET created=-1 WHERE tenant=?', ('demo',))
        payload = json.dumps({'siteId': 'unrelated-site', 'padding': 'x' * 500})
        db.executemany('INSERT INTO events VALUES(?,?,?,?,?)',
                       (('demo', f'noise-{index:05}', payload, 'synthetic', index) for index in range(20_000)))
    original = store.connect
    instructions = 0

    @contextmanager
    def measured_connection():
        nonlocal instructions
        with original() as db:
            checking = False

            def trace(statement):
                nonlocal checking
                checking = statement.lstrip().startswith('SELECT id,payload,hash,')

            def progress():
                nonlocal instructions
                if checking:
                    instructions += 100
                return 0

            db.set_trace_callback(trace)
            db.set_progress_handler(progress, 100)
            try:
                yield db
            finally:
                db.set_progress_handler(None, 0)
                db.set_trace_callback(None)

    monkeypatch.setattr(store, 'connect', measured_connection)
    result = quality(client)
    assert result['checkedRecords'] == 1 and result['state'] == 'checked'
    assert not result['partial']
    assert instructions < 5000, f'Quality query executed at least {instructions} SQLite instructions for one matching row'


def test_existing_store_builds_quality_index_without_rewriting_corrupt_or_large_rows(tmp_path):
    path = tmp_path / 'existing.sqlite'
    rows = [('demo', str(index), payload, 'retained-hash', index) for index, payload in enumerate([
        '{bad', '{"siteId":"quality-site","x":"' + 'x' * 9000 + '"}',
        '{"siteId":"quality-site"}', '{"siteId":null}', '{"siteId":["unattributable"]}',
    ])]
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE events(tenant TEXT,id TEXT,payload TEXT NOT NULL,hash TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(tenant,id))')
        db.executemany('INSERT INTO events VALUES(?,?,?,?,?)', rows)
    for _ in range(2):
        store = Store(path)
        with store.connect() as db:
            assert [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY id')] == rows
            assert sum(row['name'] == 'events_quality_site_time_v1' for row in db.execute('PRAGMA index_list(events)')) == 1


def test_quality_index_tracks_site_changes_and_keeps_timestamp_ties_deterministic(client):
    store = client.app.state.store
    sources = [event() for _ in range(2)]
    for source in sources:
        source['data']['confidence'] = 2
    store.accept('demo', sources)
    with store.connect() as db:
        db.execute('UPDATE events SET created=0 WHERE tenant=?', ('demo',))
    assert [row['eventId'] for row in quality(client)['findings']] == sorted((source['eventId'] for source in sources), reverse=True)
    moved = sources[0]
    moved['siteId'] = 'moved-site'
    with store.connect() as db:
        db.execute('UPDATE events SET payload=? WHERE tenant=? AND id=?', (canonical(moved).decode(), 'demo', moved['eventId']))
    assert quality(client)['checkedRecords'] == 1
    assert quality(client, siteId='moved-site')['checkedRecords'] == 1
