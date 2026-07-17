"""Restrictive filesystem boundaries for Gateway-owned SQLite databases."""
from __future__ import annotations

import os
import pathlib
import sqlite3
import stat
import time
from typing import Any


class SQLiteSecurityError(OSError):
    """Raised when a SQLite path cannot be made private safely."""


def path_is_within(path: pathlib.Path | str, root: pathlib.Path | str) -> bool:
    """Return whether *path* is located at or below *root*."""
    candidate = pathlib.Path(path).expanduser().absolute()
    boundary = pathlib.Path(root).expanduser().absolute()
    try:
        candidate.relative_to(boundary)
    except ValueError:
        return False
    return True


def ensure_private_directory(
    path: pathlib.Path | str,
    *,
    enforce_existing: bool = False,
) -> pathlib.Path:
    """Create a private directory and optionally tighten an existing one."""
    directory = pathlib.Path(path).expanduser()
    existed = directory.exists()
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SQLiteSecurityError(f"SQLite directory is not a real directory: {directory}")
        if enforce_existing or not existed:
            os.chmod(directory, 0o700, follow_symlinks=False)
            if stat.S_IMODE(directory.lstat().st_mode) != 0o700:
                raise SQLiteSecurityError(f"SQLite directory is not mode 0700: {directory}")
    except SQLiteSecurityError:
        raise
    except OSError as exc:
        raise SQLiteSecurityError(f"Cannot secure SQLite directory {directory}: {exc}") from exc
    return directory


def _secure_regular_file(path: pathlib.Path, *, create: bool) -> None:
    flags = os.O_RDWR
    if create:
        flags |= os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        if create:
            raise
        return
    except OSError as exc:
        raise SQLiteSecurityError(f"Cannot open SQLite artifact safely {path}: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SQLiteSecurityError(f"SQLite artifact is not a regular file: {path}")
        os.fchmod(fd, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise SQLiteSecurityError(f"SQLite artifact is not mode 0600: {path}")
    except SQLiteSecurityError:
        raise
    except OSError as exc:
        raise SQLiteSecurityError(f"Cannot secure SQLite artifact {path}: {exc}") from exc
    finally:
        os.close(fd)


def ensure_secure_sqlite_file(
    path: pathlib.Path | str,
    *,
    private_parent: bool = False,
) -> pathlib.Path:
    """Pre-create a non-symlink SQLite file with mode 0600."""
    database = pathlib.Path(path).expanduser()
    if private_parent:
        ensure_private_directory(database.parent, enforce_existing=True)
    else:
        try:
            database.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SQLiteSecurityError(
                f"Cannot create SQLite parent directory {database.parent}: {exc}"
            ) from exc
    _secure_regular_file(database, create=True)
    return database


def secure_sqlite_artifacts(path: pathlib.Path | str) -> None:
    """Tighten the database and any currently present journal artifacts."""
    database = pathlib.Path(path).expanduser()
    _secure_regular_file(database, create=True)
    for suffix in ("-wal", "-shm", "-journal"):
        _secure_regular_file(pathlib.Path(f"{database}{suffix}"), create=False)


def secure_sqlite_connect(
    path: pathlib.Path | str,
    *,
    private_parent: bool = False,
    **kwargs: Any,
) -> sqlite3.Connection:
    """Open a filesystem SQLite database only after enforcing private modes."""
    database = ensure_secure_sqlite_file(path, private_parent=private_parent)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(database), **kwargs)
        secure_sqlite_artifacts(database)
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise


def set_secure_sqlite_journal_mode(
    connection: sqlite3.Connection,
    path: pathlib.Path | str,
    mode: str = "WAL",
    *,
    timeout_seconds: float = 5.0,
) -> str:
    """Set journal mode with bounded retry for concurrent first-open races."""
    normalized = str(mode or "WAL").strip().upper()
    allowed = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}
    if normalized not in allowed:
        raise SQLiteSecurityError(f"unsupported SQLite journal mode: {mode}")
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    attempt = 0
    while True:
        attempt += 1
        try:
            row = connection.execute(f"PRAGMA journal_mode={normalized}").fetchone()
            active = str(row[0] if row else "").upper()
            if active != normalized:
                raise SQLiteSecurityError(
                    f"SQLite journal mode mismatch for {path}: requested {normalized}, active {active or 'unknown'}"
                )
            secure_sqlite_artifacts(path)
            return active
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if not any(marker in text for marker in ("locked", "busy")) or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.25, 0.005 * (2 ** min(attempt - 1, 6))))


__all__ = [
    "SQLiteSecurityError",
    "ensure_private_directory",
    "ensure_secure_sqlite_file",
    "path_is_within",
    "secure_sqlite_artifacts",
    "secure_sqlite_connect",
    "set_secure_sqlite_journal_mode",
]
