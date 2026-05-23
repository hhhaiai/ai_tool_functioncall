#!/usr/bin/env python3
"""Logging and statistics for the gateway.

Handles SQLite logging, request logs, tool failure tracking, and statistics.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import sqlite3
import threading
import uuid
from typing import Any

Json = dict[str, Any]

REQUEST_LOG_PATH = pathlib.Path(os.environ.get("GATEWAY_REQUEST_LOG") or ".gateway_requests.jsonl")
STATS_PATH = pathlib.Path(os.environ.get("GATEWAY_STATS_PATH") or ".gateway_stats.json")
SQLITE_LOG_PATH = pathlib.Path(os.environ.get("GATEWAY_SQLITE_LOG_PATH") or "gateway_log.sqlite3")
SQLITE_LOCK = threading.Lock()
SQLITE_READY = False


def _sqlite_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("GATEWAY_SQLITE_LOG_PATH") or str(SQLITE_LOG_PATH))


def _sqlite_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_sqlite_path()), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _sqlite_init() -> None:
    global SQLITE_READY
    if SQLITE_READY and _sqlite_path().exists():
        return
    with SQLITE_LOCK:
        if SQLITE_READY and _sqlite_path().exists():
            return
        conn = _sqlite_connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    downstream_key TEXT,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    fake_prompt_tools INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_request_logs_ts ON request_logs(ts);
                CREATE INDEX IF NOT EXISTS idx_request_logs_path ON request_logs(path);

                CREATE TABLE IF NOT EXISTS tool_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    failure_type TEXT,
                    arguments_keys_json TEXT NOT NULL,
                    content TEXT NOT NULL,
                    fake_prompt_tools INTEGER NOT NULL DEFAULT 0,
                    execution_ms REAL,
                    retry_count INTEGER DEFAULT 0,
                    provider TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_tool_failures_ts ON tool_failures(ts);
                CREATE INDEX IF NOT EXISTS idx_tool_failures_tool ON tool_failures(tool_name);

                CREATE TABLE IF NOT EXISTS tool_stats (
                    tool_name TEXT PRIMARY KEY,
                    calls INTEGER NOT NULL DEFAULT 0,
                    success INTEGER NOT NULL DEFAULT 0,
                    failure INTEGER NOT NULL DEFAULT 0,
                    failures_json TEXT NOT NULL DEFAULT '{}',
                    last_called_at TEXT
                );

                CREATE TABLE IF NOT EXISTS request_stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS request_stats_by_path (
                    path TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS request_stats_by_status (
                    status TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS migration_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS conversation_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    workspace_root TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    source_request_id TEXT,
                    importance INTEGER NOT NULL DEFAULT 1,
                    last_used_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_memories_session ON conversation_memories(session_key, workspace_root);
                CREATE INDEX IF NOT EXISTS idx_conversation_memories_last_used ON conversation_memories(last_used_at);
                CREATE INDEX IF NOT EXISTS idx_conversation_memories_ts ON conversation_memories(ts);
                """
            )
            # Migration: add columns that may not exist in older databases
            _sqlite_migrate_add_column(conn, "tool_failures", "execution_ms", "REAL")
            _sqlite_migrate_add_column(conn, "tool_failures", "retry_count", "INTEGER DEFAULT 0")
            _sqlite_migrate_add_column(conn, "tool_failures", "provider", "TEXT")
            _sqlite_import_legacy_logs_locked(conn)
            conn.commit()
            SQLITE_READY = True
        finally:
            conn.close()


