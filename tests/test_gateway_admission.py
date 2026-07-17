from __future__ import annotations

import json
import multiprocessing
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import base64
import threading
from http.server import ThreadingHTTPServer

import pytest

from src.gateway_admission import AdmissionService, SQLiteAdmissionBackend
from src.gateway_errors import GatewayBusyError, GatewayUnavailableError
from src import gateway_config
from src.gateway_http_handler import GatewayHandler


def _acquire_worker(path: str, start_event, release_event, queue) -> None:
    backend = SQLiteAdmissionBackend(path, busy_timeout_ms=5000)
    start_event.wait(10)
    try:
        attempt = backend.try_acquire(3, lease_ttl_seconds=30)
        queue.put((attempt.acquired, attempt.lease_id, ""))
        if attempt.acquired:
            release_event.wait(10)
            backend.release(attempt.lease_id)
    except Exception as exc:
        queue.put((False, "", f"{exc.__class__.__name__}: {exc}"))


def _crash_worker(path: str, marker: str) -> None:
    backend = SQLiteAdmissionBackend(path, busy_timeout_ms=5000)
    attempt = backend.try_acquire(1, lease_ttl_seconds=0.4)
    if not attempt.acquired:
        os._exit(2)
    pathlib.Path(marker).write_text(attempt.lease_id, encoding="utf-8")
    os._exit(0)


def _free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_sqlite_admission_enforces_limit_and_release(tmp_path: pathlib.Path) -> None:
    backend = SQLiteAdmissionBackend(tmp_path / "admission.db")
    first = backend.try_acquire(2, lease_ttl_seconds=30, now=1000)
    second = backend.try_acquire(2, lease_ttl_seconds=30, now=1000)
    denied = backend.try_acquire(2, lease_ttl_seconds=30, now=1000)
    assert first.acquired and second.acquired
    assert denied.acquired is False
    assert denied.active == 2
    assert denied.effective_limit == 2

    backend.release(first.lease_id)
    replacement = backend.try_acquire(2, lease_ttl_seconds=30, now=1000)
    assert replacement.acquired is True
    backend.release(second.lease_id)
    backend.release(replacement.lease_id)
    assert backend.snapshot(now=1000)["active"] == 0


def test_active_leases_apply_the_strictest_configured_limit(tmp_path: pathlib.Path) -> None:
    backend = SQLiteAdmissionBackend(tmp_path / "admission.db")
    broad = backend.try_acquire(3, lease_ttl_seconds=30, now=1000)
    assert broad.acquired
    strict = backend.try_acquire(1, lease_ttl_seconds=30, now=1000)
    assert strict.acquired is False
    assert strict.effective_limit == 1
    backend.release(broad.lease_id)


def test_expired_crash_lease_is_reaped_and_counted(tmp_path: pathlib.Path) -> None:
    backend = SQLiteAdmissionBackend(tmp_path / "admission.db")
    crashed = backend.try_acquire(1, lease_ttl_seconds=0.2, now=1000)
    assert crashed.acquired
    denied = backend.try_acquire(1, lease_ttl_seconds=0.2, now=1000.1)
    assert denied.acquired is False
    recovered = backend.try_acquire(1, lease_ttl_seconds=0.2, now=1000.3)
    assert recovered.acquired is True
    snapshot = backend.snapshot(now=1000.3)
    assert snapshot["expired_reaped"] == 1
    backend.release(recovered.lease_id)


def test_service_heartbeat_keeps_long_request_lease_alive(tmp_path: pathlib.Path) -> None:
    service = AdmissionService()
    cfg = {
        "max_concurrent_requests": 1,
        "concurrency_backend": "sqlite",
        "concurrency_db_path": str(tmp_path / "admission.db"),
        "concurrency_fallback_backend": "none",
        "concurrency_queue_timeout_seconds": 0,
        "concurrency_lease_ttl_seconds": 0.3,
        "concurrency_heartbeat_seconds": 0.05,
    }
    lease = service.acquire(cfg)
    try:
        time.sleep(0.7)
        with pytest.raises(GatewayBusyError):
            service.acquire(cfg)
        assert lease.heartbeat_failures == 0
        assert lease.lost is False
    finally:
        lease.release()
    replacement = service.acquire(cfg)
    replacement.release()


