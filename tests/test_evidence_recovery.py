"""Synthetic ciphertext/SQLite acknowledgement failures; no model or remote writes."""
from contextlib import contextmanager
import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from marine.api import create_app
from marine.edge_samples import CaptureJournal


def sample():
    event = json.loads(Path('contracts/examples.json').read_text())[0]
    event.update(tenantId='demo', deviceId='synthetic-device', siteId='A', occurredAt='2026-01-01T00:00:00Z')
    event['data']['confidence'] = 0.5
    _, image = cv2.imencode('.png', np.zeros((24, 24, 3), np.uint8))
    return event, image.tobytes()


def test_rolled_back_capture_removes_only_its_owned_unreferenced_cipher(tmp_path, monkeypatch):
    journal = CaptureJournal(tmp_path / 'edge.db')
    old = journal.objects.put(b'preserve unrelated original', 'old', 'demo')
    connect = journal.outbox.connect
    @contextmanager
    def failed_commit():
        with connect() as db:
            yield db
            raise RuntimeError('synthetic rollback before commit')
    monkeypatch.setattr(journal.outbox, 'connect', failed_commit)
    event, image = sample()
    with pytest.raises(RuntimeError):
        journal.capture(event, image)
    assert {p.name for p in journal.objects.folder.iterdir()} == {old['cipherSha256']}
    assert journal.objects.read(old) == b'preserve unrelated original'


def test_committed_capture_with_lost_acknowledgement_keeps_the_original_cipher(tmp_path, monkeypatch):
    journal = CaptureJournal(tmp_path / 'edge.db')
    connect = journal.outbox.connect
    @contextmanager
    def lost_acknowledgement():
        with connect() as db:
            yield db
        raise RuntimeError('synthetic acknowledgement lost after commit')
    monkeypatch.setattr(journal.outbox, 'connect', lost_acknowledgement)
    event, image = sample()
    with pytest.raises(RuntimeError):
        journal.capture(event, image)
    with connect() as db:
        manifest = json.loads(db.execute('SELECT manifest FROM captured').fetchone()[0])
    assert journal.objects.read(manifest) == image


def test_partial_admission_has_a_readable_recovery_phase_and_retries_keep_identity(tmp_path):
    app = create_app(tmp_path / 'server')
    class FailedClassifier:
        def predict(self, *args, **kwargs):
            raise RuntimeError('synthetic inference interruption')
    app.state.classifier = FailedClassifier()
    event, image = sample()
    with TestClient(app, raise_server_exceptions=False) as client:
        actor = client.post('/v1/session', headers={'X-Requested-With': 'marine-web'}).json()
        client.headers['X-Marine-Scope'] = actor['scopeHash']
        body = {'event': json.dumps(event), 'needs_review': 'true'}
        assert client.post('/v1/edge-samples', data=body, files={'image': ('crop.png', image, 'image/png')}).status_code == 500
        receipt = client.get('/v1/edge-samples/' + event['eventId']).json()
        assert receipt['status'] == 'action_required'
        assert receipt['phase'] == 'evidence_saved'
        assert receipt['nextAction'] == 'reconcile_original_request'
        assert client.get("/v1/edge-samples/not-an-id%27%20OR%201=1").status_code == 404
        class Classifier:
            def predict(self, *args, **kwargs):
                return {'speciesId':'saithe','confidence':0.5,'probabilities':{'saithe':0.5},'modelVersion':'synthetic','_embedding':[1.0,0.0]}
        app.state.classifier = Classifier()
        retried = client.post('/v1/edge-samples', data=body, files={'image': ('crop.png', image, 'image/png')})
        assert retried.status_code == 200
        assert retried.json()['sampleId'] == event['eventId']
        with app.state.store.connect() as db:
            assert db.execute('SELECT count(*) FROM edge_admissions').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM edge_evidence').fetchone()[0] == 1