def _sqlite_migrate_add_column(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    """Add a column to a table if it doesn't already exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # Column already exists


def _sqlite_import_legacy_logs_locked(conn: sqlite3.Connection) -> None:
    """One-time import of existing JSONL/JSON logs so history is preserved."""
    done = conn.execute("SELECT value FROM migration_meta WHERE key='legacy_import_v1'").fetchone()
    if done:
        return
    request_count = conn.execute("SELECT COUNT(*) FROM request_logs").fetchone()[0]
    if request_count == 0 and REQUEST_LOG_PATH.exists():
        for line in REQUEST_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO request_logs
                    (ts, request_id, path, status, downstream_key, request_json, response_json, fake_prompt_tools)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.get("ts") or _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        event.get("request_id") or f"legacy_{uuid.uuid4().hex}",
                        event.get("path") or "",
                        int(event.get("status") or 0),
                        event.get("downstream_key"),
                        json.dumps(event.get("request") or {}, ensure_ascii=False),
                        json.dumps(event.get("response"), ensure_ascii=False) if event.get("response") is not None else None,
                        1 if event.get("fake_prompt_tools") else 0,
                    ),
                )
            except Exception:
                continue
    failure_count = conn.execute("SELECT COUNT(*) FROM tool_failures").fetchone()[0]
    if failure_count == 0 and _failure_log_path().exists():
        for line in _failure_log_path().read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                conn.execute(
                    """
                    INSERT INTO tool_failures
                        (ts, tool_name, call_id, failure_type, arguments_keys_json, content, fake_prompt_tools, execution_ms, retry_count, provider)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.get("ts") or _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        event.get("tool_name") or event.get("tool") or "unknown",
                        event.get("call_id") or f"legacy_{uuid.uuid4().hex}",
                        event.get("failure_type"),
                        json.dumps(event.get("arguments_keys") or [], ensure_ascii=False),
                        event.get("content") or "",
                        1 if event.get("fake_prompt_tools") else 0,
                        event.get("execution_ms"),
                        event.get("retry_count") or 0,
                        event.get("provider"),
                    ),
                )
            except Exception:
                continue
    if STATS_PATH.exists():
        try:
            stats = json.loads(STATS_PATH.read_text(encoding="utf-8"))
            for name, item in (stats.get("tools") or {}).items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO tool_stats(tool_name, calls, success, failure, failures_json, last_called_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        int(item.get("calls") or 0),
                        int(item.get("success") or 0),
                        int(item.get("failure") or 0),
                        json.dumps(item.get("failures") or {}, ensure_ascii=False),
                        item.get("last_called_at"),
                    ),
                )
        except Exception:
            pass
    conn.execute("INSERT OR REPLACE INTO migration_meta(key, value) VALUES ('legacy_import_v1', ?)", (_dt.datetime.now(_dt.timezone.utc).isoformat(),))


def _failure_log_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("GATEWAY_FAILURE_LOG") or ".gateway_failures.jsonl")


def _logging_backend() -> str:
    from .gateway_config import _gateway_config
    backend = str(_gateway_config().get("logging_backend", "sqlite") or "sqlite").lower()
    if backend == "jsonl" and os.environ.get("GATEWAY_ALLOW_FILE_LOGGING", "").lower() not in {"1", "true", "yes", "on"}:
        return "sqlite"
    return backend


