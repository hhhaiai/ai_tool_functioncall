from __future__ import annotations

import base64
import json
import os
import pathlib
import sqlite3
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections import namedtuple
from http.server import ThreadingHTTPServer

import pytest

from src import gateway_config
from src.gateway_http_handler import GatewayHandler, _capability_contract
from src import gateway_sqlite_compact as compact


def _legacy_database(path: pathlib.Path, *, rows: int = 500, delete: int = 350) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=17")
        connection.execute("PRAGMA application_id=1196573005")
        connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
        connection.execute("CREATE INDEX idx_records_payload ON records(payload)")
        connection.executemany(
            "INSERT INTO records(payload) VALUES(?)",
            [(f"record-{index}-" + "x" * 1000,) for index in range(rows)],
        )
        connection.commit()
        connection.execute("DELETE FROM records WHERE id <= ?", (delete,))
        connection.commit()


def _mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_inspect_database_is_read_only_and_reports_reclaimable_space(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    os.chmod(database, 0o644)

    result = compact.inspect_database(database, include_counts=True)

    assert result["eligible"] is True
    assert result["manifest"]["integrity_ok"] is True
    assert result["manifest"]["auto_vacuum_mode"] == 0
    assert result["manifest"]["table_counts"] == {"records": 150}
    assert result["estimated_reclaimable_bytes"] > 0
    assert result["needs_auto_vacuum_migration"] is True
    assert _mode(database) == 0o644
    assert not pathlib.Path(f"{database}.compact.lock").exists()


def test_inspect_rejects_missing_symlink_and_active_artifacts(tmp_path: pathlib.Path) -> None:
    missing = compact.inspect_database(tmp_path / "missing.db")
    assert missing["blockers"] == ["database_missing"]

    target = tmp_path / "target.db"
    _legacy_database(target, rows=10, delete=0)
    alias = tmp_path / "alias.db"
    alias.symlink_to(target)
    linked = compact.inspect_database(alias)
    assert linked["blockers"] == ["database_symlink"]

    wal = pathlib.Path(f"{target}-wal")
    wal.touch()
    active = compact.inspect_database(target)
    assert active["eligible"] is False
    assert "wal_shm_or_journal_present" in active["blockers"]
    assert str(wal) in active["active_artifacts"]


def test_compaction_requires_explicit_stopped_confirmation(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    with pytest.raises(compact.SQLiteCompactionError, match="explicit confirmation"):
        compact.compact_database(database, confirm_gateway_stopped=False)


def test_compaction_preserves_data_schema_owner_and_enables_incremental_vacuum(
    tmp_path: pathlib.Path,
) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    before_info = database.stat()
    before_bytes = before_info.st_size

    result = compact.compact_database(database, confirm_gateway_stopped=True)

    assert result["ok"] is True
    assert result["before"]["table_counts"] == result["after"]["table_counts"] == {"records": 150}
    assert result["before"]["schema_sha256"] == result["after"]["schema_sha256"]
    assert result["after"]["auto_vacuum_mode"] == 2
    assert result["before"]["user_version"] == result["after"]["user_version"] == 17
    assert result["before"]["application_id"] == result["after"]["application_id"] == 1196573005
    assert result["before"]["sqlite_sequence"] == result["after"]["sqlite_sequence"] == {"records": 500}
    assert result["after"]["foreign_key_violations"] == []
    assert result["after_bytes"] < before_bytes
    assert result["reclaimed_bytes"] == before_bytes - result["after_bytes"]
    assert _mode(database) == 0o600
    after_info = database.stat()
    assert (after_info.st_uid, after_info.st_gid) == (before_info.st_uid, before_info.st_gid)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 150
    assert not list(tmp_path.glob(".legacy.db.compact-*"))
    assert _mode(pathlib.Path(f"{database}.compact.lock")) == 0o600


def test_compaction_fails_when_disk_headroom_is_insufficient(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(compact.shutil, "disk_usage", lambda _path: Usage(100, 100, 0))

    inspection = compact.inspect_database(database)
    assert "insufficient_disk_space" in inspection["blockers"]
    with pytest.raises(compact.SQLiteCompactionError, match="insufficient_disk_space"):
        compact.compact_database(database, confirm_gateway_stopped=True)


def test_compaction_lock_rejects_concurrent_operator(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    with compact._exclusive_compaction_lock(database):
        with pytest.raises(compact.SQLiteCompactionError, match="already running"):
            compact.compact_database(database, confirm_gateway_stopped=True)


def test_compaction_rejects_live_sqlite_writer_without_replacing_source(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    writer = sqlite3.connect(database, timeout=0.0)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(compact.SQLiteCompactionError, match="database is locked"):
            compact.compact_database(database, confirm_gateway_stopped=True)
    finally:
        writer.rollback()
        writer.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 150
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0


def test_source_change_conflict_keeps_original_database(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    original_signature = compact._source_signature
    calls = 0

    def changed_signature(path: pathlib.Path):
        nonlocal calls
        calls += 1
        value = original_signature(path)
        if calls >= 2:
            return (*value[:-1], value[-1] + 1)
        return value

    monkeypatch.setattr(compact, "_source_signature", changed_signature)
    with pytest.raises(compact.SQLiteCompactionError, match="changed during compaction"):
        compact.compact_database(database, confirm_gateway_stopped=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 150
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    assert not list(tmp_path.glob(".legacy.db.compact-*"))


def test_candidate_manifest_mismatch_keeps_original_database(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    original_manifest = compact._database_manifest
    calls = 0

    def mismatched_manifest(connection: sqlite3.Connection, *, include_counts: bool):
        nonlocal calls
        calls += 1
        result = original_manifest(connection, include_counts=include_counts)
        if calls == 2:
            result["table_counts"] = {"records": 149}
        return result

    monkeypatch.setattr(compact, "_database_manifest", mismatched_manifest)
    with pytest.raises(compact.SQLiteCompactionError, match="table counts differ"):
        compact.compact_database(database, confirm_gateway_stopped=True)

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 150
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0


def test_compaction_timeout_cancels_candidate_and_keeps_source(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database, rows=3000, delete=2000)
    monotonic_calls = 0

    def advancing_monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        return 0.0 if monotonic_calls <= 2 else 10.0

    monkeypatch.setattr(compact.time, "monotonic", advancing_monotonic)
    with pytest.raises(compact.SQLiteCompactionError, match="timed out"):
        compact.compact_database(
            database,
            confirm_gateway_stopped=True,
            timeout_seconds=1.0,
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1000
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    assert not list(tmp_path.glob(".legacy.db.compact-*"))


def test_installed_verification_failure_rolls_back_original_database(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    original_inode = database.stat().st_ino
    original_manifest = compact._database_manifest
    calls = 0

    def fail_installed_manifest(connection: sqlite3.Connection, *, include_counts: bool):
        nonlocal calls
        calls += 1
        result = original_manifest(connection, include_counts=include_counts)
        if calls == 3:
            result["schema_sha256"] = "installed-mismatch"
        return result

    monkeypatch.setattr(compact, "_database_manifest", fail_installed_manifest)
    with pytest.raises(compact.SQLiteCompactionError, match="installed database verification"):
        compact.compact_database(database, confirm_gateway_stopped=True)

    assert database.stat().st_ino == original_inode
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 150
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert not list(tmp_path.glob(".legacy.db.compact-*"))


def test_rollback_failure_preserves_original_backup_for_operator_recovery(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database)
    original_manifest = compact._database_manifest
    manifest_calls = 0

    def fail_installed_manifest(connection: sqlite3.Connection, *, include_counts: bool):
        nonlocal manifest_calls
        manifest_calls += 1
        result = original_manifest(connection, include_counts=include_counts)
        if manifest_calls == 3:
            result["schema_sha256"] = "installed-mismatch"
        return result

    original_replace = compact.os.replace
    replace_calls = 0

    def fail_rollback(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("rollback failure marker")
        return original_replace(source, destination)

    monkeypatch.setattr(compact, "_database_manifest", fail_installed_manifest)
    monkeypatch.setattr(compact.os, "replace", fail_rollback)
    with pytest.raises(compact.SQLiteCompactionError, match="backup preserved") as raised:
        compact.compact_database(database, confirm_gateway_stopped=True)

    backups = list(tmp_path.glob(".legacy.db.compact-backup-*"))
    assert len(backups) == 1
    assert str(backups[0]) in str(raised.value)
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 150
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0


def test_cli_defaults_to_preflight_and_execute_requires_confirmation(tmp_path: pathlib.Path) -> None:
    database = tmp_path / "legacy.db"
    _legacy_database(database, rows=20, delete=10)
    preflight = subprocess.run(
        [sys.executable, "-m", "src.gateway_sqlite_compact", "--database", str(database)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert preflight.returncode == 0
    assert json.loads(preflight.stdout)["eligible"] is True

    denied = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.gateway_sqlite_compact",
            "--database",
            str(database),
            "--execute",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert denied.returncode == 1
    assert "explicit confirmation" in json.loads(denied.stderr)["error"]


def test_admin_storage_preflight_is_authenticated_and_non_destructive(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "primary.db"
    _legacy_database(database, rows=20, delete=10)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("GATEWAY_SQLITE_LOG_PATH", str(database))
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(runtime))
    old_config = gateway_config.CONFIG_PATH
    gateway_config.CONFIG_PATH = tmp_path / "config.json"
    cfg = gateway_config._default_config()
    gateway_config.save_config(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/admin/storage.json"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(url, timeout=5)
        assert denied.value.code == 401

        token = base64.b64encode(b"admin:admin").decode("ascii")
        request = urllib.request.Request(url, headers={"authorization": f"Basic {token}"})
        payload = json.loads(urllib.request.urlopen(request, timeout=5).read().decode("utf-8"))
        assert payload["compaction"]["execution"] == "offline_cli_only"
        assert payload["compaction"]["online_execution_supported"] is False
        assert payload["databases"]["primary"]["eligible"] is True
        assert payload["databases"]["primary"]["manifest"]["auto_vacuum_mode"] == 0
        assert not pathlib.Path(f"{database}.compact.lock").exists()
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_config.CONFIG_PATH = old_config


def test_capabilities_disclose_offline_only_compaction() -> None:
    operations = _capability_contract()["operations"]
    assert operations["persistent_storage_preflight"] == "/admin/storage.json"
    assert operations["legacy_sqlite_compaction"] == "offline_cli_only"
    assert operations["online_destructive_vacuum"] is False
