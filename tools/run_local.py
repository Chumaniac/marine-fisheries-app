"""Start/stop only this repository's native API, Gateway and Kafka worker."""

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
RUNTIME.mkdir(exist_ok=True)


def main(action):
    lock = (RUNTIME / "services.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if action == "stop":
            identity = json.loads((RUNTIME / "services.json").read_text())
            os.kill(identity["supervisorPid"], signal.SIGTERM)
            print("Stop requested for this project supervisor")
            return
        raise SystemExit("This project supervisor is already running") from None
    if action == "stop":
        print("No project supervisor is running")
        return
    java = Path(os.environ.get("JAVA_HOME", "")) / "bin/java"
    if not java.is_file():
        raise SystemExit("Set JAVA_HOME to an installed JDK 17+ before starting")
    environment = {
        **os.environ,
        "KAFKA_BOOTSTRAP": "127.0.0.1:19092",
        "IPFS_API": "http://127.0.0.1:15001",
        "MARINE_BACKUP_RECEIPT_FILE": os.getenv(
            "MARINE_BACKUP_RECEIPT_FILE", str(ROOT / "artifacts/database/backup-receipt.json")
        ),
        "MARINE_FABRIC_ROOT": str(ROOT / ".runtime/fabric-samples/test-network"),
        "MARINE_PEER_BINARY": str(ROOT / ".runtime/fabric/bin/peer"),
    }
    commands = {
        "api": [
            str(ROOT / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "marine.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            "8870",
        ],
        "gateway": [
            str(java),
            "-Xmx256m",
            f"-Dmarine.project={ROOT}",
            "-jar",
            str(ROOT / "jvm/gateway/target/gateway-0.2.0.jar"),
        ],
        "worker": [str(ROOT / ".venv/bin/python"), "-u", "-m", "marine.worker"],
    }
    (ROOT / "artifacts").mkdir(exist_ok=True)
    children = []
    logs = []

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        for name, command in commands.items():
            output = (ROOT / f"artifacts/{name}.log").open("a")
            logs.append(output)
            children.append(
                subprocess.Popen(
                    command, cwd=ROOT, env=environment, stdout=output, stderr=subprocess.STDOUT
                )
            )
        (RUNTIME / "services.json").write_text(
            json.dumps(
                {
                    "supervisorPid": os.getpid(),
                    "children": dict(zip(commands, [p.pid for p in children])),
                }
            )
        )
        print(
            "本地应用：http://127.0.0.1:8871/（请保持此管理进程运行）", flush=True
        )
        while all(process.poll() is None for process in children):
            time.sleep(1)
        print(
            "某项服务已退出，请查看 artifacts/api.log、gateway.log、worker.log", file=sys.stderr
        )
    except KeyboardInterrupt:
        pass
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
        for process in children:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for log in logs:
            log.close()
        lock.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["start", "stop"], default="start", nargs="?")
    main(p.parse_args().action)