def test_context_ownership_does_not_cross_concurrent_transactions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Event
    from marine.evidence_store import EvidenceStore
    from marine.storage import Store, canonical
    objects = EvidenceStore(tmp_path / 'objects-owner')
    failed, saved = Store(tmp_path / 'failed.sqlite3'), Store(tmp_path / 'saved.sqlite3')
    barrier, released = Barrier(2), Event()
    manifests = {}
    def failing():
        try:
            with pytest.raises(RuntimeError):
                with objects.transaction(failed.path), failed.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    manifests['failed'] = objects.put(b'synthetic rolled back', 'one', 'demo')
                    barrier.wait(timeout=5)
                    raise RuntimeError('synthetic rollback')
        finally:
            released.set()
    def committing():
        with objects.transaction(saved.path), saved.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            manifest = objects.put(b'synthetic committed', 'two', 'demo')
            manifests['saved'] = manifest
            barrier.wait(timeout=5)
            assert released.wait(timeout=5)
            db.execute('INSERT INTO evidence(tenant,id,batch,manifest,status) VALUES(?,?,?,?,?)',
                       ('demo', manifest['evidenceId'], 'two', canonical(manifest).decode(), 'LOCAL_PENDING'))
    with ThreadPoolExecutor(2) as executor:
        a, b = executor.submit(failing), executor.submit(committing)
        a.result(timeout=10)
        b.result(timeout=10)
    assert not (objects.folder / manifests['failed']['cipherSha256']).exists()
    assert objects.read(manifests['saved']) == b'synthetic committed'


def test_unknown_commit_state_preserves_material_and_metadata_observation_never_cleans(tmp_path, monkeypatch):
    from marine.evidence_store import EvidenceStore
    from marine.storage import Store
    from marine import evidence_recovery
    store, objects = Store(tmp_path / 'marine.sqlite3'), EvidenceStore(tmp_path)
    with monkeypatch.context() as context:
        context.setattr(evidence_recovery, 'referenced', lambda *args: None)
        with pytest.raises(RuntimeError):
            with objects.transaction(store.path):
                manifest = objects.put(b'uncertain synthetic result', 'one', 'demo')
                raise RuntimeError('synthetic inaccessible database acknowledgement')
    before = {p.name:p.read_bytes() for p in objects.folder.iterdir()}
    result = evidence_recovery.recovery_status(store.path, objects.folder)
    assert result['readOnly'] is True and result['unreferenced'] == 1
    assert {p.name:p.read_bytes() for p in objects.folder.iterdir()} == before
    assert objects.read(manifest) == b'uncertain synthetic result'
    with pytest.raises(ValueError):
        evidence_recovery.recovery_status(store.path, objects.folder, 10001)


def test_a_conflicting_event_is_never_reported_as_the_original_sample_publication(tmp_path):
    from marine.storage import canonical
    app = create_app(tmp_path / 'server')
    class FailedClassifier:
        def predict(self, *args, **kwargs):
            raise RuntimeError('synthetic pause before review')
    app.state.classifier = FailedClassifier()
    event, image = sample()
    with TestClient(app, raise_server_exceptions=False) as client:
        actor = client.post('/v1/session', headers={'X-Requested-With':'marine-web'}).json()
        client.headers['X-Marine-Scope'] = actor['scopeHash']
        client.post('/v1/edge-samples', data={'event':json.dumps(event),'needs_review':'true'}, files={'image':('crop.png',image,'image/png')})
        forged = {**event, 'data':{**event['data'], 'evidenceId':'different-synthetic-original'}}
        with app.state.store.connect() as db:
            db.execute('INSERT INTO events(tenant,id,payload,hash,created) VALUES(?,?,?,?,?)', ('demo',event['eventId'],canonical(forged).decode(),'synthetic',1))
        receipt = client.get('/v1/edge-samples/'+event['eventId']).json()
        assert receipt['status'] == 'action_required' and receipt['phase'] == 'event_binding_conflict'


def test_cipher_limits_and_corrupt_key_refuse_without_resetting_existing_data(tmp_path):
    from marine.evidence_store import EvidenceStore, MAX_EVIDENCE_BYTES
    objects = EvidenceStore(tmp_path)
    with pytest.raises(ValueError):
        objects.put(b'x'*(MAX_EVIDENCE_BYTES+1),'one','demo')
    key = tmp_path / 'evidence.key'
    key.write_bytes(b'wrong synthetic key')
    with pytest.raises(ValueError):
        EvidenceStore(tmp_path)
    assert key.read_bytes() == b'wrong synthetic key'


