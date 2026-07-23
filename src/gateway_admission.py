"""Shared request-admission leases for multi-process Gateway deployments."""
from __future__ import annotations

import os
import importlib
import pathlib
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

_gateway_sqlite = importlib.import_module(
    f"{__package__}.gateway_sqlite" if __package__ else "gateway_sqlite"
)

path_is_within = _gateway_sqlite.path_is_within
secure_sqlite_artifacts = _gateway_sqlite.secure_sqlite_artifacts
secure_sqlite_connect = _gateway_sqlite.secure_sqlite_connect
set_secure_sqlite_journal_mode = _gateway_sqlite.set_secure_sqlite_journal_mode
sqlite_initialization_lock = _gateway_sqlite.sqlite_initialization_lock

Json = dict[str, Any]
_PROCESS_INSTANCE = uuid.uuid4().hex


@dataclass(frozen=True)
class AdmissionAttempt:
    acquired: bool
    backend: str
    active: int
    effective_limit: int
    retry_after_seconds: float
    lease_id: str = ""


class AdmissionLease:
    backend = "none"
    lease_id = ""

    def release(self) -> None:
        return


class SQLiteAdmissionBackend:
    """Atomic cross-process admission state with expiring leases."""

    def __init__(self, path: str | pathlib.Path, *, busy_timeout_ms: int = 1000) -> None:
        self.path = pathlib.Path(path).expanduser().absolute()
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        runtime = pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR") or ".gateway_runtime")
        connection = secure_sqlite_connect(
            self.path,
            private_parent=path_is_within(self.path, runtime),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return connection

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with sqlite_initialization_lock(self.path):
                deadline = time.monotonic() + max(2.0, self.busy_timeout_ms / 1000.0 + 1.0)
                attempt = 0
                while True:
                    attempt += 1
                    connection: sqlite3.Connection | None = None
                    try:
                        connection = self._connect()
                        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                        # Admission is also an all-writer coordination database
                        # whose lease transactions use BEGIN IMMEDIATE. Avoid a
                        # cross-process WAL SHM mapping for this narrow state.
                        set_secure_sqlite_journal_mode(connection, self.path, "DELETE")
                        connection.execute("PRAGMA synchronous=NORMAL")
                        connection.executescript(
                            """
                            CREATE TABLE IF NOT EXISTS admission_leases (
                                lease_id TEXT PRIMARY KEY,
                                owner_pid INTEGER NOT NULL,
                                owner_instance TEXT NOT NULL,
                                limit_value INTEGER NOT NULL,
                                acquired_at REAL NOT NULL,
                                heartbeat_at REAL NOT NULL,
                                expires_at REAL NOT NULL
                            );
                            CREATE INDEX IF NOT EXISTS idx_admission_expires
                                ON admission_leases(expires_at);
                            CREATE TABLE IF NOT EXISTS admission_meta (
                                key TEXT PRIMARY KEY,
                                value INTEGER NOT NULL
                            );
                            INSERT OR IGNORE INTO admission_meta(key, value) VALUES ('rejections', 0);
                            INSERT OR IGNORE INTO admission_meta(key, value) VALUES ('expired_reaped', 0);
                            """
                        )
                        self._initialized = True
                        return
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(0.25, 0.01 * (2 ** min(attempt - 1, 5))))
                    finally:
                        if connection is not None:
                            connection.close()

    def try_acquire(
        self,
        limit: int,
        *,
        lease_ttl_seconds: float,
        now: float | None = None,
    ) -> AdmissionAttempt:
        self._ensure_initialized()
        now_value = time.time() if now is None else float(now)
        requested_limit = max(1, int(limit))
        ttl = max(0.2, float(lease_ttl_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            connection.execute("DELETE FROM admission_leases WHERE expires_at <= ?", (now_value,))
            reaped = connection.total_changes - before
            if reaped:
                connection.execute(
                    "UPDATE admission_meta SET value=value+? WHERE key='expired_reaped'",
                    (reaped,),
                )
            row = connection.execute(
                "SELECT COUNT(*), MIN(limit_value), MIN(expires_at) FROM admission_leases"
            ).fetchone()
            active = int(row[0] if row else 0)
            active_limit = int(row[1]) if row and row[1] is not None else requested_limit
            effective_limit = min(requested_limit, active_limit)
            if active >= effective_limit:
                earliest_expiry = float(row[2]) if row and row[2] is not None else now_value + ttl
                connection.execute("COMMIT")
                return AdmissionAttempt(
                    False,
                    "sqlite",
                    active,
                    effective_limit,
                    max(0.01, earliest_expiry - now_value),
                )
            lease_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO admission_leases(
                    lease_id, owner_pid, owner_instance, limit_value,
                    acquired_at, heartbeat_at, expires_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    lease_id,
                    os.getpid(),
                    _PROCESS_INSTANCE,
                    requested_limit,
                    now_value,
                    now_value,
                    now_value + ttl,
                ),
            )
            connection.execute("COMMIT")
            return AdmissionAttempt(True, "sqlite", active + 1, effective_limit, 0.0, lease_id)
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def heartbeat(self, lease_id: str, *, lease_ttl_seconds: float) -> bool:
        self._ensure_initialized()
        now_value = time.time()
        ttl = max(0.2, float(lease_ttl_seconds))
        connection = self._connect()
        try:
            result = connection.execute(
                """
                UPDATE admission_leases
                SET heartbeat_at=?, expires_at=?
                WHERE lease_id=? AND owner_instance=?
                """,
                (now_value, now_value + ttl, lease_id, _PROCESS_INSTANCE),
            )
            return int(result.rowcount) == 1
        finally:
            connection.close()

    def release(self, lease_id: str) -> None:
        self._ensure_initialized()
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM admission_leases WHERE lease_id=? AND owner_instance=?",
                (lease_id, _PROCESS_INSTANCE),
            )
        finally:
            connection.close()

    def record_rejection(self) -> None:
        self._ensure_initialized()
        connection = self._connect()
        try:
            connection.execute("UPDATE admission_meta SET value=value+1 WHERE key='rejections'")
        finally:
            connection.close()

    def snapshot(self, *, now: float | None = None) -> Json:
        self._ensure_initialized()
        now_value = time.time() if now is None else float(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.total_changes
            connection.execute("DELETE FROM admission_leases WHERE expires_at <= ?", (now_value,))
            reaped = connection.total_changes - before
            if reaped:
                connection.execute(
                    "UPDATE admission_meta SET value=value+? WHERE key='expired_reaped'",
                    (reaped,),
                )
            row = connection.execute(
                "SELECT COUNT(*), MIN(limit_value), MIN(expires_at) FROM admission_leases"
            ).fetchone()
            meta = {
                str(key): int(value)
                for key, value in connection.execute("SELECT key, value FROM admission_meta").fetchall()
            }
            connection.execute("COMMIT")
            return {
                "backend": "sqlite",
                "active": int(row[0] if row else 0),
                "effective_limit": int(row[1]) if row and row[1] is not None else 0,
                "next_expiry_seconds": max(0.0, float(row[2]) - now_value)
                if row and row[2] is not None
                else 0.0,
                "rejections": int(meta.get("rejections", 0)),
                "expired_reaped": int(meta.get("expired_reaped", 0)),
                "db_path": str(self.path),
            }
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()


class SQLiteAdmissionLease(AdmissionLease):
    backend = "sqlite"

    def __init__(
        self,
        backend: SQLiteAdmissionBackend,
        lease_id: str,
        *,
        lease_ttl_seconds: float,
        heartbeat_seconds: float,
    ) -> None:
        self._backend = backend
        self.lease_id = lease_id
        self._ttl = max(0.2, float(lease_ttl_seconds))
        self._heartbeat_seconds = max(0.05, min(float(heartbeat_seconds), self._ttl / 2.0))
        self._stop = threading.Event()
        self._released = False
        self._lock = threading.Lock()
        self.heartbeat_failures = 0
        self.lost = False
        self.release_error = ""
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"gateway-admission-{lease_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._heartbeat_seconds):
            try:
                if not self._backend.heartbeat(self.lease_id, lease_ttl_seconds=self._ttl):
                    self.lost = True
                    return
            except Exception:
                self.heartbeat_failures += 1

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._heartbeat_seconds * 2.0))
        try:
            self._backend.release(self.lease_id)
        except Exception as exc:
            # The lease will expire and be reaped. Do not corrupt an otherwise
            # completed HTTP response because the cleanup backend disappeared.
            self.release_error = f"{exc.__class__.__name__}: {exc}"


