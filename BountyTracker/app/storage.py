"""Local single-instance and SQLite backup helpers."""

from contextlib import closing, contextmanager
import errno
import os
from pathlib import Path
import sqlite3
import tempfile
import time


_LOCK_CONTENTION = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
_SQLITE_SIDECARS = ('-wal', '-shm', '-journal')


@contextmanager
def acquire_instance(path):
    """Yield whether this process acquired an exclusive lock for the context."""
    with open(path, 'a+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in _LOCK_CONTENTION:
                raise
            yield False
        else:
            # Closing the file releases its lock. Keep the path so all
            # contenders always lock the same file.
            yield True


def _sidecars(path):
    return (path.with_name(path.name + suffix) for suffix in _SQLITE_SIDECARS)


def _check_destination(path):
    if any(os.path.lexists(sidecar) for sidecar in _sidecars(path)):
        raise OSError('The destination has SQLite journal files. Choose another backup file.')


def backup_database(source_connection, destination):
    """Copy a committed SQLite snapshot and atomically replace the destination file."""
    target = Path(destination).absolute()
    _check_destination(target)
    if source_connection.in_transaction:
        raise sqlite3.OperationalError('Commit or roll back the source transaction before backing up.')

    deadline = time.monotonic() + 5

    def check_progress(status, _remaining, _total):
        if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) and time.monotonic() >= deadline:
            raise sqlite3.OperationalError('The source database is busy. Try the backup again.')

    with tempfile.NamedTemporaryFile(prefix='.tracker-backup-', suffix='.db',
                                     dir=target.parent, delete=False) as temp:
        temporary = Path(temp.name)
    try:
        with closing(sqlite3.connect(temporary, timeout=5)) as copy:
            source_connection.backup(copy, pages=128, progress=check_progress, sleep=.1)
            if copy.execute('PRAGMA journal_mode=DELETE').fetchone() != ('delete',):
                raise sqlite3.DatabaseError('Could not prepare a standalone backup file.')
            if copy.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise sqlite3.DatabaseError('The backup did not pass the SQLite integrity check.')
        _check_destination(target)
        os.replace(temporary, target)
    finally:
        for path in (temporary, *_sidecars(temporary)):
            path.unlink(missing_ok=True)