def test_admission_storage_is_private_and_contains_no_request_identity(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(runtime))
    database = runtime / "admission.db"
    backend = SQLiteAdmissionBackend(database)
    attempt = backend.try_acquire(2, lease_ttl_seconds=30)
    assert attempt.acquired
    assert runtime.stat().st_mode & 0o777 == 0o700
    assert database.stat().st_mode & 0o777 == 0o600
    raw = b"".join(item.read_bytes() for item in runtime.glob("admission.db*"))
    assert b"client" not in raw
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT lease_id, owner_pid, owner_instance, limit_value FROM admission_leases"
        ).fetchone()
    assert row[0] == attempt.lease_id
    assert row[1] > 0
    assert len(row[2]) == 32
    assert row[3] == 2
    backend.release(attempt.lease_id)


def test_admission_database_symlink_is_rejected(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "target.db"
    sqlite3.connect(target).close()
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)
    backend = SQLiteAdmissionBackend(alias)
    with pytest.raises(Exception, match="safely"):
        backend.try_acquire(1, lease_ttl_seconds=30)


def test_idle_service_snapshot_reports_configured_limit(tmp_path: pathlib.Path) -> None:
    service = AdmissionService()
    cfg = {
        "max_concurrent_requests": 7,
        "concurrency_backend": "sqlite",
        "concurrency_db_path": str(tmp_path / "admission.db"),
    }
    snapshot = service.snapshot(cfg)
    assert snapshot["active"] == 0
    assert snapshot["configured_limit"] == 7
    assert snapshot["effective_limit"] == 7


def test_release_backend_failure_is_deferred_to_ttl_without_raising(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AdmissionService()
    cfg = {
        "max_concurrent_requests": 1,
        "concurrency_backend": "sqlite",
        "concurrency_db_path": str(tmp_path / "admission.db"),
        "concurrency_lease_ttl_seconds": 30,
        "concurrency_heartbeat_seconds": 10,
    }
    lease = service.acquire(cfg)
    monkeypatch.setattr(lease._backend, "release", lambda _lease_id: (_ for _ in ()).throw(OSError("release marker")))
    lease.release()
    assert "release marker" in lease.release_error


@pytest.mark.skipif(os.name == "nt", reason="spawned SQLite process contract is exercised on POSIX")
def test_sqlite_admission_is_atomic_across_processes(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "admission.db")
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    release_event = context.Event()
    queue = context.Queue()
    processes = [
        context.Process(target=_acquire_worker, args=(path, start_event, release_event, queue))
        for _ in range(8)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [queue.get(timeout=20) for _ in processes]
    assert all(not error for _acquired, _lease_id, error in results), results
    assert sum(1 for acquired, _lease_id, _error in results if acquired) == 3
    release_event.set()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert SQLiteAdmissionBackend(path).snapshot()["active"] == 0


@pytest.mark.skipif(os.name == "nt", reason="crash lease recovery is exercised on POSIX")
def test_process_crash_lease_expires_without_release(tmp_path: pathlib.Path) -> None:
    path = str(tmp_path / "admission.db")
    marker = tmp_path / "acquired.txt"
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_worker, args=(path, str(marker)))
    process.start()
    process.join(timeout=20)
    assert process.exitcode == 0
    assert marker.exists()

    backend = SQLiteAdmissionBackend(path)
    assert backend.try_acquire(1, lease_ttl_seconds=0.4).acquired is False
    time.sleep(0.5)
    recovered = backend.try_acquire(1, lease_ttl_seconds=0.4)
    assert recovered.acquired is True
    assert backend.snapshot()["expired_reaped"] >= 1
    backend.release(recovered.lease_id)


def test_service_can_fail_closed_or_report_memory_fallback(tmp_path: pathlib.Path) -> None:
    blocker = tmp_path / "not-directory"
    blocker.write_text("blocked", encoding="utf-8")
    base = {
        "max_concurrent_requests": 1,
        "concurrency_backend": "sqlite",
        "concurrency_db_path": str(blocker / "admission.db"),
        "concurrency_queue_timeout_seconds": 0,
    }
    closed = AdmissionService()
    with pytest.raises(GatewayUnavailableError):
        closed.acquire({**base, "concurrency_fallback_backend": "none"})

    fallback = AdmissionService()
    lease = fallback.acquire({**base, "concurrency_fallback_backend": "memory"})
    assert lease.backend == "memory_fallback"
    description = fallback.describe({**base, "concurrency_fallback_backend": "memory"})
    assert description["degraded"] is True
    assert description["scope"] == "process"
    lease.release()


def test_admin_metrics_reports_shared_admission_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_config = gateway_config.CONFIG_PATH
    gateway_config.CONFIG_PATH = tmp_path / "config.json"
    database = tmp_path / "admission.db"
    monkeypatch.setenv("GATEWAY_CONCURRENCY_DB_PATH", str(database))
    cfg = gateway_config._default_config()
    cfg["gateway"]["max_concurrent_requests"] = 2
    cfg["gateway"]["concurrency_backend"] = "sqlite"
    cfg["gateway"]["concurrency_db_path"] = str(database)
    gateway_config.save_config(cfg)
    from src.gateway_admission import ADMISSION_SERVICE

    lease = ADMISSION_SERVICE.acquire(cfg["gateway"])
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        token = base64.b64encode(b"admin:admin").decode("ascii")
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/admin/metrics",
            headers={"authorization": f"Basic {token}"},
        )
        text = urllib.request.urlopen(request, timeout=5).read().decode("utf-8")
        assert "gateway_request_admission_active 1" in text
        assert "gateway_request_admission_limit 2" in text
        assert 'gateway_request_admission_backend_info{backend="sqlite",configured="sqlite"} 1' in text
        assert "gateway_request_admission_degraded 0" in text
    finally:
        lease.release()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_config.CONFIG_PATH = old_config


