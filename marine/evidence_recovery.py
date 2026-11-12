"""Transaction-scoped ownership and read-only evidence reconciliation.

Never sweep old objects. A failed new object can be removed only after a
fresh database read proves it unreferenced; unknown outcomes retain the file.
"""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sqlite3
import time

MAX_PENDING_OBJECTS = 64
MAX_AUDIT_FILES = 10000
MAX_REFERENCE_BYTES = 256 * 1024
REFERENCES = [('evidence', 'manifest', '$.cipherSha256'), ('edge_evidence', 'manifest', '$.cipherSha256'),
              ('reviews', 'payload', '$.manifest.cipherSha256'), ('captured', 'manifest', '$.cipherSha256')]


@dataclass
class _Operation:
    owner: object
    database: str
    objects: list = field(default_factory=list)


_current = ContextVar('marine_evidence_operation', default=None)


def active_operation(owner):
    operation = _current.get()
    return operation if operation is not None and operation.owner is owner else None


def referenced(database, cipher_hash, deadline=None):
    """True/False/None: referenced, proven absent, or could not safely determine."""
    if not re.fullmatch(r'[a-f0-9]{64}', cipher_hash):
        return None
    deadline = deadline or time.monotonic() + 2
    if time.monotonic() >= deadline:
        return None
    try:
        raw = Path(database)
        if raw.is_symlink() or not raw.is_file():
            return None
        path = raw.resolve(strict=True)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_REFERENCE_BYTES)
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 129")}
            if len(tables) > 128:
                return None
            relevant = False
            for table, column, json_path in REFERENCES:
                if table not in tables:
                    continue
                relevant = True
                # Names/paths come from the fixed whitelist above, never from input.
                expression = f"CASE WHEN length(CAST({column} AS BLOB))<={MAX_REFERENCE_BYTES} THEN CASE WHEN json_valid({column}) THEN json_extract({column},'{json_path}') END END"
                if db.execute(f'SELECT 1 FROM {table} WHERE ({expression})=? LIMIT 1', (cipher_hash,)).fetchone():
                    return True
                if db.execute(f'SELECT 1 FROM {table} WHERE ({expression}) IS NULL LIMIT 1').fetchone():
                    return None
            return False if relevant else None
    except (OSError, ValueError, sqlite3.Error):
        return None


def material_exists(database):
    """A known reference requires the original key; uncertainty is never 'fresh'."""
    deadline = time.monotonic() + 2
    try:
        path = Path(database).resolve(strict=True)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
            db.execute('PRAGMA query_only=ON')
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_REFERENCE_BYTES)
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 129")}
            if len(tables) > 128:
                return True
            return any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table, _, _ in REFERENCES if table in tables)
    except (OSError, ValueError, sqlite3.Error):
        return True


@contextmanager
def transaction(owner, database):
    current = active_operation(owner)
    if current is not None and current.database == str(database):
        yield
        return
    operation = _Operation(owner, str(database))
    token = _current.set(operation)
    failed = False
    try:
        yield
    except BaseException:
        failed = True
        raise
    finally:
        _current.reset(token)
        deadline = time.monotonic() + 2
        for cipher_hash, stamp in operation.objects if failed else []:
            if referenced(operation.database, cipher_hash, deadline) is False:
                owner.discard_owned(cipher_hash, stamp)


def recovery_status(database, objects, limit=256):
    """Bounded metadata-only observation; no key reads, deletion or DB writes."""
    if type(limit) is not int or not 1 <= limit <= MAX_AUDIT_FILES:
        raise ValueError('Invalid evidence observation limit')
    import os
    result = {'schema': 'marine.evidence-recovery/v1', 'readOnly': True, 'checksContentHashes': False,
              'referenced': 0, 'unreferenced': 0, 'unconfirmed': 0, 'temporary': 0, 'other': 0,
              'scanned': 0, 'complete': True}
    folder = Path(objects)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Regular evidence directory required')
    deadline = time.monotonic() + 2
    with os.scandir(folder) as entries:
        for entry in entries:
            if result['scanned'] >= limit:
                result['complete'] = False
                break
            result['scanned'] += 1
            if not entry.is_file(follow_symlinks=False):
                result['other'] += 1
            elif re.fullmatch(r'[a-f0-9]{64}', entry.name):
                state = referenced(database, entry.name, deadline)
                result['referenced' if state is True else 'unreferenced' if state is False else 'unconfirmed'] += 1
            elif re.fullmatch(r'[a-f0-9]{64}\.[a-f0-9]{12}', entry.name):
                result['temporary'] += 1
            else:
                result['other'] += 1
    if result['unconfirmed'] or result['other'] or result['temporary']:
        result['complete'] = False
    return result


def main():
    import argparse
    import os
    parser = argparse.ArgumentParser(description='Read-only evidence reference observation, never cleanup')
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=256)
    args = parser.parse_args()
    base = os.path.realpath(os.getcwd())
    root = os.path.realpath(args.root)
    prefix = base.rstrip(os.sep) + os.sep
    if root != base and not root.startswith(prefix):
        raise SystemExit('EVIDENCE_RECOVERY_CONTEXT_INVALID')
    try:
        print(json.dumps(recovery_status(Path(root) / 'marine.sqlite3', Path(root) / 'objects', args.limit)))
    except (OSError, ValueError):
        raise SystemExit('EVIDENCE_RECOVERY_UNAVAILABLE; originals unchanged') from None


if __name__ == '__main__':
    main()