def _sqlite_insert_tool_failure(event: Json) -> None:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        conn.execute(
            """
            INSERT INTO tool_failures
                (ts, tool_name, call_id, failure_type, arguments_keys_json, content, fake_prompt_tools, execution_ms, retry_count, provider)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("ts") or _dt.datetime.now(_dt.timezone.utc).isoformat(),
                event.get("tool_name") or "unknown",
                event.get("call_id") or f"call_{uuid.uuid4().hex}",
                event.get("failure_type"),
                json.dumps(event.get("arguments_keys") or [], ensure_ascii=False),
                event.get("content") or "",
                1 if event.get("fake_prompt_tools") else 0,
                event.get("execution_ms"),
                event.get("retry_count") or 0,
                event.get("provider"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _sqlite_record_tool_stat(name: str, success: bool, failure_type: str | None = None) -> None:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        conn.execute(
            """
            INSERT INTO tool_stats (tool_name, calls, success, failure, failures_json, last_called_at)
            VALUES (?, 1, ?, ?, '{}', ?)
            ON CONFLICT(tool_name) DO UPDATE SET
                calls = tool_stats.calls + 1,
                success = tool_stats.success + ?,
                failure = tool_stats.failure + ?,
                last_called_at = ?
            """,
            (
                name,
                1 if success else 0,
                0 if success else 1,
                _dt.datetime.now(_dt.timezone.utc).isoformat(),
                1 if success else 0,
                0 if success else 1,
                _dt.datetime.now(_dt.timezone.utc).isoformat(),
            ),
        )
        if failure_type and not success:
            row = conn.execute("SELECT failures_json FROM tool_stats WHERE tool_name=?", (name,)).fetchone()
            if row:
                failures = json.loads(row[0])
                failures[failure_type] = failures.get(failure_type, 0) + 1
                conn.execute("UPDATE tool_stats SET failures_json=? WHERE tool_name=?", (json.dumps(failures), name))
        conn.commit()
    finally:
        conn.close()


def _sqlite_record_request_stat(path: str, status: int) -> None:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        conn.execute(
            """
            INSERT INTO request_stats (key, value) VALUES ('total', 1)
            ON CONFLICT(key) DO UPDATE SET value = request_stats.value + 1
            """
        )
        conn.execute(
            """
            INSERT INTO request_stats_by_path (path, value) VALUES (?, 1)
            ON CONFLICT(path) DO UPDATE SET value = request_stats_by_path.value + 1
            """,
            (path,),
        )
        status_key = f"{status // 100}xx"
        conn.execute(
            """
            INSERT INTO request_stats_by_status (status, value) VALUES (?, 1)
            ON CONFLICT(status) DO UPDATE SET value = request_stats_by_status.value + 1
            """,
            (status_key,),
        )
        conn.commit()
    finally:
        conn.close()


def _sqlite_insert_request_log(event: Json) -> None:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        conn.execute(
            """
            INSERT INTO request_logs
                (ts, request_id, path, status, downstream_key, request_json, response_json, fake_prompt_tools)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("ts") or _dt.datetime.now(_dt.timezone.utc).isoformat(),
                event.get("request_id") or f"req_{uuid.uuid4().hex}",
                event.get("path") or "",
                int(event.get("status") or 0),
                event.get("downstream_key"),
                json.dumps(event.get("request") or {}, ensure_ascii=False),
                json.dumps(event.get("response"), ensure_ascii=False) if event.get("response") is not None else None,
                1 if event.get("fake_prompt_tools") else 0,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _sqlite_stats_snapshot() -> Json:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        tools = {}
        for row in conn.execute("SELECT tool_name, calls, success, failure, failures_json, last_called_at FROM tool_stats"):
            tools[row[0]] = {
                "calls": row[1],
                "success": row[2],
                "failure": row[3],
                "failures": json.loads(row[4]),
                "last_called_at": row[5],
            }
        total = conn.execute("SELECT value FROM request_stats WHERE key='total'").fetchone()
        by_path = {row[0]: row[1] for row in conn.execute("SELECT path, value FROM request_stats_by_path")}
        by_status = {row[0]: row[1] for row in conn.execute("SELECT status, value FROM request_stats_by_status")}
        total_requests = total[0] if total else 0
        requests: Json = {"total": total_requests}
        requests.update(by_path)
        for status, value in by_status.items():
            status_key = str(status)
            requests[status_key if status_key.endswith("xx") else f"{int(status_key) // 100}xx"] = value
        return {
            "backend": "sqlite",
            "total_requests": total_requests,
            "requests": requests,
            "by_path": by_path,
            "by_status": by_status,
            "tools": tools,
        }
    finally:
        conn.close()


def _sqlite_tail_requests(limit: int = 50) -> list[Json]:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        rows = conn.execute(
            "SELECT ts, request_id, path, status, downstream_key, request_json, response_json, fake_prompt_tools FROM request_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts": row[0],
                "request_id": row[1],
                "path": row[2],
                "status": row[3],
                "downstream_key": row[4],
                "request": json.loads(row[5]),
                "response": json.loads(row[6]) if row[6] else None,
                "fake_prompt_tools": bool(row[7]),
            }
            for row in rows
        ]
    finally:
        conn.close()


def _sqlite_tail_failures(limit: int = 50) -> list[Json]:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        rows = conn.execute(
            "SELECT ts, tool_name, call_id, failure_type, arguments_keys_json, content, fake_prompt_tools, execution_ms, retry_count, provider FROM tool_failures ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts": row[0],
                "tool_name": row[1],
                "call_id": row[2],
                "failure_type": row[3],
                "arguments_keys": json.loads(row[4]),
                "content": row[5],
                "fake_prompt_tools": bool(row[6]),
                "execution_ms": row[7],
                "retry_count": row[8],
                "provider": row[9],
            }
            for row in rows
        ]
    finally:
        conn.close()


def _record_tool_failure(
    tool_name: str,
    call_id: str,
    failure_type: str | None,
    arguments_keys: list[str],
    content: str,
    *,
    fake_prompt_tools: bool = False,
    execution_ms: float | None = None,
    retry_count: int = 0,
    provider: str | None = None,
) -> None:
    event = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "tool_name": tool_name,
        "call_id": call_id,
        "failure_type": failure_type,
        "arguments_keys": arguments_keys,
        "content": content,
        "fake_prompt_tools": fake_prompt_tools,
        "execution_ms": execution_ms,
        "retry_count": retry_count,
        "provider": provider,
    }
    if _logging_backend() == "sqlite":
        _sqlite_insert_tool_failure(event)
    else:
        _write_jsonl_file(_failure_log_path(), event)