@pytest.mark.skipif(os.name == "nt", reason="live multi-process HTTP contract is exercised on POSIX")
def test_two_live_gateway_processes_share_one_admission_limit(tmp_path: pathlib.Path) -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    shared_db = tmp_path / "shared-admission.sqlite3"
    holder_backend = SQLiteAdmissionBackend(shared_db, busy_timeout_ms=5000)
    holder = holder_backend.try_acquire(1, lease_ttl_seconds=30)
    assert holder.acquired
    processes: list[subprocess.Popen] = []
    ports = [_free_port(), _free_port()]
    try:
        for index, port in enumerate(ports):
            env = {
                **os.environ,
                "GATEWAY_CONFIG_PATH": str(tmp_path / f"config-{index}.json"),
                "GATEWAY_RUNTIME_DIR": str(tmp_path / f"runtime-{index}"),
                "GATEWAY_SQLITE_LOG_PATH": str(tmp_path / f"log-{index}.sqlite3"),
                "GATEWAY_PERSISTENCE_DB_PATH": str(tmp_path / f"persistence-{index}.sqlite3"),
                "GATEWAY_CONCURRENCY_BACKEND": "sqlite",
                "GATEWAY_CONCURRENCY_DB_PATH": str(shared_db),
                "GATEWAY_CONCURRENCY_FALLBACK_BACKEND": "none",
                "GATEWAY_MAX_CONCURRENT_REQUESTS": "1",
                "GATEWAY_CONCURRENCY_QUEUE_TIMEOUT": "0",
                "GATEWAY_RATE_LIMIT_ENABLED": "0",
                "GATEWAY_DOWNSTREAM_KEY": "admission-test-key",
                "GATEWAY_ADMIN_PASSWORD": "admission-test-admin",
                "UPSTREAM_BASE_URL": "http://127.0.0.1:9",
                "UPSTREAM_API_KEY": "test-only",
                "UPSTREAM_MODEL": "test-model",
                "GATEWAY_MAINTENANCE_ENABLED": "0",
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
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=0.5):
                        break
                except Exception:
                    if time.time() >= deadline:
                        raise
                    time.sleep(0.05)

        statuses = []
        for port in ports:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/tools/call",
                data=json.dumps({"tool": "calculator", "arguments": {"expression": "20+22"}}).encode(),
                headers={"Authorization": "Bearer admission-test-key", "Content-Type": "application/json"},
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request, timeout=5)
            statuses.append(denied.value.code)
            payload = json.loads(denied.value.read())
            assert payload["error"]["detail"]["backend"] == "sqlite"
        assert statuses == [429, 429]

        holder_backend.release(holder.lease_id)
        request = urllib.request.Request(
            f"http://127.0.0.1:{ports[0]}/v1/tools/call",
            data=json.dumps({"tool": "calculator", "arguments": {"expression": "20+22"}}).encode(),
            headers={"Authorization": "Bearer admission-test-key", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["content"] == "42"
    finally:
        try:
            holder_backend.release(holder.lease_id)
        except Exception:
            pass
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
