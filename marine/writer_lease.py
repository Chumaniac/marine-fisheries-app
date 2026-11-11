"""Single active API writer per persistent volume; standby must remain fenced."""

import fcntl
from pathlib import Path


class WriterLease:
    def __init__(self, folder):
        self.file = (Path(folder) / ".api-writer.lock").open("a")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            raise RuntimeError("Another API writer owns this data volume") from None

    def close(self):
        self.file.close()
