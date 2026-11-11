"""Synthetic loopback child; output is confined to its private test directory."""
import http.server
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile

if len(sys.argv) != 3:
    raise SystemExit("FIXTURE_ARGUMENT_INVALID")
port, mode = int(sys.argv[1]), sys.argv[2]
if not 1 <= port <= 65535 or mode not in {"normal", "hang", "managed"}:
    raise SystemExit("FIXTURE_ARGUMENT_INVALID")
# There is no output-path argument. The caller exclusively creates this directory.
root = Path(tempfile.gettempdir()).resolve() / ("native-fixture-" + str(port))
directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
info = os.fstat(directory_fd)
if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
    os.close(directory_fd)
    raise SystemExit("FIXTURE_DIRECTORY_INVALID")


def write_result(name, content):
    # Fixed filenames, a held directory descriptor and exclusive creation prevent escapes.
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


if mode == "hang":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/target")
            self.end_headers()
            return
        if self.path == "/target":
            try:
                write_result("result.redirected", "unexpected")
            except FileExistsError:
                pass
        body = b'{"service":"fixture-ready"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
write_result("result.json", json.dumps({"pid": os.getpid(), "cwd": os.getcwd()}))
if mode == "managed":
    write_result("result.state.json", json.dumps({"pid": os.getpid(), "port": port, "started": subprocess.check_output(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="], text=True).strip()}))
server.serve_forever()
