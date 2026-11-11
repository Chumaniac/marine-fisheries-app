import pytest
from marine.backup import backup, restore, verify
from marine.storage import Store, canonical
from marine.trace import EvidenceStore


def test_online_backup_recovery_and_tamper(tmp_path):
    source = tmp_path / "source"
    store = Store(source / "marine.sqlite3")
    objects = EvidenceStore(source)
    manifest = objects.put(b"private synthetic evidence", "batch", "A")
    with store.connect() as db:
        db.execute(
            "INSERT INTO evidence(tenant,id,batch,manifest,status) VALUES(?,?,?,?,?)",
            ("A", manifest["evidenceId"], "batch", canonical(manifest).decode(), "LOCAL_PENDING"),
        )
    assert backup(source, tmp_path / "bundle")["evidenceObjects"] == 1
    restore(tmp_path / "bundle", tmp_path / "restored")
    assert EvidenceStore(tmp_path / "restored").read(manifest) == b"private synthetic evidence"
    with pytest.raises(ValueError):
        restore(tmp_path / "bundle", source)
    (tmp_path / "bundle" / "objects" / manifest["cipherSha256"]).write_bytes(b"tamper")
    with pytest.raises(ValueError):
        verify(tmp_path / "bundle")


def test_interrupted_restore_leaves_destination_retryable(tmp_path, monkeypatch):
    import shutil

    source = tmp_path / "s"
    Store(source / "marine.sqlite3")
    EvidenceStore(source)
    backup(source, tmp_path / "bundle")
    original = shutil.copyfile

    def fail(*args, **kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr(shutil, "copyfile", fail)
    with pytest.raises(OSError):
        restore(tmp_path / "bundle", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    monkeypatch.setattr(shutil, "copyfile", original)
    assert restore(tmp_path / "bundle", tmp_path / "restored")["status"] == "verified"


def test_backup_inventory_stays_stable_after_connection_collection(tmp_path):
    import gc
    source = tmp_path / 'source'
    Store(source / 'marine.sqlite3')
    EvidenceStore(source)
    bundle = tmp_path / 'bundle'
    backup(source, bundle)
    before = {p.relative_to(bundle) for p in bundle.rglob('*') if p.is_file()}
    assert not any(str(p).endswith(('-wal', '-shm')) for p in before)
    gc.collect()
    assert verify(bundle)['status'] == 'verified'
    assert before == {p.relative_to(bundle) for p in bundle.rglob('*') if p.is_file()}
