import importlib.util
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_workspace_keeps_private_key_and_saved_data(tmp_path):
    module = script("start_app")
    data = tmp_path / "workspace"
    key = module.prepare_workspace(data)
    original = key.read_bytes()
    (data / "saved-report.json").write_text("synthetic-success")
    assert module.prepare_workspace(data).read_bytes() == original
    assert (data / "saved-report.json").read_text() == "synthetic-success"
    assert data.stat().st_mode & 0o777 == 0o700
    assert key.stat().st_mode & 0o777 == 0o600


def test_local_workspace_rejects_key_link_without_touching_target(tmp_path):
    module = script("start_app")
    data = tmp_path / "workspace"
    data.mkdir(mode=0o700)
    outside = tmp_path / "original"
    outside.write_bytes(b"original private key" * 3)
    (data / "jwt.key").symlink_to(outside)
    with pytest.raises(ValueError, match="LINK"):
        module.prepare_workspace(data)
    assert outside.read_bytes() == b"original private key" * 3


def test_distribution_allowlist_and_immutable_output(tmp_path):
    module = script("package_local_release")
    for path in [
        ".env",
        "artifacts/database/marine.sqlite3",
        "data/raw/image.png",
        "web/../jwt.key",
        "web/node_modules/data",
        "marine/.env",
    ]:
        assert not module.allowed(path)
    assert module.allowed("web/app.js")
    first = module.assemble({"web/index.html": b"synthetic"}, "0.3.0", "a" * 40, tmp_path)
    original = first.read_bytes()
    with pytest.raises(FileExistsError):
        module.assemble({"web/index.html": b"changed"}, "0.3.0", "a" * 40, tmp_path)
    assert first.read_bytes() == original


def test_packaged_review_can_export_confirmed_training_samples(tmp_path):
    """Exercise the HTTP export from the extracted app, without checkout imports."""
    import os
    import subprocess
    import sys
    import zipfile

    module = script("package_local_release")
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT, text=True
    ).split("\0")
    files = {name: (ROOT / name).read_bytes() for name in tracked if module.allowed(name)}
    archive = module.assemble(files, "0.4.1", "a" * 40, tmp_path / "package")
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(tmp_path / "installed")
    installed = tmp_path / "installed/marine-fisheries-0.4.1"
    check = tmp_path / "test_packaged_review.py"
    check.write_bytes((ROOT / "tests/test_review.py").read_bytes())
    quality_check = tmp_path / "test_packaged_quality.py"
    quality_check.write_bytes((ROOT / "tests/test_data_quality.py").read_bytes())
    lineage_check = tmp_path / "test_packaged_lineage.py"
    lineage_check.write_bytes((ROOT / "tests/test_lineage.py").read_bytes())
    (tmp_path / "test_api.py").write_bytes((ROOT / "tests/test_api.py").read_bytes())
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("MARINE_", "KAFKA_", "IPFS_", "FABRIC_"))
    }
    env.update(PYTHONPATH=str(installed), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(check), str(quality_check), str(lineage_check), "-q"],
        cwd=installed, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_distribution_tampering_and_other_process_rejected(tmp_path, monkeypatch):
    import hashlib
    import json

    module = script("start_app")
    path = tmp_path / "web"
    path.mkdir()
    (path / "index.html").write_bytes(b"synthetic")
    (tmp_path / "distribution.json").write_text(
        json.dumps({"files": {"web/index.html": hashlib.sha256(b"synthetic").hexdigest()}})
    )
    module.verify_distribution(tmp_path.resolve())
    (path / "index.html").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="DIGEST"):
        module.verify_distribution(tmp_path.resolve())
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **kw: "/other/app")
    assert not module.owned_process({"pid": 1234, "started": "synthetic", "port": 8871})


def test_installation_check_reports_package_version_without_touching_runtime_state(tmp_path):
    import subprocess
    import sys
    import tomllib
    state = tmp_path / "running.json"
    original = b'{"pid":99999999,"started":"synthetic-marker","port":8871}'
    state.write_bytes(original)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "tools/start_app.py"), "--check", "--data-dir", str(tmp_path)], capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report["installedVersion"] == tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert report["runtime"] == "not-checked"
    assert state.read_bytes() == original
    assert {path.name for path in tmp_path.iterdir()} == {"running.json"}

def test_local_port_probe_allows_an_immediate_restart_after_a_connection():
    module = script("start_app")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(2)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            connection, _ = listener.accept()
            with connection:
                connection.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""
    module.ensure_loopback_port_available(port)


def test_local_port_probe_refuses_an_active_listener_without_stopping_it():
    module = script("start_app")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        with pytest.raises(SystemExit, match="LOCAL_PORT_UNAVAILABLE"):
            module.ensure_loopback_port_available(port)
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            assert listener.fileno() >= 0