class MemoryAdmissionLease(AdmissionLease):
    backend = "memory"

    def __init__(self, backend: "MemoryAdmissionBackend", lease_id: str) -> None:
        self._backend = backend
        self.lease_id = lease_id
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._backend.release(self.lease_id)


class MemoryAdmissionBackend:
    """Process-local fallback with truthful degraded semantics."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._leases: dict[str, int] = {}
        self.rejections = 0

    def acquire(self, limit: int, *, timeout_seconds: float) -> MemoryAdmissionLease | None:
        requested = max(1, int(limit))
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._condition:
            while True:
                effective = min([requested, *self._leases.values()]) if self._leases else requested
                if len(self._leases) < effective:
                    lease_id = uuid.uuid4().hex
                    self._leases[lease_id] = requested
                    return MemoryAdmissionLease(self, lease_id)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.rejections += 1
                    return None
                self._condition.wait(timeout=remaining)

    def release(self, lease_id: str) -> None:
        with self._condition:
            self._leases.pop(lease_id, None)
            self._condition.notify_all()

    def snapshot(self) -> Json:
        with self._condition:
            effective = min(self._leases.values()) if self._leases else 0
            return {
                "backend": "memory",
                "active": len(self._leases),
                "effective_limit": effective,
                "rejections": self.rejections,
                "expired_reaped": 0,
            }

    def clear(self) -> None:
        with self._condition:
            self._leases.clear()
            self.rejections = 0
            self._condition.notify_all()


class AdmissionService:
    def __init__(self) -> None:
        self.memory = MemoryAdmissionBackend()
        self._sqlite_lock = threading.Lock()
        self._sqlite: dict[tuple[str, int], SQLiteAdmissionBackend] = {}
        self._last_backend = "uninitialized"
        self._last_configured_backend = ""
        self._last_error = ""

    def _sqlite_backend(self, cfg: Json) -> SQLiteAdmissionBackend:
        path = str(
            cfg.get("concurrency_db_path")
            or pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR", ".gateway_runtime")) / "admission.sqlite3"
        )
        busy_timeout = max(1, int(cfg.get("concurrency_busy_timeout_ms") or 1000))
        key = (str(pathlib.Path(path).expanduser().absolute()), busy_timeout)
        with self._sqlite_lock:
            backend = self._sqlite.get(key)
            if backend is None:
                backend = SQLiteAdmissionBackend(key[0], busy_timeout_ms=busy_timeout)
                self._sqlite[key] = backend
            return backend

    def acquire(self, cfg: Json) -> AdmissionLease:
        try:
            limit = int(cfg.get("max_concurrent_requests") or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return AdmissionLease()
        timeout = max(0.0, float(cfg.get("concurrency_queue_timeout_seconds") or 0.0))
        configured = str(cfg.get("concurrency_backend") or "sqlite").strip().lower()
        if configured not in {"memory", "sqlite"}:
            configured = "sqlite"
        self._last_configured_backend = configured
        if configured == "memory":
            lease = self.memory.acquire(limit, timeout_seconds=timeout)
            self._last_backend = "memory"
            self._last_error = ""
            if lease is None:
                from .gateway_errors import GatewayBusyError
                raise GatewayBusyError(
                    f"gateway concurrency limit reached ({limit})",
                    detail={"backend": "memory", "retry_after_seconds": max(0.01, timeout)},
                )
            return lease

        ttl = max(0.2, float(cfg.get("concurrency_lease_ttl_seconds") or 120.0))
        heartbeat = max(0.05, float(cfg.get("concurrency_heartbeat_seconds") or min(30.0, ttl / 3.0)))
        deadline = time.monotonic() + timeout
        backend = self._sqlite_backend(cfg)
        try:
            while True:
                attempt = backend.try_acquire(limit, lease_ttl_seconds=ttl)
                if attempt.acquired:
                    self._last_backend = "sqlite"
                    self._last_error = ""
                    return SQLiteAdmissionLease(
                        backend,
                        attempt.lease_id,
                        lease_ttl_seconds=ttl,
                        heartbeat_seconds=heartbeat,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    backend.record_rejection()
                    self._last_backend = "sqlite"
                    from .gateway_errors import GatewayBusyError
                    raise GatewayBusyError(
                        f"gateway concurrency limit reached ({attempt.effective_limit})",
                        detail={
                            "backend": "sqlite",
                            "active": attempt.active,
                            "limit": attempt.effective_limit,
                            "retry_after_seconds": attempt.retry_after_seconds,
                        },
                    )
                time.sleep(min(0.05, remaining, attempt.retry_after_seconds))
        except Exception as exc:
            from .gateway_errors import GatewayBusyError, GatewayUnavailableError
            if isinstance(exc, GatewayBusyError):
                raise
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            fallback = str(cfg.get("concurrency_fallback_backend") or "none").strip().lower()
            if fallback == "memory":
                lease = self.memory.acquire(limit, timeout_seconds=timeout)
                self._last_backend = "memory_fallback"
                if lease is None:
                    raise GatewayBusyError(
                        f"gateway concurrency limit reached ({limit})",
                        detail={"backend": "memory_fallback", "retry_after_seconds": max(0.01, timeout)},
                    )
                lease.backend = "memory_fallback"
                return lease
            self._last_backend = "unavailable"
            raise GatewayUnavailableError(
                "request admission backend unavailable",
                detail={"backend": "sqlite"},
            ) from exc

    def snapshot(self, cfg: Json) -> Json:
        configured = str(cfg.get("concurrency_backend") or "sqlite").strip().lower()
        if configured == "sqlite":
            try:
                snapshot = self._sqlite_backend(cfg).snapshot()
                snapshot.update({
                    "configured_backend": "sqlite",
                    "configured_limit": max(0, int(cfg.get("max_concurrent_requests") or 0)),
                    "last_backend": self._last_backend,
                    "degraded": self._last_backend == "memory_fallback",
                })
                if self._last_error:
                    snapshot["last_error"] = self._last_error
                if not snapshot.get("effective_limit"):
                    snapshot["effective_limit"] = snapshot["configured_limit"]
                return snapshot
            except Exception as exc:
                snapshot = self.memory.snapshot()
                snapshot.update({
                    "backend": "memory_fallback" if str(cfg.get("concurrency_fallback_backend") or "none") == "memory" else "unavailable",
                    "configured_backend": "sqlite",
                    "configured_limit": max(0, int(cfg.get("max_concurrent_requests") or 0)),
                    "last_backend": self._last_backend,
                    "degraded": True,
                    "last_error": f"{exc.__class__.__name__}: {exc}",
                })
                return snapshot
        snapshot = self.memory.snapshot()
        snapshot.update({
            "configured_backend": "memory",
            "configured_limit": max(0, int(cfg.get("max_concurrent_requests") or 0)),
            "last_backend": "memory",
            "degraded": False,
        })
        if not snapshot.get("effective_limit"):
            snapshot["effective_limit"] = snapshot["configured_limit"]
        return snapshot

    def describe(self, cfg: Json) -> Json:
        configured = str(cfg.get("concurrency_backend") or "sqlite").strip().lower()
        if configured not in {"memory", "sqlite"}:
            configured = "sqlite"
        active = self._last_backend
        if active == "uninitialized" or configured != self._last_configured_backend:
            active = configured
        return {
            "configured_backend": configured,
            "backend": active,
            "degraded": active == "memory_fallback",
            "scope": "shared" if active == "sqlite" else "process",
        }

    def clear(self) -> None:
        self.memory.clear()
        with self._sqlite_lock:
            self._sqlite.clear()
        self._last_backend = "uninitialized"
        self._last_configured_backend = ""
        self._last_error = ""


ADMISSION_SERVICE = AdmissionService()


__all__ = [
    "ADMISSION_SERVICE",
    "AdmissionAttempt",
    "AdmissionLease",
    "AdmissionService",
    "MemoryAdmissionBackend",
    "SQLiteAdmissionBackend",
    "SQLiteAdmissionLease",
]
