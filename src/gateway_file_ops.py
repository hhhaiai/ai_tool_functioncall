#!/usr/bin/env python3
"""Crash-safe and concurrency-safe local file replacement helpers."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import stat
import shutil
import tempfile
import threading
from contextlib import ExitStack, contextmanager
from typing import Any, Callable, Iterator, TypeVar

_logger = logging.getLogger(__name__)

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore

_LOCK_STRIPES = tuple(threading.RLock() for _ in range(256))
T = TypeVar("T")


def _normalized_path(path: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(path).expanduser().resolve(strict=False)


def _path_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8", errors="surrogatepass")).hexdigest()


def _lock_directory() -> pathlib.Path:
    root = pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR") or ".gateway_runtime")
    return root.expanduser().resolve(strict=False) / "file_locks"


@contextmanager
def path_write_lock(path: pathlib.Path | str) -> Iterator[pathlib.Path]:
    """Serialize mutations of one resolved path within and across processes."""
    target = _normalized_path(path)
    digest = _path_digest(target)
    stripe = _LOCK_STRIPES[int(digest[:8], 16) % len(_LOCK_STRIPES)]
    lock_fd: int | None = None
    stripe.acquire()
    try:
        if fcntl is not None:
            try:
                lock_dir = _lock_directory()
                lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                lock_path = lock_dir / f"{digest}.lock"
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    os.fchmod(lock_fd, 0o600)
                except OSError:
                    pass
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as exc:
                if lock_fd is not None:
                    os.close(lock_fd)
                    lock_fd = None
                _logger.warning("Cross-process file lock unavailable for %s: %s", target, exc)
        yield target
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
        stripe.release()


def _fsync_directory(path: pathlib.Path) -> None:
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    try:
        directory_fd = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def fsync_directory(path: pathlib.Path | str) -> None:
    _fsync_directory(_normalized_path(path))


@contextmanager
def path_write_locks(paths: Any) -> Iterator[tuple[pathlib.Path, ...]]:
    """Acquire multiple path locks in deterministic order."""
    targets = tuple(sorted({_normalized_path(path) for path in paths}, key=str))
    with ExitStack() as stack:
        locked = tuple(stack.enter_context(path_write_lock(path)) for path in targets)
        yield locked


def _atomic_write_bytes_unlocked(
    target: pathlib.Path,
    data: bytes,
    *,
    new_file_mode: int = 0o600,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_stat: os.stat_result | None = None
    try:
        existing_stat = target.stat()
    except FileNotFoundError:
        pass

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.gateway-",
        suffix=".tmp",
        dir=str(target.parent),
    )
    temporary_path = pathlib.Path(temporary_name)
    try:
        mode = stat.S_IMODE(existing_stat.st_mode) if existing_stat is not None else int(new_file_mode)
        os.fchmod(fd, mode)
        if existing_stat is not None and hasattr(os, "fchown"):
            try:
                os.fchown(fd, existing_stat.st_uid, existing_stat.st_gid)
            except (PermissionError, OSError):
                pass
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            written = os.write(fd, view[offset:])
            if written <= 0:
                raise OSError("atomic file write made no progress")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary_path, target)
        _fsync_directory(target.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary_path.exists():
            try:
                temporary_path.unlink()
            except OSError:
                pass


def atomic_write_bytes(
    path: pathlib.Path | str,
    data: bytes,
    *,
    new_file_mode: int = 0o600,
    allowed_root: pathlib.Path | str | None = None,
) -> pathlib.Path:
    with path_write_lock(path) as target:
        if allowed_root is not None:
            root = _normalized_path(allowed_root)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"atomic write target escapes allowed root: {target}") from exc
        _atomic_write_bytes_unlocked(target, bytes(data), new_file_mode=new_file_mode)
        return target


def atomic_write_text(
    path: pathlib.Path | str,
    content: str,
    *,
    encoding: str = "utf-8",
    new_file_mode: int = 0o600,
    allowed_root: pathlib.Path | str | None = None,
) -> pathlib.Path:
    return atomic_write_bytes(
        path,
        str(content).encode(encoding),
        new_file_mode=new_file_mode,
        allowed_root=allowed_root,
    )


def atomic_create_bytes(
    path: pathlib.Path | str,
    data: bytes,
    *,
    new_file_mode: int = 0o600,
) -> bool:
    """Atomically create a file if absent; return whether this caller won."""
    with path_write_lock(path) as target:
        if target.exists():
            return False
        _atomic_write_bytes_unlocked(target, bytes(data), new_file_mode=new_file_mode)
        return True


def atomic_update_text(
    path: pathlib.Path | str,
    updater: Callable[[str], tuple[str, T]],
    *,
    encoding: str = "utf-8",
    errors: str = "replace",
    new_file_mode: int = 0o600,
) -> T:
    """Atomically read, transform, and replace a text file under one lock."""
    with path_write_lock(path) as target:
        original = target.read_text(encoding=encoding, errors=errors)
        updated, result = updater(original)
        _atomic_write_bytes_unlocked(
            target,
            str(updated).encode(encoding),
            new_file_mode=new_file_mode,
        )
        return result


def atomic_update_json(
    path: pathlib.Path | str,
    updater: Callable[[Any], tuple[Any, T]],
    *,
    default: Any,
) -> T:
    """Atomically read, transform, and replace one JSON document."""
    with path_write_lock(path) as target:
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            current = default
        updated, result = updater(current)
        payload = json.dumps(updated, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write_bytes_unlocked(target, payload)
        return result


def atomic_copy_file(
    source: pathlib.Path | str,
    destination: pathlib.Path | str,
    *,
    overwrite: bool = False,
) -> pathlib.Path:
    source_path = _normalized_path(source)
    with path_write_lock(destination) as target:
        if target.exists() and not overwrite:
            raise FileExistsError(str(target))
        data = source_path.read_bytes()
        mode = stat.S_IMODE(source_path.stat().st_mode)
        _atomic_write_bytes_unlocked(target, data, new_file_mode=mode)
        return target


def replace_bytes_locked(path: pathlib.Path | str, data: bytes, *, mode: int = 0o600) -> pathlib.Path:
    """Replace bytes while the caller already owns the path lock."""
    target = _normalized_path(path)
    _atomic_write_bytes_unlocked(target, bytes(data), new_file_mode=mode)
    try:
        target.chmod(mode)
    except OSError:
        pass
    return target


def remove_path_locked(path: pathlib.Path | str) -> None:
    """Remove a patch target while the caller already owns its path lock."""
    target = _normalized_path(path)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()
    _fsync_directory(target.parent)


def durable_create_directory(
    path: pathlib.Path | str,
    *,
    parents: bool = True,
    exist_ok: bool = True,
) -> pathlib.Path:
    with path_write_lock(path) as target:
        target.mkdir(parents=parents, exist_ok=exist_ok)
        _fsync_directory(target.parent)
        return target


def durable_delete_path(path: pathlib.Path | str, *, recursive: bool = False) -> pathlib.Path:
    with path_write_lock(path) as target:
        if not target.exists() and not target.is_symlink():
            raise FileNotFoundError(str(target))
        if target.is_dir() and not target.is_symlink():
            if not recursive:
                raise IsADirectoryError(str(target))
            shutil.rmtree(target)
        else:
            target.unlink()
        _fsync_directory(target.parent)
        return target


def durable_move_path(
    source: pathlib.Path | str,
    destination: pathlib.Path | str,
    *,
    overwrite: bool = False,
) -> tuple[pathlib.Path, pathlib.Path]:
    source_path = _normalized_path(source)
    destination_path = _normalized_path(destination)
    with path_write_locks((source_path, destination_path)):
        if not source_path.exists() and not source_path.is_symlink():
            raise FileNotFoundError(str(source_path))
        if destination_path.exists() and not overwrite:
            raise FileExistsError(str(destination_path))
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, destination_path)
        _fsync_directory(source_path.parent)
        if destination_path.parent != source_path.parent:
            _fsync_directory(destination_path.parent)
        return source_path, destination_path


__all__ = [
    "atomic_copy_file",
    "atomic_create_bytes",
    "atomic_update_text",
    "atomic_update_json",
    "atomic_write_bytes",
    "atomic_write_text",
    "durable_create_directory",
    "durable_delete_path",
    "durable_move_path",
    "fsync_directory",
    "path_write_lock",
    "path_write_locks",
    "remove_path_locked",
    "replace_bytes_locked",
]