def test_missing_key_with_old_objects_never_generates_a_replacement_key(tmp_path):
    from marine.evidence_store import EvidenceStore
    objects = EvidenceStore(tmp_path)
    original = objects.put(b'preserve encrypted original', 'one', 'demo')
    key = tmp_path / 'evidence.key'
    key.unlink()
    with pytest.raises(ValueError, match='key is missing'):
        EvidenceStore(tmp_path)
    assert not key.exists()
    assert objects.read(original) == b'preserve encrypted original'


def test_temporary_name_collision_never_deletes_a_file_this_operation_did_not_create(tmp_path, monkeypatch):
    from marine.evidence_store import EvidenceStore
    from marine import evidence_store
    import os
    objects = EvidenceStore(tmp_path)
    actual_open = evidence_store.os.open
    collided = []
    def conflict(path, flags, mode=0o777, **kwargs):
        if isinstance(path, Path) and path.parent == objects.folder and '.' in path.name:
            descriptor = actual_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(descriptor, b'preserve unrelated temporary material')
            os.close(descriptor)
            collided.append(path)
            raise FileExistsError('synthetic name collision')
        return actual_open(path, flags, mode, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(evidence_store.os, 'open', conflict)
        with pytest.raises(FileExistsError):
            objects.put(b'synthetic new evidence', 'one', 'demo')
    assert len(collided) == 1
    assert collided[0].read_bytes() == b'preserve unrelated temporary material'


def test_human_review_committed_before_publication_is_not_sent_back_for_another_label(tmp_path):
    from marine.storage import canonical
    app = create_app(tmp_path / 'server')
    class Classifier:
        def predict(self, *args, **kwargs):
            return {'speciesId':'saithe','confidence':0.5,'probabilities':{'saithe':0.5},'modelVersion':'synthetic','_embedding':[1.0,0.0]}
    app.state.classifier = Classifier()
    event, image = sample()
    body = {'event':json.dumps(event),'needs_review':'true'}
    with TestClient(app) as client:
        actor = client.post('/v1/session', headers={'X-Requested-With':'marine-web'}).json()
        client.headers['X-Marine-Scope'] = actor['scopeHash']
        first = client.post('/v1/edge-samples', data=body, files={'image':('crop.png',image,'image/png')}).json()
        review_id = first['reviewId']
        with app.state.store.connect() as db:
            review = json.loads(db.execute('SELECT payload FROM reviews WHERE tenant=? AND id=?', ('demo',review_id)).fetchone()[0])
            review.update(state='reviewed', label='saithe')
            db.execute('UPDATE reviews SET payload=? WHERE tenant=? AND id=?', (canonical(review).decode(),'demo',review_id))
        paused = client.get('/v1/edge-samples/'+event['eventId']).json()
        assert paused['status'] == 'action_required' and paused['phase'] == 'reviewed_publication_pending'
        assert paused['nextAction'] == 'reconcile_original_request'
        repeated = client.post('/v1/edge-samples', data=body, files={'image':('crop.png',image,'image/png')})
        assert repeated.status_code == 200
        assert client.get('/v1/edge-samples/'+event['eventId']).json()['status'] == 'published'
        with app.state.store.connect() as db:
            assert db.execute('SELECT count(*) FROM reviews').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1


def test_successful_commit_does_not_scan_reference_tables_and_peer_admission_is_rechecked(tmp_path, monkeypatch):
    from marine import evidence_recovery
    from marine.storage import canonical
    import hashlib
    app = create_app(tmp_path / 'server')
    event, image = sample()
    event['data']['confidence'] = 0.95
    prior = app.state.evidence_store.put(image, 'edge-capture', 'demo')
    connect = app.state.store.connect
    peer_committed = []
    @contextmanager
    def peer_before_write_transaction():
        if evidence_recovery.active_operation(app.state.evidence_store) and not peer_committed:
            peer_committed.append(True)
            with connect() as db:
                db.execute('INSERT INTO edge_evidence VALUES(?,?,?,?,?)', ('demo',event['data']['evidenceId'],event['deviceId'],canonical(prior).decode(),hashlib.sha256(image).hexdigest()))
        with connect() as db:
            yield db
    with TestClient(app) as client:
        actor = client.post('/v1/session', headers={'X-Requested-With':'marine-web'}).json()
        client.headers['X-Marine-Scope'] = actor['scopeHash']
        monkeypatch.setattr(app.state.store, 'connect', peer_before_write_transaction)
        def no_success_scan(*args):
            raise AssertionError('Successful commits must not perform unindexed recovery scans')
        monkeypatch.setattr(evidence_recovery, 'referenced', no_success_scan)
        result = client.post('/v1/edge-samples', data={'event':json.dumps(event),'needs_review':'false'}, files={'image':('crop.png',image,'image/png')})
        assert result.status_code == 200
        assert peer_committed and {p.name for p in app.state.evidence_store.folder.iterdir()} == {prior['cipherSha256']}


def test_successful_new_object_commit_never_runs_failure_reconciliation_scans(tmp_path, monkeypatch):
    from marine import evidence_recovery
    from marine.evidence_store import EvidenceStore
    from marine.storage import Store, canonical
    store, objects = Store(tmp_path / 'marine.sqlite3'), EvidenceStore(tmp_path)
    def unexpected(*args):
        raise AssertionError('No reference scan on the successful write path')
    monkeypatch.setattr(evidence_recovery, 'referenced', unexpected)
    with objects.transaction(store.path), store.connect() as db:
        manifest = objects.put(b'synthetic committed original', 'one', 'demo')
        db.execute('INSERT INTO evidence(tenant,id,batch,manifest,status) VALUES(?,?,?,?,?)',
                   ('demo',manifest['evidenceId'],'one',canonical(manifest).decode(),'LOCAL_PENDING'))
    assert objects.read(manifest) == b'synthetic committed original'


def test_application_with_old_database_references_never_silently_rekeys_an_empty_object_folder(tmp_path):
    from marine.storage import canonical
    app = create_app(tmp_path / 'server')
    manifest = app.state.evidence_store.put(b'synthetic missing storage', 'one', 'demo')
    with app.state.store.connect() as db:
        db.execute('INSERT INTO evidence(tenant,id,batch,manifest,status) VALUES(?,?,?,?,?)',
                   ('demo',manifest['evidenceId'],'one',canonical(manifest).decode(),'LOCAL_PENDING'))
    (app.state.evidence_store.folder / manifest['cipherSha256']).unlink()
    key = tmp_path / 'server/evidence.key'
    key.unlink()
    with pytest.raises(ValueError, match='key is missing'):
        create_app(tmp_path / 'server')
    assert not key.exists()


def test_retirement_captures_and_preserves_a_concurrent_replacement_instead_of_unlinking_it(tmp_path, monkeypatch):
    from marine.evidence_store import EvidenceStore
    from marine.storage import Store
    from marine import evidence_store
    store, objects = Store(tmp_path / 'marine.sqlite3'), EvidenceStore(tmp_path)
    actual_rename = evidence_store.os.rename
    replaced = []
    def concurrent(source, destination, **kwargs):
        if Path(source).parent == objects.folder and Path(destination).parent.name.startswith('.recovery-'):
            Path(source).unlink()
            Path(source).write_bytes(b'foreign material at a replaced filename')
            replaced.append(Path(source))
        return actual_rename(source, destination, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(evidence_store.os, 'rename', concurrent)
        with pytest.raises(RuntimeError):
            with objects.transaction(store.path):
                objects.put(b'synthetic failed write', 'one', 'demo')
                raise RuntimeError('synthetic DB rollback')
    assert len(replaced) == 1
    assert replaced[0].read_bytes() == b'foreign material at a replaced filename'
    from marine.evidence_recovery import recovery_status
    assert recovery_status(store.path, objects.folder)['complete'] is False
