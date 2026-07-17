from __future__ import annotations

import multiprocessing
import os
import pathlib
import sqlite3
import io
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from src.gateway_rate_limit import RateLimitService, SQLiteRateLimiter, _identity_hash


def _consume_worker(path: str, rpm: int, attempts: int, now: float, queue) -> None:
    limiter = SQLiteRateLimiter(path, busy_timeout_ms=5000)
    allowed = 0
    rejected = 0
    try:
        for _ in range(attempts):
            decision = limiter.consume("shared-client", rpm, now=now)
            if decision.allowed:
                allowed += 1
            else:
                rejected += 1
        queue.put((allowed, rejected, ""))
    except Exception as exc:
        queue.put((allowed, rejected, f"{exc.__class__.__name__}: {exc}"))


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_sqlite_token_bucket_persists_across_limiter_restart(tmp_path):
    path = tmp_path / "limits.sqlite3"
    first = SQLiteRateLimiter(path)
    assert first.consume("client-a", 2, now=1000).allowed is True
    assert first.consume("client-a", 2, now=1000).allowed is True

    restarted = SQLiteRateLimiter(path)
    denied = restarted.consume("client-a", 2, now=1000)
    assert denied.allowed is False
    assert denied.retry_after_seconds == pytest.approx(30.0)


def test_sqlite_token_bucket_refills_and_expires_old_identities(tmp_path):
    path = tmp_path / "limits.sqlite3"
    limiter = SQLiteRateLimiter(path, state_ttl_seconds=60)
    assert limiter.consume("old-client", 2, now=1000).allowed
    assert limiter.consume("old-client", 2, now=1000).allowed
    assert not limiter.consume("old-client", 2, now=1000).allowed
    assert limiter.consume("old-client", 2, now=1030).allowed

    assert limiter.consume("new-client", 2, now=5000).allowed
    snapshot = limiter.snapshot()
    assert snapshot["active_identities"] == 1
    assert snapshot["rejections"] == 1


def test_sqlite_rate_limit_storage_contains_only_hashed_identity(tmp_path):
    path = tmp_path / "limits.sqlite3"
    identity = "private-client-name@example.test"
    limiter = SQLiteRateLimiter(path)
    assert limiter.consume(identity, 10, now=1000).allowed

    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT identity_hash FROM rate_limit_buckets").fetchall()
    finally:
        conn.close()
    assert rows == [(_identity_hash(identity),)]
    for artifact in path.parent.glob(path.name + "*"):
        assert identity.encode() not in artifact.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_sqlite_rate_limit_rejects_database_symlink(tmp_path):
    target = tmp_path / "target.db"
    sqlite3.connect(target).close()
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)
    limiter = SQLiteRateLimiter(alias)
    with pytest.raises(Exception, match="safely"):
        limiter.consume("client", 10, now=1000)


