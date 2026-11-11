"""Run the private local application with durable keys and bounded owned processes."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import secrets
import signal
import socket
import subprocess
import threading
import tomllib
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


def verify_distribution(root: Path) -> None:
    manifest = root / "distribution.json"
    if not manifest.exists():
        if (root / ".git").exists():
            return
        raise ValueError("DISTRIBUTION_MANIFEST_MISSING")
    if manifest.stat().st_size > 1_000_000:
        raise ValueError("DISTRIBUTION_MANIFEST_INVALID")
    value = json.loads(manifest.read_text())
    files = value.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 2000:
        raise ValueError("DISTRIBUTION_MANIFEST_INVALID")
    total = 0
    for name, expected in files.items():
        parts = PurePosixPath(name).parts
        if (
            not parts
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or "\\" in name
        ):
            raise ValueError("DISTRIBUTION_PATH_INVALID")
        path = root.joinpath(*parts)
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ValueError("DISTRIBUTION_LINK_INVALID")
        if not path.is_file() or path.stat().st_size > 10_000_000:
            raise ValueError("DISTRIBUTION_FILE_INVALID")
        total += path.stat().st_size
        if total > 32 * 1024 * 1024:
            raise ValueError("DISTRIBUTION_CAPACITY")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise ValueError("DISTRIBUTION_DIGEST_MISMATCH")


def process_stamp(pid: int) -> str:
    return subprocess.check_output(["ps", "-p", str(pid), "-o", "lstart="], text=True, env={**os.environ, "LC_ALL": "C"}).strip()


def owned_process(state: dict) -> bool:
    if (
        not isinstance(state, dict)
        or type(state.get("pid")) is not int
        or state["pid"] <= 1
        or not isinstance(state.get("started"), str)
        or not state["started"]
        or type(state.get("port")) is not int
        or not 1024 <= state["port"] <= 65535
    ):
        return False
    try:
        command = subprocess.check_output(
            ["ps", "-p", str(state["pid"]), "-o", "command="], text=True,
            env={**{key: value for key, value in os.environ.items() if key != "LC_ALL"}, "LC_CTYPE": "en_US.UTF-8" if platform.system() == "Darwin" else "C.UTF-8", "LC_TIME": "C"}
        )
        return (
            str(ROOT / "tools/start_app.py") in command
            and process_stamp(state["pid"]) == state["started"]
        )
    except (ProcessLookupError, subprocess.CalledProcessError):
        return False


def prepare_workspace(data: Path) -> Path:
    if data.is_symlink():
        raise ValueError("WORKSPACE_LINK_INVALID")
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data.stat().st_mode & 0o077:
        raise ValueError("WORKSPACE_PERMISSIONS_INVALID")
    key = data / "jwt.key"
    if key.is_symlink():
        raise ValueError("WORKSPACE_KEY_LINK_INVALID")
    try:
        fd = os.open(key, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if not key.is_file() or key.stat().st_mode & 0o077 or not 32 <= key.stat().st_size <= 128:
            raise ValueError("WORKSPACE_KEY_INVALID")
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(48))
            stream.flush()
            os.fsync(stream.fileno())
    return key


def ensure_loopback_port_available(port: int) -> None:
    """Match server address reuse while refusing an active listener."""
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit("LOCAL_PORT_UNAVAILABLE; existing service left unchanged") from None


def retire_stale_state(running: Path, state: dict) -> None:
    """Remove metadata only after both its process and loopback listener are gone."""
    if (not isinstance(state, dict) or type(state.get('pid')) is not int or state['pid'] <= 1
            or type(state.get('port')) is not int or not 1024 <= state['port'] <= 65535):
        raise ValueError('LOCAL_APP_STATE_INVALID')
    try:
        os.kill(state['pid'], 0)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise SystemExit('LOCAL_APP_PROCESS_CHANGED; existing metadata and service preserved') from None
    else:
        raise SystemExit('LOCAL_APP_PROCESS_CHANGED; existing metadata and service preserved')
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', state['port']))
        except OSError:
            raise SystemExit('LOCAL_PORT_UNAVAILABLE; existing metadata and service preserved') from None
    running.unlink()


def main() -> int:
    default = (
        Path.home() / "Library/Application Support/MarineFisheries/workspace"
        if platform.system() == "Darwin"
        else Path.home() / ".local/share/marine-fisheries/workspace"
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=default)
    parser.add_argument("--port", type=int, default=8871)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--check-env", action="store_true", help="Offline dependency version and installed-file check")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    data = args.data_dir.expanduser().absolute()
    running = data / "running.json"
    if args.stop:
        if not running.is_file() or running.stat().st_size > 4096:
            raise SystemExit("LOCAL_APP_NOT_RUNNING")
        state = json.loads(running.read_text())
        if not owned_process(state):
            raise SystemExit("LOCAL_APP_PROCESS_CHANGED; no process stopped")
        os.kill(state["pid"], signal.SIGTERM)
        print("已请求停止本地应用；已保存的数据和证据均保留。")
        return 0
    if not 1024 <= args.port <= 65535:
        raise ValueError("LOCAL_PORT_INVALID")
    verify_distribution(ROOT)
    if args.check:
        print(
            json.dumps(
                {
                    "installedVersion": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
                    "runtime": "not-checked",
                    "workspaceExists": data.is_dir(),
                    "mode": "local",
                    "modelInstalled": (data.parent / "models/norfisk/model.onnx").is_file(),
                    "production": False,
                }
            )
        )
        return 0
    from runtime_environment import check_environment, environment_lock
    if args.check_env:
        report = check_environment(ROOT)
        print(json.dumps(report))
        return 0 if report['ready'] else 2
    if running.exists():
        if running.stat().st_size > 4096:
            raise ValueError("LOCAL_APP_STATE_INVALID")
        state = json.loads(running.read_text())
        if owned_process(state):
            url = f"http://127.0.0.1:{state['port']}/"
            print("本地应用已在运行： " + url)
            if not args.no_open and not args.check:
                webbrowser.open(url)
            return 0
        retire_stale_state(running, state)
    # Embedded application resources are immutable after signing. Lock the private workspace.
    lock_root = data if (ROOT / 'embedded-runtime.json').is_file() else ROOT
    if lock_root == data:
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
    with environment_lock(lock_root):
        report = check_environment(ROOT)
        if not report['ready']:
            print(json.dumps(report))
            return 2
        return run_application(args, data, running)


def run_application(args, data, running):
    key = prepare_workspace(data)
    # This entry point starts local services only; unrelated project shell settings are not inherited.
    for name in list(os.environ):
        if name.startswith(("MARINE_", "IPFS_", "KAFKA_")):
            os.environ.pop(name)
    os.environ.update(MARINE_MODE="local", MARINE_DATA_DIR=str(data), MARINE_JWT_KEY_FILE=str(key))
    models = data.parent / "models/norfisk"
    if (models / "model.onnx").is_file():
        os.environ["MARINE_MODEL_DIR"] = str(models)
    protected = data.parent / "models/protected-manifest.csv"
    if protected.is_file():
        os.environ["MARINE_PROTECTED_MANIFEST"] = str(protected)
    ensure_loopback_port_available(args.port)
    os.chdir(ROOT)
    import uvicorn
    from marine.api import create_app

    app = create_app(data, mode="local")
    fd = os.open(running, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(
            {"pid": os.getpid(), "started": process_stamp(os.getpid()), "port": args.port}, stream
        )
    stop = threading.Event()
    url = f"http://127.0.0.1:{args.port}/"

    def open_when_ready():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            if stop.wait(0.2):
                return
            try:
                opener.open(url, timeout=1).close()
                webbrowser.open(url)
                return
            except OSError:
                pass

    if not args.no_open:
        threading.Thread(target=open_when_ready, daemon=True).start()
    print("打开 " + url + "（本地模式；按 Ctrl+C 停止，已保存数据会保留）", flush=True)
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            log_level="warning",
            limit_concurrency=48,
            timeout_keep_alive=5,
        )
    finally:
        stop.set()
        if running.is_file():
            try:
                if json.loads(running.read_text()).get("pid") == os.getpid():
                    running.unlink()
            except (OSError, ValueError):
                pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError):
        raise SystemExit(
            "本地程序或工作区不可用（LOCAL_DISTRIBUTION_OR_WORKSPACE_INVALID）；未输出私人配置"
        ) from None
