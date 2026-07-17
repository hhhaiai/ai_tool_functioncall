"""Offline, fail-closed compaction for legacy Gateway SQLite databases.

Full SQLite VACUUM is intentionally not exposed as an online HTTP mutation.
Operators can inspect databases through the authenticated Admin API, then run
this module as a CLI only after stopping every Gateway process that can access
the target database.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Gateway production targets are POSIX
    fcntl = None  # type: ignore[assignment]

_gateway_sqlite = importlib.import_module(
    f"{__package__}.gateway_sqlite" if __package__ else "gateway_sqlite"
)

secure_sqlite_artifacts = _gateway_sqlite.secure_sqlite_artifacts

Json = dict[str, Any]
_MIN_HEADROOM_BYTES = 16 * 1024 * 1024


class SQLiteCompactionError(RuntimeError):
    """Raised when offline compaction cannot be proven safe."""


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _source_signature(path: pathlib.Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _sqlite_artifacts(path: pathlib.Path) -> list[pathlib.Path]:
    return [pathlib.Path(f"{path}-wal"), pathlib.Path(f"{path}-shm"), pathlib.Path(f"{path}-journal")]


def _readonly_connection(path: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0)


def _integrity_result(connection: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]


def _database_manifest(connection: sqlite3.Connection, *, include_counts: bool) -> Json:
    integrity = _integrity_result(connection)
    schema_rows = connection.execute(
        """
        SELECT type, name, tbl_name, COALESCE(sql, '')
        FROM sqlite_schema
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name, tbl_name, sql
        """
    ).fetchall()
    schema_payload = json.dumps(schema_rows, ensure_ascii=False, separators=(",", ":"))
    manifest: Json = {
        "integrity": integrity,
        "integrity_ok": integrity == ["ok"],
        "schema_sha256": hashlib.sha256(schema_payload.encode("utf-8")).hexdigest(),
        "schema_objects": len(schema_rows),
        "page_size": int(connection.execute("PRAGMA page_size").fetchone()[0]),
        "page_count": int(connection.execute("PRAGMA page_count").fetchone()[0]),
        "freelist_count": int(connection.execute("PRAGMA freelist_count").fetchone()[0]),
        "auto_vacuum_mode": int(connection.execute("PRAGMA auto_vacuum").fetchone()[0]),
        "journal_mode": str(connection.execute("PRAGMA journal_mode").fetchone()[0]),
        "user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(connection.execute("PRAGMA application_id").fetchone()[0]),
        "encoding": str(connection.execute("PRAGMA encoding").fetchone()[0]),
    }
    if include_counts:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for table in tables:
            counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
            )
        manifest["table_counts"] = counts
        sequence_exists = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        manifest["sqlite_sequence"] = (
            {
                str(row[0]): int(row[1])
                for row in connection.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall()
            }
            if sequence_exists
            else {}
        )
        foreign_rows = connection.execute(
            "SELECT * FROM pragma_foreign_key_check LIMIT 1001"
        ).fetchall()
        manifest["foreign_key_violations"] = [list(row) for row in foreign_rows[:1000]]
        manifest["foreign_key_violations_truncated"] = len(foreign_rows) > 1000
    return manifest


def inspect_database(
    path: pathlib.Path | str,
    *,
    include_counts: bool = False,
    headroom_ratio: float = 1.25,
) -> Json:
    """Return a read-only compaction preflight without modifying the database."""
    database = pathlib.Path(path).expanduser().absolute()
    result: Json = {
        "path": str(database),
        "exists": database.exists(),
        "eligible": False,
        "blockers": [],
        "active_artifacts": [],
    }
    try:
        info = database.lstat()
    except FileNotFoundError:
        result["blockers"].append("database_missing")
        return result
    if stat.S_ISLNK(info.st_mode):
        result["blockers"].append("database_symlink")
        return result
    if not stat.S_ISREG(info.st_mode):
        result["blockers"].append("database_not_regular")
        return result

    result.update({
        "size_bytes": int(info.st_size),
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "owner_uid": int(info.st_uid),
        "owner_gid": int(info.st_gid),
    })
    active = [str(item) for item in _sqlite_artifacts(database) if item.exists()]
    result["active_artifacts"] = active
    if active:
        result["blockers"].append("wal_shm_or_journal_present")
        return result

    try:
        with _readonly_connection(database) as connection:
            manifest = _database_manifest(connection, include_counts=include_counts)
    except sqlite3.Error as exc:
        result["blockers"].append("sqlite_open_or_check_failed")
        result["error_type"] = exc.__class__.__name__
        result["error"] = str(exc)[:1000]
        return result
    result["manifest"] = manifest
    if not manifest["integrity_ok"]:
        result["blockers"].append("quick_check_failed")

    live_pages = max(1, int(manifest["page_count"]) - int(manifest["freelist_count"]))
    estimated_output = live_pages * int(manifest["page_size"])
    required = max(_MIN_HEADROOM_BYTES, int(estimated_output * max(1.0, float(headroom_ratio))))
    free = int(shutil.disk_usage(database.parent).free)
    result.update({
        "estimated_output_bytes": estimated_output,
        "required_free_bytes": required,
        "available_free_bytes": free,
        "estimated_reclaimable_bytes": max(0, int(info.st_size) - estimated_output),
        "needs_auto_vacuum_migration": int(manifest["auto_vacuum_mode"]) != 2,
    })
    if free < required:
        result["blockers"].append("insufficient_disk_space")
    result["eligible"] = not result["blockers"]
    return result


@contextlib.contextmanager
def _exclusive_compaction_lock(database: pathlib.Path) -> Iterator[pathlib.Path]:
    if fcntl is None:
        raise SQLiteCompactionError("offline compaction requires POSIX advisory locks")
    lock_path = pathlib.Path(f"{database}.compact.lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SQLiteCompactionError(f"cannot open compaction lock: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SQLiteCompactionError("compaction lock is not a regular file")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SQLiteCompactionError("another compaction is already running") from exc
        yield lock_path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _fsync_directory(path: pathlib.Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _apply_original_ownership(path: pathlib.Path, source_info: os.stat_result) -> None:
    os.chmod(path, 0o600, follow_symlinks=False)
    candidate = path.lstat()
    if (candidate.st_uid, candidate.st_gid) != (source_info.st_uid, source_info.st_gid):
        os.chown(path, source_info.st_uid, source_info.st_gid, follow_symlinks=False)


def compact_database(
    path: pathlib.Path | str,
    *,
    confirm_gateway_stopped: bool,
    timeout_seconds: float = 3600.0,
    headroom_ratio: float = 1.25,
) -> Json:
    """Compact one stopped Gateway database and atomically install the result."""
    if not confirm_gateway_stopped:
        raise SQLiteCompactionError("execution requires explicit confirmation that all Gateway processes are stopped")
    database = pathlib.Path(path).expanduser().absolute()
    with _exclusive_compaction_lock(database):
        preflight = inspect_database(database, include_counts=True, headroom_ratio=headroom_ratio)
        if not preflight.get("eligible"):
            blockers = ", ".join(str(item) for item in preflight.get("blockers") or [])
            raise SQLiteCompactionError(f"database is not eligible for offline compaction: {blockers}")
        source_info = database.lstat()
        source_signature = _source_signature(database)
        before_manifest = dict(preflight["manifest"])
        started = time.monotonic()
        candidate_fd, candidate_name = tempfile.mkstemp(
            prefix=f".{database.name}.compact-",
            suffix=".sqlite3",
            dir=str(database.parent),
        )
        os.close(candidate_fd)
        candidate = pathlib.Path(candidate_name)
        candidate.unlink()
        backup = database.parent / f".{database.name}.compact-backup-{uuid.uuid4().hex}"
        source_connection: sqlite3.Connection | None = None
        installed = False
        preserve_backup = False
        try:
            source_connection = sqlite3.connect(str(database), timeout=0.0)
            source_connection.execute("PRAGMA busy_timeout=0")
            locking_mode = str(source_connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0])
            if locking_mode.lower() != "exclusive":
                raise SQLiteCompactionError("could not enable exclusive SQLite locking mode")
            source_connection.execute("BEGIN EXCLUSIVE")
            source_connection.commit()
            source_connection.execute("PRAGMA auto_vacuum=INCREMENTAL")

            deadline = time.monotonic() + max(0.001, float(timeout_seconds))

            def progress() -> int:
                return 1 if time.monotonic() >= deadline else 0

            if time.monotonic() >= deadline:
                raise SQLiteCompactionError("SQLite compaction timed out before execution")
            source_connection.set_progress_handler(progress, 1_000)
            try:
                source_connection.execute("VACUUM INTO ?", (str(candidate),))
            except sqlite3.OperationalError as exc:
                if time.monotonic() >= deadline:
                    raise SQLiteCompactionError("SQLite compaction timed out") from exc
                raise
            finally:
                source_connection.set_progress_handler(None, 0)

            _apply_original_ownership(candidate, source_info)
            secure_sqlite_artifacts(candidate)
            with _readonly_connection(candidate) as candidate_connection:
                after_manifest = _database_manifest(candidate_connection, include_counts=True)
            if not after_manifest["integrity_ok"]:
                raise SQLiteCompactionError("compacted database failed quick_check")
            if after_manifest["schema_sha256"] != before_manifest["schema_sha256"]:
                raise SQLiteCompactionError("compacted database schema differs from source")
            if after_manifest.get("table_counts") != before_manifest.get("table_counts"):
                raise SQLiteCompactionError("compacted database table counts differ from source")
            for field in (
                "user_version",
                "application_id",
                "encoding",
                "sqlite_sequence",
                "foreign_key_violations",
                "foreign_key_violations_truncated",
            ):
                if after_manifest.get(field) != before_manifest.get(field):
                    raise SQLiteCompactionError(f"compacted database {field} differs from source")
            if int(after_manifest["auto_vacuum_mode"]) != 2:
                raise SQLiteCompactionError("compacted database did not enable incremental auto-vacuum")
            if _source_signature(database) != source_signature:
                raise SQLiteCompactionError("source database changed during compaction")
            active = [str(item) for item in _sqlite_artifacts(database) if item.exists()]
            if active:
                raise SQLiteCompactionError("source database created WAL/SHM/journal artifacts during compaction")

            os.link(database, backup)
            os.replace(candidate, database)
            installed = True
            _fsync_directory(database.parent)
            secure_sqlite_artifacts(database)
            with _readonly_connection(database) as installed_connection:
                installed_manifest = _database_manifest(installed_connection, include_counts=True)
            if installed_manifest != after_manifest:
                raise SQLiteCompactionError("installed database verification differs from validated candidate")
            backup.unlink()
            _fsync_directory(database.parent)
            installed = False
            return {
                "ok": True,
                "database": str(database),
                "before_bytes": int(preflight["size_bytes"]),
                "after_bytes": int(database.stat().st_size),
                "reclaimed_bytes": max(0, int(preflight["size_bytes"]) - int(database.stat().st_size)),
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "before": before_manifest,
                "after": installed_manifest,
            }
        except Exception as exc:
            rollback_error: BaseException | None = None
            if source_connection is not None:
                source_connection.close()
                source_connection = None
            if installed and backup.exists():
                try:
                    os.replace(backup, database)
                    _fsync_directory(database.parent)
                    installed = False
                except BaseException as rollback_exc:
                    rollback_error = rollback_exc
                    preserve_backup = True
            if rollback_error is not None:
                raise SQLiteCompactionError(
                    f"compaction failed and rollback also failed; original backup preserved at {backup}: "
                    f"{rollback_error}"
                ) from rollback_error
            if isinstance(exc, SQLiteCompactionError):
                raise
            if isinstance(exc, sqlite3.Error):
                raise SQLiteCompactionError(f"SQLite compaction failed: {exc}") from exc
            raise
        finally:
            if source_connection is not None:
                source_connection.close()
            if candidate.exists():
                candidate.unlink()
            if backup.exists() and not preserve_backup:
                backup.unlink()


def gateway_database_paths(config: Json | None = None) -> dict[str, pathlib.Path]:
    """Return the configured Gateway-owned database catalog."""
    if config is None:
        try:
            from .gateway_config import load_config
        except ImportError:  # pragma: no cover
            from gateway_config import load_config
        config = load_config()
    active_config: Json = config if isinstance(config, dict) else {}
    gateway_value = active_config.get("gateway")
    persistence_value = active_config.get("persistence")
    gateway: Json = gateway_value if isinstance(gateway_value, dict) else {}
    persistence: Json = persistence_value if isinstance(persistence_value, dict) else {}
    runtime = pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR") or ".gateway_runtime")
    primary = pathlib.Path(
        os.environ.get("GATEWAY_SQLITE_LOG_PATH")
        or gateway.get("sqlite_log_path")
        or "gateway_log.sqlite3"
    )
    persistence_path = pathlib.Path(
        os.environ.get("GATEWAY_PERSISTENCE_DB_PATH")
        or persistence.get("db_path")
        or runtime / "gateway.db"
    )
    return {
        "primary": primary,
        "persistence": persistence_path,
        "stats": pathlib.Path(os.environ.get("GATEWAY_STATS_DB_PATH") or runtime / "stats.db"),
        "planner": runtime / "agent_planner.sqlite3",
        "rate_limit": pathlib.Path(
            os.environ.get("GATEWAY_RATE_LIMIT_DB_PATH")
            or gateway.get("rate_limit_db_path")
            or runtime / "rate_limits.sqlite3"
        ),
        "admission": pathlib.Path(
            os.environ.get("GATEWAY_CONCURRENCY_DB_PATH")
            or gateway.get("concurrency_db_path")
            or runtime / "admission.sqlite3"
        ),
    }


def inspect_gateway_databases(config: Json | None = None) -> Json:
    return {
        name: inspect_database(path, include_counts=False)
        for name, path in gateway_database_paths(config).items()
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect or compact a stopped Gateway SQLite database")
    parser.add_argument("--database", required=True, help="SQLite database path")
    parser.add_argument("--execute", action="store_true", help="Perform compaction; default is read-only preflight")
    parser.add_argument(
        "--confirm-gateway-stopped",
        action="store_true",
        help="Required with --execute; confirms every Gateway process using this DB is stopped",
    )
    parser.add_argument("--timeout-seconds", type=float, default=3600.0)
    parser.add_argument("--headroom-ratio", type=float, default=1.25)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.execute:
            result = compact_database(
                args.database,
                confirm_gateway_stopped=bool(args.confirm_gateway_stopped),
                timeout_seconds=float(args.timeout_seconds),
                headroom_ratio=float(args.headroom_ratio),
            )
        else:
            result = inspect_database(
                args.database,
                include_counts=True,
                headroom_ratio=float(args.headroom_ratio),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("eligible", True) or result.get("ok") else 2
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error_type": exc.__class__.__name__, "error": str(exc)},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SQLiteCompactionError",
    "compact_database",
    "gateway_database_paths",
    "inspect_database",
    "inspect_gateway_databases",
    "main",
]
