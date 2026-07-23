#!/usr/bin/env python3
"""Privacy-preserving request rate limiting backends.

The in-memory backend is useful for tests and single-process development.  The
SQLite token bucket provides atomic quota consumption across worker processes
and survives process restarts without persisting raw client identities.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

try:
    from .gateway_sqlite import path_is_within, secure_sqlite_artifacts, secure_sqlite_connect, set_secure_sqlite_journal_mode, sqlite_initialization_lock
except ImportError:  # pragma: no cover - legacy top-level import mode
    from gateway_sqlite import path_is_within, secure_sqlite_artifacts, secure_sqlite_connect, set_secure_sqlite_journal_mode, sqlite_initialization_lock


Json = dict[str, Any]


def _identity_hash(identity: str) -> str:
    payload = ("gateway-rate-limit-v1\0" + str(identity)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    backend: str
    remaining: float
    retry_after_seconds: float


class MemoryRateLimiter:
    """Process-local sliding-window limiter retained as a safe fallback."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.windows: dict[str, deque[float]] = {}
        self.rejected = 0

    def consume(self, identity: str, rpm: int, *, now: float | None = None) -> RateLimitDecision:
        now_value = time.monotonic() if now is None else float(now)
        key = _identity_hash(identity)
        cutoff = now_value - 60.0
        with self.lock:
            window = self.windows.setdefault(key, deque())
            while window and window[0] <= cutoff:
                window.popleft()
            if len(window) >= rpm:
                self.rejected += 1
                retry_after = max(0.0, 60.0 - (now_value - window[0])) if window else 60.0
                return RateLimitDecision(False, "memory", 0.0, retry_after)
            window.append(now_value)
            remaining = max(0.0, float(rpm - len(window)))
            if len(self.windows) > 10_000:
                stale = [item for item, values in self.windows.items() if not values or values[-1] <= cutoff][:1000]
                for item in stale:
                    self.windows.pop(item, None)
            return RateLimitDecision(True, "memory", remaining, 0.0)

    def snapshot(self) -> Json:
        with self.lock:
            return {
                "backend": "memory",
                "active_identities": len(self.windows),
                "rejections": self.rejected,
            }

    def clear(self) -> None:
        with self.lock:
            self.windows.clear()
            self.rejected = 0


class SQLiteRateLimiter:
    """Cross-process token bucket stored in a restrictive SQLite database."""

    def __init__(
        self,
        path: str | pathlib.Path,
        *,
        busy_timeout_ms: int = 1000,
        state_ttl_seconds: float = 3600.0,
    ) -> None:
        self.path = pathlib.Path(path).expanduser().absolute()
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self.state_ttl_seconds = max(60.0, float(state_ttl_seconds))
        self._init_lock = threading.Lock()
        self._initialized = False
        self._last_cleanup = 0.0

    def _connect(self) -> sqlite3.Connection:
        runtime = pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR") or ".gateway_runtime")
        conn = secure_sqlite_connect(
            self.path,
            private_parent=path_is_within(self.path, runtime),
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return conn

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
                    conn = None
                    try:
                        conn = self._connect()
                        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                        # This is an all-writer coordination database: every
                        # token update is already serialized by BEGIN IMMEDIATE.
                        # DELETE mode avoids a fragile cross-process WAL SHM map.
                        set_secure_sqlite_journal_mode(conn, self.path, "DELETE")
                        conn.execute("PRAGMA synchronous=NORMAL")
                        conn.execute(
                            """
                            CREATE TABLE IF NOT EXISTS rate_limit_buckets (
                                identity_hash TEXT PRIMARY KEY,
                                tokens REAL NOT NULL,
                                updated_at REAL NOT NULL,
                                last_seen REAL NOT NULL,
                                rejected_count INTEGER NOT NULL DEFAULT 0
                            )
                            """
                        )
                        conn.execute(
                            "CREATE INDEX IF NOT EXISTS idx_rate_limit_last_seen ON rate_limit_buckets(last_seen)"
                        )
                        conn.execute(
                            """
                            CREATE TABLE IF NOT EXISTS rate_limit_meta (
                                key TEXT PRIMARY KEY,
                                value INTEGER NOT NULL
                            )
                            """
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO rate_limit_meta(key, value) VALUES ('total_rejections', 0)"
                        )
                        break
                    except sqlite3.OperationalError as exc:
                        if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                            raise
                        time.sleep(min(0.25, 0.01 * (2 ** min(attempt - 1, 5))))
                    finally:
                        if conn is not None:
                            conn.close()
            self._initialized = True

    def consume(self, identity: str, rpm: int, *, now: float | None = None) -> RateLimitDecision:
        self._ensure_initialized()
        now_value = time.time() if now is None else float(now)
        capacity = float(max(1, int(rpm)))
        refill_per_second = capacity / 60.0
        key = _identity_hash(identity)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT tokens, updated_at, rejected_count FROM rate_limit_buckets WHERE identity_hash = ?",
                (key,),
            ).fetchone()
            rejected_count = 0
            if row is None:
                tokens = capacity
                updated_at = now_value
            else:
                stored_tokens, stored_updated_at, rejected_count = row
                elapsed = max(0.0, now_value - float(stored_updated_at))
                tokens = min(capacity, max(0.0, float(stored_tokens)) + elapsed * refill_per_second)
                updated_at = now_value

            if tokens >= 1.0:
                tokens -= 1.0
                allowed = True
                retry_after = 0.0
            else:
                allowed = False
                rejected_count = int(rejected_count) + 1
                retry_after = max(0.0, (1.0 - tokens) / refill_per_second)
                conn.execute(
                    "UPDATE rate_limit_meta SET value = value + 1 WHERE key = 'total_rejections'"
                )

            conn.execute(
                """
                INSERT INTO rate_limit_buckets(identity_hash, tokens, updated_at, last_seen, rejected_count)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(identity_hash) DO UPDATE SET
                    tokens=excluded.tokens,
                    updated_at=excluded.updated_at,
                    last_seen=excluded.last_seen,
                    rejected_count=excluded.rejected_count
                """,
                (key, tokens, updated_at, now_value, rejected_count),
            )
            if now_value - self._last_cleanup >= 60.0:
                conn.execute(
                    "DELETE FROM rate_limit_buckets WHERE last_seen < ?",
                    (now_value - self.state_ttl_seconds,),
                )
                self._last_cleanup = now_value
            conn.execute("COMMIT")
            return RateLimitDecision(allowed, "sqlite", max(0.0, tokens), retry_after)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def snapshot(self) -> Json:
        self._ensure_initialized()
        conn = self._connect()
        try:
            active_row = conn.execute("SELECT COUNT(*) FROM rate_limit_buckets").fetchone()
            rejected_row = conn.execute(
                "SELECT value FROM rate_limit_meta WHERE key = 'total_rejections'"
            ).fetchone()
            return {
                "backend": "sqlite",
                "active_identities": int(active_row[0] if active_row else 0),
                "rejections": int(rejected_row[0] if rejected_row else 0),
                "db_path": str(self.path),
            }
        finally:
            conn.close()


