"""Synthetic tenant-scoped workload observations never repair or resend records."""
import json
from uuid import uuid4

from test_api import client
from marine.operational_workload import workload_snapshot
from marine.storage import Store, CAPACITIES


def test_operations_exposes_explicit_capacity_and_unknown_admission_time(client):
    store=client.app.state.store
    sample=str(uuid4())
    with store.connect() as db:
        db.execute("INSERT INTO edge_admissions VALUES(?,?,?,?,?,'received',NULL)",
                   ('demo',sample,'a'*64,str(uuid4()),'synthetic-device'))
        db.execute("INSERT INTO edge_admissions VALUES(?,?,?,?,?,'received',NULL)",
                   ('foreign',str(uuid4()),'b'*64,str(uuid4()),'PRIVATE_FOREIGN_DEVICE'))
    response=client.get('/v1/operations')
    assert response.status_code==200
    report=response.json()['workload']
    assert report['schema']=='marine.operational-workload/v1' and report['tenant']=='demo'
    assert report['readOnly'] is True
    assert report['admissions']['counts']['received']==1
    item=report['admissions']['items'][0]
    assert item['sampleId']==sample and item['recordedAt'] is None and item['cause']=='not_recorded'
    assert item['nextAction']=='check_original_request'
    assert 'PRIVATE_FOREIGN_DEVICE' not in json.dumps(report)
    capacities={row['key']:row for row in report['capacity']}
    assert capacities['events']['limit']==500000 and capacities['events']['used']==0
    assert capacities['edge_admissions']['used']==1 and capacities['edge_admissions']['limit']==1000
    with store.connect() as db:
        assert db.execute('SELECT status FROM edge_admissions WHERE tenant=? AND id=?',('demo',sample)).fetchone()[0]=='received'


def test_missing_tables_are_unknown_and_observations_do_not_initialize_or_write(tmp_path):
    store=Store(tmp_path/'synthetic.sqlite')
    with store.connect() as db:
        before=list(db.execute("SELECT name FROM sqlite_master WHERE type='table'"))
    report=workload_snapshot(store,'demo','operator')
    assert not report['complete'] and not report['admissions']['available']
    rows={r['key']:r for r in report['capacity']}
    for key in ['edge_admissions','edge_evidence']:
        assert rows[key]['used'] is None and rows[key]['reason']=='table_unavailable'
    with store.connect() as db:
        assert list(db.execute("SELECT name FROM sqlite_master WHERE type='table'"))==before
        assert db.execute('SELECT count(*) FROM events').fetchone()[0]==0


def test_viewer_does_not_read_tenant_workload_even_if_storage_is_unavailable():
    class NeverRead:
        def connect(self):
            raise AssertionError('restricted role accessed workload storage')
    report=workload_snapshot(NeverRead(),'demo','viewer')
    assert report['access']=='restricted' and not report['complete'] and report['capacity']==[]
    assert report['admissions']['counts'] is None and report['admissions']['reason']=='role_required'


def test_capacity_uses_admission_scope_and_bad_review_metadata_is_not_zero(client):
    store=client.app.state.store
    with store.connect() as db:
        for index,status in enumerate(['pending','action_required','dispatching','outcome_unknown','published']):
            db.execute('INSERT INTO outbox(tenant,id,topic,payload,status) VALUES(?,?,?,?,?)',('demo',str(index),'synthetic','{}',status))
        db.execute('INSERT INTO reviews VALUES(?,?,?)',('demo','bad','PRIVATE_MALFORMED_REVIEW'))
    report=workload_snapshot(store,'demo','auditor');rows={r['key']:r for r in report['capacity']}
    assert rows['outbox_active']['used']==4 and rows['reviews']['used']==1
    assert rows['review_bytes']['used'] is None and rows['review_bytes']['status']=='unknown'
    assert not report['complete'] and 'PRIVATE_MALFORMED_REVIEW' not in json.dumps(report)


def test_admission_detail_is_bounded_and_does_not_infer_failure_or_age(client):
    store=client.app.state.store
    with store.connect() as db:
        for n in range(25):
            db.execute("INSERT INTO edge_admissions VALUES(?,?,?,?,?,'received',NULL)",('demo',str(uuid4()),'a'*64,str(uuid4()),'synthetic-device'))
    report=workload_snapshot(store,'demo','operator')
    assert len(report['admissions']['items'])==20 and report['admissions']['truncated']
    assert report['admissions']['counts']['received']==25 and not report['complete']
    assert all(r['recordedAt'] is None and r['cause']=='not_recorded' for r in report['admissions']['items'])


def test_review_byte_aggregate_is_bounded_and_oversized_bodies_stay_unavailable(client):
    store=client.app.state.store
    with store.connect() as db:
        db.execute('INSERT INTO reviews VALUES(?,?,?)',('demo','valid',json.dumps({'manifest':{'byteLength':100}})))
    report=workload_snapshot(store,'demo','operator')
    assert next(r for r in report['capacity'] if r['key']=='review_bytes')['used']==100
    with store.connect() as db:
        db.execute('INSERT INTO reviews VALUES(?,?,?)',('demo','oversize',json.dumps({'manifest':{'byteLength':50},'private':'x'*65536})))
    report=workload_snapshot(store,'demo','operator')
    assert next(r for r in report['capacity'] if r['key']=='review_bytes')['used'] is None
    assert CAPACITIES['review_bytes']==256*1024*1024


def test_workload_api_role_is_checked_from_the_authenticated_claims(tmp_path,monkeypatch):
    import time
    import jwt
    from fastapi.testclient import TestClient
    from marine.api import create_app
    key=b'synthetic-workload-role-only-key-0000000000000000'
    path=tmp_path/'key';path.write_bytes(key);path.chmod(0o600)
    monkeypatch.setenv('MARINE_JWT_KEY_FILE',str(path))
    app=create_app(tmp_path/'state',mode='local')
    with TestClient(app) as http:
        for role,access in [('viewer','restricted'),('auditor','allowed')]:
            token=jwt.encode({'iss':'marine-local','aud':'marine-api','sub':'synthetic-'+role,'tenant':'demo','role':role,'iat':int(time.time()),'exp':int(time.time())+60},key,algorithm='HS256')
            response=http.get('/v1/operations',headers={'Authorization':'Bearer '+token})
            assert response.status_code==200 and response.json()['workload']['access']==access
            if access=='restricted':assert response.json()['workload']['capacity']==[] and response.json()['workload']['admissions']['counts'] is None