def _read_json_file(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_file(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl_file(path: pathlib.Path, event: Json) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _record_tool_stat(name: str, success: bool, failure_type: str | None = None) -> None:
    if _logging_backend() == "sqlite":
        _sqlite_record_tool_stat(name, success, failure_type)
    else:
        stats = _read_json_file(STATS_PATH, {"tools": {}})
        tools = stats.setdefault("tools", {})
        tool = tools.setdefault(name, {"calls": 0, "success": 0, "failure": 0, "failures": {}})
        tool["calls"] = tool.get("calls", 0) + 1
        if success:
            tool["success"] = tool.get("success", 0) + 1
        else:
            tool["failure"] = tool.get("failure", 0) + 1
            if failure_type:
                failures = tool.setdefault("failures", {})
                failures[failure_type] = failures.get(failure_type, 0) + 1
        tool["last_called_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        _write_json_file(STATS_PATH, stats)


def _record_request_stat(path: str, status: int) -> None:
    if _logging_backend() == "sqlite":
        _sqlite_record_request_stat(path, status)
    else:
        stats = _read_json_file(STATS_PATH, {"requests": {}})
        requests = stats.setdefault("requests", {})
        requests["total"] = requests.get("total", 0) + 1
        requests[path] = requests.get(path, 0) + 1
        status_key = f"{status // 100}xx"
        requests[status_key] = requests.get(status_key, 0) + 1
        _write_json_file(STATS_PATH, stats)


def _redact_payload(value: Any) -> Any:
    from .gateway_config import _redact_sensitive_values

    return _redact_sensitive_values(value)


def _max_log_payload_chars(cfg: Json) -> int:
    try:
        value = int(cfg.get("max_log_payload_chars") or 200000)
    except (TypeError, ValueError):
        value = 200000
    return max(1, value)


def _compact_json_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _truncate_log_payload(value: Any, max_chars: int) -> Any:
    if value is None:
        return None
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= max_chars:
        return value
    original_chars = len(rendered)

    def summary(preview_chars: int) -> Json:
        preview_chars = max(0, preview_chars)
        return {
            "gateway_truncated": True,
            "original_chars": original_chars,
            "max_chars": max_chars,
            "omitted_chars": max(0, original_chars - preview_chars),
            "preview": rendered[:preview_chars],
        }

    # Keep the stored truncation summary itself within the configured budget
    # whenever possible. Very small budgets still keep a machine-readable marker.
    preview_budget = max_chars
    while preview_budget > 0:
        candidate = summary(preview_budget)
        if _compact_json_len(candidate) <= max_chars:
            return candidate
        preview_budget //= 2
    minimal: Json = {
        "gateway_truncated": True,
        "original_chars": original_chars,
        "max_chars": max_chars,
        "omitted_chars": original_chars,
        "preview": "",
    }
    if _compact_json_len(minimal) <= max_chars:
        return minimal
    return {"gateway_truncated": True}


def _write_request_log(path: str, body: Json, status: int, response: Json | None, downstream_key: str | None) -> None:
    from .gateway_config import _gateway_config
    cfg = _gateway_config()
    if not cfg.get("request_logging", True):
        return
    max_payload_chars = _max_log_payload_chars(cfg)
    redacted_request = _redact_payload(body)
    redacted_response = _redact_payload(response) if response else None
    event = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "request_id": f"req_{uuid.uuid4().hex}",
        "path": path,
        "status": status,
        "downstream_key": downstream_key,
        "request": _truncate_log_payload(redacted_request, max_payload_chars),
        "response": _truncate_log_payload(redacted_response, max_payload_chars),
        "fake_prompt_tools": False,
    }
    if _logging_backend() == "sqlite":
        _sqlite_insert_request_log(event)
    else:
        _write_jsonl_file(REQUEST_LOG_PATH, event)


def _tail_jsonl(path: pathlib.Path, limit: int = 50) -> list[Json]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    result = []
    for line in reversed(lines):
        if len(result) >= limit:
            break
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                result.append(event)
        except Exception:
            continue
    return list(reversed(result))


def _stats_snapshot() -> Json:
    if _logging_backend() == "sqlite":
        return _sqlite_stats_snapshot()
    return _read_json_file(STATS_PATH, {})


def _tail_requests(limit: int = 50) -> list[Json]:
    if _logging_backend() == "sqlite":
        return _sqlite_tail_requests(limit)
    return _tail_jsonl(REQUEST_LOG_PATH, limit)


def _tail_failures(limit: int = 50) -> list[Json]:
    if _logging_backend() == "sqlite":
        return _sqlite_tail_failures(limit)
    return _tail_jsonl(_failure_log_path(), limit)


def _tool_catalog_snapshot() -> Json:
    from .gateway_builtin_tools import BUILTIN_TOOLS
    tools = []
    seen: set[str] = set()
    for name, tool in BUILTIN_TOOLS.items():
        if name in seen:
            continue
        seen.add(name)
        tools.append({
            "name": name,
            "canonical_name": tool.name,
            "description": tool.description,
            "risk": tool.risk,
            "parameters": tool.parameters,
        })
    unsupported = []
    for row in _tail_failures(200):
        tool_name = row.get("tool_name") or row.get("tool")
        if tool_name:
            unsupported.append({"tool": tool_name, **row})
    return {"tools": tools, "unsupported_or_failed": unsupported}