@pytest.mark.skipif(os.name == "nt", reason="spawned SQLite process contract is exercised on POSIX CI")
def test_sqlite_rate_limit_is_atomic_across_processes(tmp_path):
    path = str(tmp_path / "limits.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(target=_consume_worker, args=(path, 7, 5, 1000.0, queue))
        for _ in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    results = [queue.get(timeout=2) for _ in processes]
    assert all(not error for _allowed, _rejected, error in results), results
    assert sum(allowed for allowed, _rejected, _error in results) == 7
    assert sum(rejected for _allowed, rejected, _error in results) == 13


def test_rate_limit_service_falls_back_to_memory_when_sqlite_is_unavailable(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocked", encoding="utf-8")
    service = RateLimitService()
    cfg = {
        "rate_limit_backend": "sqlite",
        "rate_limit_db_path": str(blocker / "limits.sqlite3"),
        "rate_limit_fallback_backend": "memory",
    }

    decision = service.consume("client-a", 1, cfg)
    assert decision.allowed is True
    assert decision.backend == "memory_fallback"
    assert service.consume("client-a", 1, cfg).allowed is False
    description = service.describe(cfg)
    assert description["configured_backend"] == "sqlite"
    assert description["backend"] == "memory_fallback"
    assert description["degraded"] is True


def test_rate_limit_service_can_fail_closed_when_fallback_is_disabled(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("blocked", encoding="utf-8")
    service = RateLimitService()
    cfg = {
        "rate_limit_backend": "sqlite",
        "rate_limit_db_path": str(blocker / "limits.sqlite3"),
        "rate_limit_fallback_backend": "none",
    }

    with pytest.raises(OSError):
        service.consume("client-a", 10, cfg)


def test_http_429_preserves_backend_detail_and_retry_after_header():
    from src.gateway_errors import GatewayBusyError
    from src.gateway_http_handler import _handle_error

    class Handler:
        def __init__(self):
            self.status = None
            self.headers = {}
            self.wfile = io.BytesIO()

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.headers[key] = value

        def end_headers(self):
            return

    handler = Handler()
    _handle_error(
        handler,
        "/v1/chat/completions",
        GatewayBusyError(
            "request rate limit exceeded",
            detail={"backend": "sqlite", "retry_after_seconds": 2.01},
        ),
    )

    assert handler.status == 429
    assert handler.headers["Retry-After"] == "3"
    payload = json.loads(handler.wfile.getvalue())
    assert payload["error"]["detail"]["backend"] == "sqlite"


def test_http_enforcement_maps_fail_closed_backend_error_to_503(monkeypatch):
    from src.gateway_errors import GatewayUnavailableError
    from src.gateway_http_handler import _enforce_request_rate_limit

    class Handler:
        client_address = ("127.0.0.1", 12345)

    monkeypatch.setattr(
        "src.gateway_config._gateway_config",
        lambda: {
            "rate_limit_enabled": True,
            "rate_limit_rpm": 10,
            "rate_limit_backend": "sqlite",
            "rate_limit_fallback_backend": "none",
        },
    )
    monkeypatch.setattr(
        "src.gateway_rate_limit.RATE_LIMIT_SERVICE.consume",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("database unavailable")),
    )

    with pytest.raises(GatewayUnavailableError) as exc_info:
        _enforce_request_rate_limit(Handler(), "client-a")
    assert exc_info.value.status == 503
    assert exc_info.value.detail["backend"] == "sqlite"


@pytest.mark.skipif(os.name == "nt", reason="multi-process live Gateway contract is exercised on POSIX CI")
def test_two_live_gateway_processes_share_one_rate_limit(tmp_path):
    root = pathlib.Path(__file__).resolve().parents[1]
    shared_db = tmp_path / "shared-rate-limits.sqlite3"
    processes = []
    ports = [_free_port(), _free_port()]
    try:
        for index, port in enumerate(ports):
            runtime = tmp_path / f"runtime-{index}"
            env = {
                **os.environ,
                "GATEWAY_CONFIG_PATH": str(tmp_path / f"config-{index}.json"),
                "GATEWAY_RUNTIME_DIR": str(runtime),
                "GATEWAY_SQLITE_LOG_PATH": str(tmp_path / f"log-{index}.sqlite3"),
                "GATEWAY_PERSISTENCE_DB_PATH": str(tmp_path / f"persistence-{index}.sqlite3"),
                "GATEWAY_RATE_LIMIT_BACKEND": "sqlite",
                "GATEWAY_RATE_LIMIT_DB_PATH": str(shared_db),
                "GATEWAY_RATE_LIMIT_RPM": "2",
                "GATEWAY_DOWNSTREAM_KEY": "rate-limit-test-key",
                "GATEWAY_ADMIN_PASSWORD": "rate-limit-test-admin",
                "UPSTREAM_BASE_URL": "http://127.0.0.1:9",
                "UPSTREAM_API_KEY": "test-only",
                "UPSTREAM_MODEL": "test-model",
            }
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "src.gateway_app", "--host", "127.0.0.1", "--port", str(port)],
                    cwd=root,
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )

        for port in ports:
            deadline = time.time() + 10
            while True:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except Exception:
                    if time.time() >= deadline:
                        raise
                    time.sleep(0.05)

        statuses = []
        for port in (ports[0], ports[1], ports[0]):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/tools/call",
                data=json.dumps({"tool": "calculator", "arguments": {"expression": "20+22"}}).encode(),
                headers={
                    "Authorization": "Bearer rate-limit-test-key",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    statuses.append(response.status)
            except urllib.error.HTTPError as exc:
                statuses.append(exc.code)
                if exc.code == 429:
                    assert int(exc.headers["Retry-After"]) >= 1
                    payload = json.loads(exc.read())
                    assert payload["error"]["detail"]["backend"] == "sqlite"

        assert statuses == [200, 200, 429]
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
