from __future__ import annotations

import importlib
import os
import pathlib
import sqlite3
import stat
import sys
import threading

import pytest

from src.gateway_sqlite import (
    SQLiteSecurityError,
    ensure_private_directory,
    secure_sqlite_artifacts,
    secure_sqlite_connect,
    set_secure_sqlite_journal_mode,
)


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_secure_connect_tightens_database_and_shared_parent_is_unchanged(tmp_path: pathlib.Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    database = shared / "gateway.db"
    database.touch(mode=0o666)
    os.chmod(database, 0o666)

    connection = secure_sqlite_connect(database, private_parent=False)
    connection.close()

    assert _mode(shared) == 0o755
    assert _mode(database) == 0o600


def test_private_directory_and_sqlite_artifacts_are_restrictive(tmp_path: pathlib.Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o777)
    os.chmod(runtime, 0o777)
    database = runtime / "gateway.db"

    connection = secure_sqlite_connect(database, private_parent=True)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('private')")
    connection.commit()
    secure_sqlite_artifacts(database)

    assert _mode(runtime) == 0o700
    assert _mode(database) == 0o600
    assert _mode(pathlib.Path(f"{database}-wal")) == 0o600
    assert _mode(pathlib.Path(f"{database}-shm")) == 0o600
    connection.close()


def test_sqlite_symlink_is_rejected(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "target.db"
    target.write_bytes(b"")
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)

    with pytest.raises(SQLiteSecurityError, match="safely"):
        secure_sqlite_connect(alias)


def test_directory_symlink_is_rejected(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "real-runtime"
    target.mkdir()
    alias = tmp_path / "runtime"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(SQLiteSecurityError, match="real directory"):
        ensure_private_directory(alias, enforce_existing=True)


def test_permission_failure_is_fail_closed(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "gateway.db"

    def deny_fchmod(_fd: int, _mode: int) -> None:
        raise PermissionError("denied by test")

    monkeypatch.setattr(os, "fchmod", deny_fchmod)
    with pytest.raises(SQLiteSecurityError, match="Cannot secure SQLite artifact"):
        secure_sqlite_connect(database)


def test_concurrent_first_connect_keeps_restrictive_modes(tmp_path: pathlib.Path) -> None:
    runtime = tmp_path / "runtime"
    database = runtime / "concurrent.db"
    errors: list[BaseException] = []

    def initialize(index: int) -> None:
        try:
            connection = secure_sqlite_connect(database, private_parent=True, timeout=30.0)
            set_secure_sqlite_journal_mode(connection, database, "WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS values_table (value INTEGER)")
            connection.execute("INSERT INTO values_table VALUES (?)", (index,))
            connection.commit()
            secure_sqlite_artifacts(database)
            connection.close()
        except BaseException as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=initialize, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _mode(runtime) == 0o700
    assert _mode(database) == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 8


def test_gateway_logging_database_is_mode_0600(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src import gateway_logging

    database = tmp_path / "gateway-log.db"
    monkeypatch.setenv("GATEWAY_SQLITE_LOG_PATH", str(database))
    gateway_logging.SQLITE_READY = False
    gateway_logging._sqlite_init()

    assert _mode(database) == 0o600
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_invalid_journal_mode_is_rejected_without_sql_execution(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "gateway.db"
    connection = secure_sqlite_connect(database)
    try:
        with pytest.raises(SQLiteSecurityError, match="unsupported SQLite journal mode"):
            set_secure_sqlite_journal_mode(connection, database, "WAL; DROP TABLE secrets")
    finally:
        connection.close()


def test_default_persistence_runtime_is_private(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src import gateway_persistence

    monkeypatch.chdir(tmp_path)
    gateway_persistence.close_persistence()
    try:
        gateway_persistence.init_persistence()
        runtime = tmp_path / ".gateway_runtime"
        database = runtime / "gateway.db"
        assert _mode(runtime) == 0o700
        assert _mode(database) == 0o600
        assert gateway_persistence._get_db().execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    finally:
        gateway_persistence.close_persistence()


def test_default_stats_runtime_is_private(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src import gateway_stats

    runtime = tmp_path / "runtime"
    monkeypatch.delenv("GATEWAY_STATS_DB_PATH", raising=False)
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(runtime))
    if gateway_stats._db_conn is not None:
        gateway_stats._db_conn.close()
        gateway_stats._db_conn = None
    try:
        gateway_stats._get_db()
        assert _mode(runtime) == 0o700
        assert _mode(runtime / "stats.db") == 0o600
        assert gateway_stats._get_db().execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    finally:
        if gateway_stats._db_conn is not None:
            gateway_stats._db_conn.close()
            gateway_stats._db_conn = None


def test_default_planner_runtime_is_private(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.gateway_agent_planner import AgentPlannerStore

    runtime = tmp_path / "runtime"
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(runtime))
    AgentPlannerStore()

    assert _mode(runtime) == 0o700
    assert _mode(runtime / "agent_planner.sqlite3") == 0o600
    with sqlite3.connect(runtime / "agent_planner.sqlite3") as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_persistence_remains_importable_in_legacy_top_level_mode() -> None:
    src_dir = pathlib.Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(src_dir))
    try:
        module = importlib.import_module("gateway_persistence")
        assert module.PersistenceConfig().db_path == ".gateway_runtime/gateway.db"
    finally:
        sys.path.remove(str(src_dir))
        sys.modules.pop("gateway_persistence", None)
