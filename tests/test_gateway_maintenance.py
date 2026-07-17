from __future__ import annotations

import datetime as dt
import os
import pathlib
import sqlite3
import time

import pytest

from src import gateway_logging, gateway_persistence, gateway_stats
from src.gateway_agent_planner import AgentPlannerStore
from src.gateway_maintenance import (
    cleanup_stale_runtime,
    maintenance_snapshot,
    record_maintenance_crash,
    run_gateway_maintenance,
)


def _reset_logging(database: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_SQLITE_LOG_PATH", str(database))
    gateway_logging.SQLITE_READY = False
    gateway_logging._sqlite_init()


def test_primary_retention_is_batched_and_reports_capacity(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "primary.db"
    _reset_logging(database, monkeypatch)
    old = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc).isoformat()
    recent = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).isoformat()
    with sqlite3.connect(database) as connection:
        for index in range(5):
            connection.execute(
                "INSERT INTO request_logs(ts,request_id,path,status,request_json) VALUES(?,?,?,?,?)",
                (old, f"old-{index}", "/old", 200, "{}"),
            )
        connection.execute(
            "INSERT INTO request_logs(ts,request_id,path,status,request_json) VALUES(?,?,?,?,?)",
            (recent, "recent", "/recent", 200, "{}"),
        )
        connection.commit()

    first = gateway_logging.cleanup_primary_sqlite(
        request_retention_days=30,
        request_max_rows=100,
        failure_retention_days=30,
        failure_max_rows=100,
        memory_retention_days=30,
        memory_max_rows=100,
        batch_size=2,
        max_batches=2,
        now=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
    )
    assert first["eligible"]["request_logs"] == 5
    assert first["deleted"]["request_logs"] == 4
    assert first["rows"]["request_logs"] == 2
    assert first["space_bytes"] > 0
    assert set(first["checkpoint"]) == {"busy", "log_frames", "checkpointed_frames"}

    second = gateway_logging.cleanup_primary_sqlite(
        request_retention_days=30,
        request_max_rows=100,
        batch_size=2,
        max_batches=2,
        now=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
    )
    assert second["deleted"]["request_logs"] == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT request_id FROM request_logs").fetchall() == [("recent",)]