class RateLimitService:
    def __init__(self) -> None:
        self.memory = MemoryRateLimiter()
        self._sqlite_lock = threading.Lock()
        self._sqlite: dict[tuple[str, int, float], SQLiteRateLimiter] = {}
        self._last_backend = "uninitialized"
        self._last_configured_backend = ""
        self._last_error = ""

    def _sqlite_limiter(self, cfg: Json) -> SQLiteRateLimiter:
        path = str(
            cfg.get("rate_limit_db_path")
            or pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR", ".gateway_runtime")) / "rate_limits.sqlite3"
        )
        busy_timeout = max(1, int(cfg.get("rate_limit_busy_timeout_ms") or 1000))
        ttl = max(60.0, float(cfg.get("rate_limit_state_ttl_seconds") or 3600.0))
        key = (str(pathlib.Path(path).expanduser().absolute()), busy_timeout, ttl)
        with self._sqlite_lock:
            limiter = self._sqlite.get(key)
            if limiter is None:
                limiter = SQLiteRateLimiter(key[0], busy_timeout_ms=busy_timeout, state_ttl_seconds=ttl)
                self._sqlite[key] = limiter
            return limiter

    def consume(self, identity: str, rpm: int, cfg: Json) -> RateLimitDecision:
        backend = str(cfg.get("rate_limit_backend") or "memory").strip().lower()
        if backend not in {"memory", "sqlite"}:
            backend = "memory"
        if backend == "memory":
            self._last_configured_backend = "memory"
            self._last_backend = "memory"
            self._last_error = ""
            return self.memory.consume(identity, rpm)
        try:
            decision = self._sqlite_limiter(cfg).consume(identity, rpm)
            self._last_configured_backend = "sqlite"
            self._last_backend = "sqlite"
            self._last_error = ""
            return decision
        except Exception as exc:
            fallback = str(cfg.get("rate_limit_fallback_backend") or "memory").strip().lower()
            self._last_configured_backend = "sqlite"
            self._last_error = f"{exc.__class__.__name__}: {exc}"
            if fallback != "memory":
                raise
            decision = self.memory.consume(identity, rpm)
            self._last_backend = "memory_fallback"
            return RateLimitDecision(
                decision.allowed,
                "memory_fallback",
                decision.remaining,
                decision.retry_after_seconds,
            )

    def snapshot(self, cfg: Json) -> Json:
        backend = str(cfg.get("rate_limit_backend") or "memory").strip().lower()
        if backend == "sqlite":
            try:
                snapshot = self._sqlite_limiter(cfg).snapshot()
                snapshot["configured_backend"] = "sqlite"
                snapshot["last_backend"] = self._last_backend
                snapshot["degraded"] = self._last_backend == "memory_fallback"
                if self._last_error:
                    snapshot["last_error"] = self._last_error
                return snapshot
            except Exception as exc:
                memory = self.memory.snapshot()
                memory.update(
                    {
                        "backend": "memory_fallback",
                        "configured_backend": "sqlite",
                        "last_backend": "memory_fallback",
                        "degraded": True,
                        "last_error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                return memory
        snapshot = self.memory.snapshot()
        snapshot.update({"configured_backend": "memory", "last_backend": "memory", "degraded": False})
        return snapshot

    def describe(self, cfg: Json) -> Json:
        configured = str(cfg.get("rate_limit_backend") or "memory").strip().lower()
        if configured not in {"memory", "sqlite"}:
            configured = "memory"
        active = self._last_backend
        if active == "uninitialized" or configured != self._last_configured_backend:
            active = configured
        return {
            "configured_backend": configured,
            "backend": active,
            "degraded": active == "memory_fallback",
        }


RATE_LIMIT_SERVICE = RateLimitService()


__all__ = [
    "MemoryRateLimiter",
    "RATE_LIMIT_SERVICE",
    "RateLimitDecision",
    "RateLimitService",
    "SQLiteRateLimiter",
]
