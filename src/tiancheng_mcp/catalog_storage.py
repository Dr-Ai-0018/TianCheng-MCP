"""Shared Catalog maintenance exclusion and reversible database-group moves."""
from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import stat
import sqlite3
import threading
import time
import uuid
import weakref


class CatalogStorageError(ValueError):
    pass


class _CatalogLock:
    def __init__(self, database: Path) -> None:
        self.path = Path(str(database) + '.lock')
        self.mutex = threading.RLock()
        self.depth = 0
        self.descriptor: int | None = None

    def __enter__(self):
        self.mutex.acquire()
        if self.depth:
            self.depth += 1
            return self
        descriptor = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(self.path):
                info = self.path.lstat()
                if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    raise CatalogStorageError('Catalog maintenance lock must be a regular file')
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CatalogStorageError('Catalog maintenance lock must be a regular file')
            if not os.fstat(descriptor).st_size:
                os.write(descriptor, b'0')
            deadline = time.monotonic() + 5
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Catalog is busy with another refresh or maintenance operation')
                    time.sleep(0.02)
            self.descriptor = descriptor
            self.depth = 1
            return self
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            self.mutex.release()
            raise

    def __exit__(self, *_):
        try:
            self.depth -= 1
            if not self.depth:
                descriptor = self.descriptor
                self.descriptor = None
                try:
                    if os.name == 'nt':
                        import msvcrt
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            self.mutex.release()


_registry: weakref.WeakValueDictionary[str, _CatalogLock] = weakref.WeakValueDictionary()
_registry_lock = threading.Lock()


def catalog_lock(database: Path) -> _CatalogLock:
    key = os.path.normcase(str(database.resolve()))
    with _registry_lock:
        lock = _registry.get(key)
        if lock is None:
            lock = _CatalogLock(database)
            _registry[key] = lock
        return lock


def database_group(database: Path) -> list[Path]:
    return [Path(str(database) + suffix) for suffix in ('', '-wal', '-shm')]


def ensure_quiescent(database: Path) -> None:
    for path in database_group(database):
        if os.path.lexists(path):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise CatalogStorageError('Catalog database group must contain regular files')
    if not database.exists():
        return
    connection = sqlite3.connect(database.resolve().as_uri() + '?mode=rw', uri=True, timeout=0)
    try:
        checkpoint = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint[0]:
            raise CatalogStorageError('Catalog has an active SQLite reader or writer; close it before rebuilding')
    except sqlite3.DatabaseError as exc:
        if not any(message in str(exc).casefold() for message in
                   ('file is not a database', 'database disk image is malformed')):
            raise CatalogStorageError('Catalog is busy or unavailable; rebuild did not replace it') from exc
        # A corrupt database can still be moved intact for recovery.
    finally:
        connection.close()


def restore_group(moved: list[tuple[Path, Path]]) -> None:
    errors = []
    for current, backup in reversed(moved):
        try:
            if os.path.lexists(current):
                raise CatalogStorageError('Catalog rollback destination is occupied')
            backup.replace(current)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise CatalogStorageError('Catalog rollback incomplete; preserve database backup files for recovery') from errors[0]


def move_database_group(database: Path, label: str) -> list[tuple[Path, Path]]:
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
    plan = []
    for current in database_group(database):
        if not os.path.lexists(current):
            continue
        info = current.lstat()
        if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise CatalogStorageError('Catalog database group must contain regular files')
        backup = current.with_name(f'{current.name}.{label}-{stamp}')
        if os.path.lexists(backup):
            raise CatalogStorageError('Catalog backup name already exists')
        plan.append((current, backup))
    moved = []
    try:
        for current, backup in plan:
            current.replace(backup)
            moved.append((current, backup))
    except BaseException:
        restore_group(moved)
        raise
    return moved