def test_primary_max_rows_applies_even_to_recent_records(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "primary.db"
    _reset_logging(database, monkeypatch)
    recent = dt.datetime.now(dt.timezone.utc).isoformat()
    with sqlite3.connect(database) as connection:
        for index in range(7):
            connection.execute(
                "INSERT INTO tool_failures(ts,tool_name,call_id,arguments_keys_json,content) VALUES(?,?,?,?,?)",
                (recent, "Bash", f"call-{index}", "[]", "failure"),
            )
        connection.commit()

    result = gateway_logging.cleanup_primary_sqlite(
        request_retention_days=3650,
        request_max_rows=100,
        failure_retention_days=3650,
        failure_max_rows=3,
        memory_retention_days=3650,
        memory_max_rows=100,
        batch_size=10,
        max_batches=1,
    )
    assert result["deleted"]["tool_failures"] == 4
    assert result["rows"]["tool_failures"] == 3


def test_primary_retention_dry_run_does_not_delete(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "primary.db"
    _reset_logging(database, monkeypatch)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO conversation_memories(ts,session_key,workspace_root,kind,summary) VALUES(?,?,?,?,?)",
            ("2020-01-01T00:00:00+00:00", "session", "/workspace", "summary", "old"),
        )
        connection.commit()

    result = gateway_logging.cleanup_primary_sqlite(
        memory_retention_days=1,
        memory_max_rows=100,
        dry_run=True,
        now=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
    )
    assert result["eligible"]["conversation_memories"] == 1
    assert result["deleted"]["conversation_memories"] == 0
    assert result["rows"]["conversation_memories"] == 1


def test_planner_retention_bounds_sessions_and_events(tmp_path: pathlib.Path) -> None:
    store = AgentPlannerStore(tmp_path / "planner.db")
    with store._connect() as connection:
        for index in range(6):
            connection.execute(
                "INSERT INTO planner_sessions(session_key,state_json,updated_at) VALUES(?,?,?)",
                (f"session-{index}", "{}", 1.0),
            )
            connection.execute(
                "INSERT INTO runtime_events(ts,event_type) VALUES(?,?)",
                (1.0, "old"),
            )

    result = store.cleanup(
        retention_days=1,
        max_sessions=3,
        max_events=3,
        batch_size=2,
        max_batches=2,
        now=10 * 86400,
    )
    assert result["deleted"]["planner_sessions"] == 4
    assert result["deleted"]["runtime_events"] == 4
    assert result["rows"] == {"planner_sessions": 2, "runtime_events": 2}


def test_runtime_cleanup_is_allowlisted_bounded_and_dry_run_first(tmp_path: pathlib.Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    stale = runtime / "agent-planner-smoke-old"
    stale.mkdir()
    (stale / "payload.txt").write_text("old", encoding="utf-8")
    arbitrary = runtime / "customer-data"
    arbitrary.mkdir()
    (arbitrary / "keep.txt").write_text("keep", encoding="utf-8")
    timestamp = time.time() - 20 * 86400
    os.utime(stale, (timestamp, timestamp))
    os.utime(arbitrary, (timestamp, timestamp))

    preview = cleanup_stale_runtime(runtime, retention_days=7, dry_run=True)
    assert preview["eligible"] == 1
    assert preview["selected"] == 1
    assert preview["would_remove"] == 1
    assert preview["removed"] == 0
    assert stale.exists()
    assert arbitrary.exists()

    applied = cleanup_stale_runtime(runtime, retention_days=7, dry_run=False)
    assert applied["removed"] == 1
    assert not stale.exists()
    assert arbitrary.exists()


def test_runtime_cleanup_skips_oversized_entries(tmp_path: pathlib.Path) -> None:
    runtime = tmp_path / "runtime"
    stale = runtime / "project-scope-cli-smoke-old"
    stale.mkdir(parents=True)
    (stale / "payload.bin").write_bytes(b"x" * 32)
    timestamp = time.time() - 20 * 86400
    os.utime(stale, (timestamp, timestamp))

    result = cleanup_stale_runtime(
        runtime,
        retention_days=7,
        max_entry_bytes=16,
        dry_run=False,
    )
    assert result["skipped_oversized"] == 1
    assert stale.exists()


def test_maintenance_failure_is_observable(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("GATEWAY_STATS_DB_PATH", str(runtime / "stats.db"))
    gateway_persistence.close_persistence()
    gateway_persistence.init_persistence(
        gateway_persistence.PersistenceConfig(db_path=str(runtime / "gateway.db"))
    )
    if gateway_stats._db_conn is not None:
        gateway_stats._db_conn.close()
        gateway_stats._db_conn = None

    def fail_cleanup(*_args, **_kwargs):
        raise OSError("maintenance failure marker")

    monkeypatch.setattr(gateway_persistence, "cleanup_expired_semantic_cache", fail_cleanup)
    config = {
        "gateway": {"logging_backend": "jsonl"},
        "stats": {"retention_days": 30},
        "maintenance": {"dry_run": False, "runtime_cleanup_enabled": False},
    }
    try:
        result = run_gateway_maintenance(config, now=time.time())
        assert result["last_success"] is False
        assert result["failures_total"] >= 1
        assert result["components"]["persistence"]["ok"] is False
        assert "maintenance failure marker" in result["last_error"]
        assert maintenance_snapshot()["last_error"] == result["last_error"]
    finally:
        gateway_persistence.close_persistence()
        if gateway_stats._db_conn is not None:
            gateway_stats._db_conn.close()
            gateway_stats._db_conn = None


def test_cycle_level_maintenance_crash_is_observable() -> None:
    before = maintenance_snapshot()["failures_total"]
    result = record_maintenance_crash(RuntimeError("cycle crash marker"))
    assert result["last_success"] is False
    assert result["failures_total"] == before + 1
    assert result["components"]["cycle"]["error_type"] == "RuntimeError"
    assert "cycle crash marker" in result["last_error"]
