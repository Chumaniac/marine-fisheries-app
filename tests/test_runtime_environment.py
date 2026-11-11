"""Dependency checks are offline and refuse missing or modified installed files."""
import base64
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]

def module():
    spec = importlib.util.spec_from_file_location('runtime_environment', ROOT/'tools/runtime_environment.py')
    value = importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def test_daily_launcher_never_synchronizes_dependencies():
    text=(ROOT/'Start Marine.command').read_text()
    assert 'pip sync' not in text and 'uv venv' not in text
    assert 'Prepare Marine.command' in text


def test_integrity_check_is_offline_and_detects_tampered_runtime(tmp_path,monkeypatch):
    value=module(); root=tmp_path.resolve();(root/'requirements.local.lock').write_text('synthetic==1.2.3\n')
    package=root/'synthetic.py';package.write_bytes(b'original synthetic runtime')
    digest=base64.urlsafe_b64encode(hashlib.sha256(package.read_bytes()).digest()).rstrip(b'=').decode()
    class File:
        hash=SimpleNamespace(mode='sha256',value=digest)
        def __str__(self):return 'synthetic.py'
    dist=SimpleNamespace(version='1.2.3',files=[File()],locate_file=lambda _:package)
    monkeypatch.setattr(value.metadata,'distribution',lambda _:dist)
    monkeypatch.setattr(value.sys,'prefix',str(root))
    assert value.check_environment(root)['ready']
    package.write_bytes(b'tampered synthetic runtime')
    assert value.check_environment(root)['reason']=='installed_file_mismatch'
    package.unlink();assert not value.check_environment(root)['ready']


def test_missing_or_wrong_version_cannot_be_repaired_by_a_status_read(tmp_path,monkeypatch):
    value=module();root=tmp_path.resolve();(root/'requirements.local.lock').write_text('synthetic==1.2.3\n')
    monkeypatch.setattr(value.metadata,'distribution',lambda _:SimpleNamespace(version='9.0',files=[]))
    assert value.check_environment(root)['reason']=='installed_version_mismatch'
    assert {p.name for p in root.iterdir()}=={'requirements.local.lock'}


def test_preparation_cannot_replace_an_environment_in_use(tmp_path):
    import pytest
    value=module();root=tmp_path.resolve()
    with value.environment_lock(root):
        with value.environment_lock(root):
            with pytest.raises(ValueError,match='ENVIRONMENT_IN_USE'):
                with value.environment_lock(root,exclusive=True):
                    pytest.fail('preparation must not enter')
    with value.environment_lock(root,exclusive=True):
        pass
    assert (root/'.marine-runtime.lock').stat().st_mode & 0o777 == 0o600


def test_retained_environments_have_a_hard_cap_without_automatic_deletion(tmp_path):
    import pytest
    value=module();root=tmp_path.resolve()
    for i in range(4):
        folder=root/f'.venv.previous-{i}';folder.mkdir();(folder/'keep').write_bytes(b'previous synthetic environment')
    with pytest.raises(ValueError,match='RETENTION_CAPACITY'):
        value.retained_capacity(root)
    assert len(list(root.glob('.venv.previous-*')))==4
    assert all(p.read_bytes()==b'previous synthetic environment' for p in root.glob('.venv.previous-*/keep'))
