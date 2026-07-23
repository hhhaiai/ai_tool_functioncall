#!/usr/bin/env python3
"""Persistent Gateway-owned OpenAI Assistants/Threads compatibility service.

The configured upstream may expose only chat completions.  This module owns the
stateful Assistants surface locally and executes runs through the Gateway's
canonical chat/tool orchestration path.  No upstream Assistants implementation
is required.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .gateway_errors import BadRequestError, GatewayError
from .gateway_sqlite import (
    path_is_within,
    secure_sqlite_artifacts,
    secure_sqlite_connect,
    set_secure_sqlite_journal_mode,
    sqlite_initialization_lock,
)

Json = dict[str, Any]
RunExecutor = Callable[[Json], Json]

_STORE_LOCK = threading.RLock()
_STORE: "AssistantStore | None" = None
_STORE_PATH = ""


class AssistantAPIError(GatewayError):
    """HTTP-aware error raised by the Gateway-owned Assistants service."""

    def __init__(self, message: str, *, status: int, detail: Any | None = None) -> None:
        super().__init__(message, detail=detail)
        self.status = int(status)


@dataclass(frozen=True)
class AssistantRouteResult:
    status: int
    payload: Json
    resource: str
    action: str


def _now() -> int:
    return int(time.time())


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _metadata(value: Any) -> Json:
    return dict(value) if isinstance(value, dict) else {}


def _json_object(value: str | bytes | None, *, default: Json | None = None) -> Json:
    if value is None or value == "" or value == b"":
        return dict(default or {})
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return dict(default or {})
    return decoded if isinstance(decoded, dict) else dict(default or {})


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _assistant_db_path() -> pathlib.Path:
    from .gateway_config import load_config

    cfg = load_config()
    raw = cfg.get("assistants") if isinstance(cfg.get("assistants"), dict) else {}
    configured = str(raw.get("db_path") or os.environ.get("GATEWAY_ASSISTANTS_DB_PATH") or "").strip()
    runtime = pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR") or ".gateway_runtime").expanduser()
    return pathlib.Path(configured).expanduser() if configured else runtime / "assistants.sqlite3"


def _tenant_key(client_id: str | None) -> str:
    identity = str(client_id or "local").strip() or "local"
    digest = hashlib.sha256(identity.encode("utf-8", "ignore")).hexdigest()
    return f"tenant_{digest[:32]}"


def _limit(value: Any, *, default: int = 20, maximum: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(1, parsed))


def _order(value: Any) -> str:
    return "asc" if str(value or "desc").lower() == "asc" else "desc"


def _list_payload(items: list[Json], *, has_more: bool) -> Json:
    return {
        "object": "list",
        "data": items,
        "first_id": items[0].get("id") if items else None,
        "last_id": items[-1].get("id") if items else None,
        "has_more": bool(has_more),
    }


def _require_id(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BadRequestError(f"missing required field: {name}")
    return text


def _normalize_message_content(value: Any) -> list[Json]:
    if isinstance(value, str):
        return [{"type": "text", "text": {"value": value, "annotations": []}}]
    if not isinstance(value, list) or not value:
        raise BadRequestError("message content must be a non-empty string or list")
    normalized: list[Json] = []
    for block in value:
        if not isinstance(block, dict):
            raise BadRequestError("message content blocks must be objects")
        item = dict(block)
        block_type = str(item.get("type") or "text")
        if block_type == "text":
            text = item.get("text")
            if isinstance(text, str):
                item["text"] = {"value": text, "annotations": []}
            elif isinstance(text, dict):
                text_obj = dict(text)
                text_obj["value"] = str(text_obj.get("value") or "")
                text_obj["annotations"] = text_obj.get("annotations") if isinstance(text_obj.get("annotations"), list) else []
                item["text"] = text_obj
            else:
                raise BadRequestError("text content block requires text")
        item["type"] = block_type
        normalized.append(item)
    return normalized


def _message_text(message: Json) -> str:
    parts: list[str] = []
    content = message.get("content")
    if isinstance(content, str):
        return content
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
        elif isinstance(text, dict):
            parts.append(str(text.get("value") or ""))
    return "\n".join(part for part in parts if part)


class AssistantStore:
    """Small multi-process-safe SQLite repository for Assistants resources."""

    def __init__(self, path: pathlib.Path | str) -> None:
        self.path = pathlib.Path(path).expanduser().absolute()
        self._initialized = False
        self._init_lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        runtime = pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR") or ".gateway_runtime").expanduser().absolute()
        connection = secure_sqlite_connect(
            self.path,
            private_parent=path_is_within(self.path, runtime),
            timeout=5.0,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with sqlite_initialization_lock(self.path):
                connection = self._connect()
                try:
                    connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
                    set_secure_sqlite_journal_mode(connection, self.path, "WAL")
                    connection.execute("PRAGMA synchronous=NORMAL")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS assistants (
                            tenant_key TEXT NOT NULL,
                            resource_id TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            payload_json TEXT NOT NULL,
                            PRIMARY KEY (tenant_key, resource_id)
                        );
                        CREATE INDEX IF NOT EXISTS idx_assistants_list
                            ON assistants(tenant_key, created_at, resource_id);

                        CREATE TABLE IF NOT EXISTS threads (
                            tenant_key TEXT NOT NULL,
                            resource_id TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            payload_json TEXT NOT NULL,
                            PRIMARY KEY (tenant_key, resource_id)
                        );

                        CREATE TABLE IF NOT EXISTS messages (
                            tenant_key TEXT NOT NULL,
                            thread_id TEXT NOT NULL,
                            resource_id TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            payload_json TEXT NOT NULL,
                            PRIMARY KEY (tenant_key, resource_id),
                            FOREIGN KEY (tenant_key, thread_id)
                                REFERENCES threads(tenant_key, resource_id) ON DELETE CASCADE
                        );
                        CREATE INDEX IF NOT EXISTS idx_messages_list
                            ON messages(tenant_key, thread_id, created_at, resource_id);

                        CREATE TABLE IF NOT EXISTS runs (
                            tenant_key TEXT NOT NULL,
                            thread_id TEXT NOT NULL,
                            resource_id TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            status TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            state_json TEXT NOT NULL,
                            PRIMARY KEY (tenant_key, resource_id),
                            FOREIGN KEY (tenant_key, thread_id)
                                REFERENCES threads(tenant_key, resource_id) ON DELETE CASCADE
                        );
                        CREATE INDEX IF NOT EXISTS idx_runs_list
                            ON runs(tenant_key, thread_id, created_at, resource_id);

                        CREATE TABLE IF NOT EXISTS run_steps (
                            tenant_key TEXT NOT NULL,
                            thread_id TEXT NOT NULL,
                            run_id TEXT NOT NULL,
                            resource_id TEXT NOT NULL,
                            created_at INTEGER NOT NULL,
                            updated_at INTEGER NOT NULL,
                            status TEXT NOT NULL,
                            payload_json TEXT NOT NULL,
                            PRIMARY KEY (tenant_key, resource_id),
                            FOREIGN KEY (tenant_key, run_id)
                                REFERENCES runs(tenant_key, resource_id) ON DELETE CASCADE
                        );
                        CREATE INDEX IF NOT EXISTS idx_run_steps_list
                            ON run_steps(tenant_key, thread_id, run_id, created_at, resource_id);
                        """
                    )
                    connection.commit()
                    secure_sqlite_artifacts(self.path)
                    self._initialized = True
                finally:
                    connection.close()

    def _connection(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect()

    def put_resource(self, table: str, tenant: str, payload: Json) -> None:
        if table not in {"assistants", "threads"}:
            raise ValueError("unsupported resource table")
        now = _now()
        created = int(payload.get("created_at") or now)
        with self._connection() as connection:
            connection.execute(
                f"""
                INSERT INTO {table}(tenant_key, resource_id, created_at, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant_key, resource_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (tenant, str(payload["id"]), created, now, _json_dump(payload)),
            )

    def get_resource(self, table: str, tenant: str, resource_id: str) -> Json | None:
        if table not in {"assistants", "threads"}:
            raise ValueError("unsupported resource table")
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT payload_json FROM {table} WHERE tenant_key=? AND resource_id=?",
                (tenant, resource_id),
            ).fetchone()
        return _json_object(row["payload_json"]) if row else None

    def delete_resource(self, table: str, tenant: str, resource_id: str) -> bool:
        if table not in {"assistants", "threads"}:
            raise ValueError("unsupported resource table")
        with self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE tenant_key=? AND resource_id=?",
                (tenant, resource_id),
            )
        return bool(cursor.rowcount)

    def list_resources(self, table: str, tenant: str, query: Json) -> Json:
        if table not in {"assistants", "threads"}:
            raise ValueError("unsupported resource table")
        return self._list_table(table, tenant, query)

    def put_message(self, tenant: str, payload: Json) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO messages(tenant_key, thread_id, resource_id, created_at, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_key, resource_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (
                    tenant,
                    str(payload["thread_id"]),
                    str(payload["id"]),
                    int(payload.get("created_at") or now),
                    now,
                    _json_dump(payload),
                ),
            )

    def get_message(self, tenant: str, thread_id: str, message_id: str) -> Json | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM messages WHERE tenant_key=? AND thread_id=? AND resource_id=?",
                (tenant, thread_id, message_id),
            ).fetchone()
        return _json_object(row["payload_json"]) if row else None

    def delete_message(self, tenant: str, thread_id: str, message_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM messages WHERE tenant_key=? AND thread_id=? AND resource_id=?",
                (tenant, thread_id, message_id),
            )
        return bool(cursor.rowcount)

    def list_messages(self, tenant: str, thread_id: str, query: Json) -> Json:
        return self._list_table("messages", tenant, query, thread_id=thread_id)

    def create_run(self, tenant: str, payload: Json, state: Json) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    tenant_key, thread_id, resource_id, created_at, updated_at,
                    status, payload_json, state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tenant,
                    str(payload["thread_id"]),
                    str(payload["id"]),
                    int(payload.get("created_at") or now),
                    now,
                    str(payload["status"]),
                    _json_dump(payload),
                    _json_dump(state),
                ),
            )

    def get_run(self, tenant: str, thread_id: str, run_id: str) -> tuple[Json, Json] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json, state_json FROM runs
                WHERE tenant_key=? AND thread_id=? AND resource_id=?
                """,
                (tenant, thread_id, run_id),
            ).fetchone()
        if not row:
            return None
        return _json_object(row["payload_json"]), _json_object(row["state_json"])

    def list_runs(self, tenant: str, thread_id: str, query: Json) -> Json:
        return self._list_table("runs", tenant, query, thread_id=thread_id)

    def save_run_if_status(
        self,
        tenant: str,
        payload: Json,
        state: Json,
        *,
        expected: set[str],
        step: Json | None = None,
        message: Json | None = None,
    ) -> Json:
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, payload_json FROM runs WHERE tenant_key=? AND thread_id=? AND resource_id=?",
                (tenant, str(payload["thread_id"]), str(payload["id"])),
            ).fetchone()
            if not row:
                raise _not_found("run", str(payload["id"]))
            if str(row["status"]) not in expected:
                connection.rollback()
                return _json_object(row["payload_json"])
            connection.execute(
                """
                UPDATE runs SET updated_at=?, status=?, payload_json=?, state_json=?
                WHERE tenant_key=? AND thread_id=? AND resource_id=?
                """,
                (
                    now,
                    str(payload["status"]),
                    _json_dump(payload),
                    _json_dump(state),
                    tenant,
                    str(payload["thread_id"]),
                    str(payload["id"]),
                ),
            )
            if message is not None:
                connection.execute(
                    """
                    INSERT INTO messages(tenant_key, thread_id, resource_id, created_at, updated_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant,
                        str(message["thread_id"]),
                        str(message["id"]),
                        int(message["created_at"]),
                        now,
                        _json_dump(message),
                    ),
                )
            if step is not None:
                connection.execute(
                    """
                    INSERT INTO run_steps(
                        tenant_key, thread_id, run_id, resource_id, created_at,
                        updated_at, status, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant,
                        str(step["thread_id"]),
                        str(step["run_id"]),
                        str(step["id"]),
                        int(step["created_at"]),
                        now,
                        str(step["status"]),
                        _json_dump(step),
                    ),
                )
            connection.commit()
        return payload

    def transition_run(
        self,
        tenant: str,
        thread_id: str,
        run_id: str,
        *,
        expected: set[str],
        mutate: Callable[[Json, Json], tuple[Json, Json]],
    ) -> tuple[Json, Json]:
        now = _now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, payload_json, state_json FROM runs
                WHERE tenant_key=? AND thread_id=? AND resource_id=?
                """,
                (tenant, thread_id, run_id),
            ).fetchone()
            if not row:
                raise _not_found("run", run_id)
            run = _json_object(row["payload_json"])
            state = _json_object(row["state_json"])
            if str(row["status"]) not in expected:
                connection.rollback()
                raise AssistantAPIError(
                    f"run {run_id} cannot transition from {row['status']}",
                    status=409,
                    detail={"run_id": run_id, "status": row["status"]},
                )
            run, state = mutate(run, state)
            connection.execute(
                """
                UPDATE runs SET updated_at=?, status=?, payload_json=?, state_json=?
                WHERE tenant_key=? AND thread_id=? AND resource_id=?
                """,
                (now, str(run["status"]), _json_dump(run), _json_dump(state), tenant, thread_id, run_id),
            )
            connection.commit()
        return run, state

    def put_step(self, tenant: str, step: Json) -> None:
        now = _now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO run_steps(
                    tenant_key, thread_id, run_id, resource_id, created_at,
                    updated_at, status, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_key, resource_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    payload_json=excluded.payload_json
                """,
                (
                    tenant,
                    str(step["thread_id"]),
                    str(step["run_id"]),
                    str(step["id"]),
                    int(step["created_at"]),
                    now,
                    str(step["status"]),
                    _json_dump(step),
                ),
            )

    def get_step(self, tenant: str, thread_id: str, run_id: str, step_id: str) -> Json | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM run_steps
                WHERE tenant_key=? AND thread_id=? AND run_id=? AND resource_id=?
                """,
                (tenant, thread_id, run_id, step_id),
            ).fetchone()
        return _json_object(row["payload_json"]) if row else None

    def list_steps(self, tenant: str, thread_id: str, run_id: str, query: Json) -> Json:
        return self._list_table("run_steps", tenant, query, thread_id=thread_id, run_id=run_id)

    def complete_pending_tool_step(self, tenant: str, thread_id: str, run_id: str, outputs: list[Json]) -> None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT resource_id, payload_json FROM run_steps
                WHERE tenant_key=? AND thread_id=? AND run_id=? AND status='in_progress'
                ORDER BY created_at DESC, resource_id DESC LIMIT 1
                """,
                (tenant, thread_id, run_id),
            ).fetchone()
            if not row:
                return
            step = _json_object(row["payload_json"])
            raw_details = step.get("step_details")
            details: Json = raw_details if isinstance(raw_details, dict) else {}
            raw_calls = details.get("tool_calls")
            calls: list[Any] = raw_calls if isinstance(raw_calls, list) else []
            output_map = {str(item.get("tool_call_id") or ""): str(item.get("output") or "") for item in outputs}
            for call in calls:
                if isinstance(call, dict) and str(call.get("id") or "") in output_map:
                    call["output"] = output_map[str(call.get("id"))]
            step["status"] = "completed"
            step["completed_at"] = _now()
            step["step_details"] = details
            connection.execute(
                """
                UPDATE run_steps SET updated_at=?, status='completed', payload_json=?
                WHERE tenant_key=? AND resource_id=?
                """,
                (_now(), _json_dump(step), tenant, str(row["resource_id"])),
            )

    def cancel_steps(self, tenant: str, thread_id: str, run_id: str) -> None:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT resource_id, payload_json FROM run_steps
                WHERE tenant_key=? AND thread_id=? AND run_id=? AND status='in_progress'
                """,
                (tenant, thread_id, run_id),
            ).fetchall()
            for row in rows:
                step = _json_object(row["payload_json"])
                step["status"] = "cancelled"
                step["cancelled_at"] = _now()
                connection.execute(
                    """
                    UPDATE run_steps SET updated_at=?, status='cancelled', payload_json=?
                    WHERE tenant_key=? AND resource_id=?
                    """,
                    (_now(), _json_dump(step), tenant, str(row["resource_id"])),
                )

    def _list_table(
        self,
        table: str,
        tenant: str,
        query: Json,
        *,
        thread_id: str | None = None,
        run_id: str | None = None,
    ) -> Json:
        allowed = {"assistants", "threads", "messages", "runs", "run_steps"}
        if table not in allowed:
            raise ValueError("unsupported list table")
        requested = _limit(query.get("limit"))
        ordering = _order(query.get("order"))
        clauses = ["tenant_key=?"]
        params: list[Any] = [tenant]
        if thread_id is not None:
            clauses.append("thread_id=?")
            params.append(thread_id)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        after = str(query.get("after") or "").strip()
        before = str(query.get("before") or "").strip()
        cursor_id = after or before
        if cursor_id:
            with self._connection() as connection:
                cursor = connection.execute(
                    f"SELECT created_at, rowid AS sequence FROM {table} WHERE tenant_key=? AND resource_id=?",
                    (tenant, cursor_id),
                ).fetchone()
            if cursor:
                op = "<" if (after and ordering == "desc") or (before and ordering == "asc") else ">"
                clauses.append(f"(created_at, rowid) {op} (?, ?)")
                params.extend([int(cursor["created_at"]), int(cursor["sequence"])])
        sql = (
            f"SELECT payload_json FROM {table} WHERE {' AND '.join(clauses)} "
            f"ORDER BY created_at {ordering.upper()}, rowid {ordering.upper()} LIMIT ?"
        )
        params.append(requested + 1)
        with self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        items = [_json_object(row["payload_json"]) for row in rows[:requested]]
        return _list_payload(items, has_more=len(rows) > requested)

    def cleanup(
        self,
        *,
        retention_days: int = 30,
        max_rows: int = 50_000,
        batch_size: int = 1_000,
        max_batches: int = 4,
        incremental_vacuum_pages: int = 256,
        dry_run: bool = False,
        now: float | None = None,
    ) -> Json:
        """Bound age and row count for every Assistants resource table."""
        cutoff = int(float(now if now is not None else time.time()) - max(0, int(retention_days)) * 86400)
        row_limit = max(1, int(max_rows))
        bounded_batch = max(1, min(int(batch_size), 100_000))
        bounded_batches = max(1, min(int(max_batches), 100))
        tables = ("run_steps", "runs", "messages", "threads", "assistants")
        eligible: Json = {}
        deleted: Json = {table: 0 for table in tables}
        with self._connection() as connection:
            for table in tables:
                total = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                old = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE updated_at < ?",
                        (cutoff,),
                    ).fetchone()[0]
                )
                eligible[table] = max(old, max(0, total - row_limit))
                if dry_run:
                    continue
                for _ in range(bounded_batches):
                    current = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    excess = max(0, current - row_limit)
                    cursor = connection.execute(
                        f"""
                        DELETE FROM {table} WHERE rowid IN (
                            SELECT rowid FROM {table}
                            WHERE updated_at < ? OR ? > 0
                            ORDER BY CASE WHEN updated_at < ? THEN 0 ELSE 1 END,
                                     updated_at ASC, rowid ASC
                            LIMIT ?
                        )
                        """,
                        (cutoff, excess, cutoff, min(bounded_batch, max(old, excess))),
                    )
                    changed = max(0, int(cursor.rowcount or 0))
                    deleted[table] = int(deleted[table]) + changed
                    connection.commit()
                    if changed < bounded_batch:
                        break
            if not dry_run:
                connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                if incremental_vacuum_pages > 0:
                    connection.execute(f"PRAGMA incremental_vacuum({max(0, int(incremental_vacuum_pages))})")
        secure_sqlite_artifacts(self.path)
        with self._connection() as connection:
            rows = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
        return {
            "dry_run": bool(dry_run),
            "eligible": eligible,
            "deleted": deleted,
            "rows": rows,
            "space_bytes": sum(
                candidate.stat().st_size
                for candidate in (self.path, pathlib.Path(f"{self.path}-wal"), pathlib.Path(f"{self.path}-shm"))
                if candidate.exists()
            ),
        }


def get_assistant_store() -> AssistantStore:
    global _STORE, _STORE_PATH
    path = _assistant_db_path()
    key = str(path.absolute())
    with _STORE_LOCK:
        if _STORE is None or _STORE_PATH != key:
            _STORE = AssistantStore(path)
            _STORE_PATH = key
        _STORE.initialize()
        return _STORE


def reset_assistant_store() -> None:
    """Reset the process-local store handle; intended for config reload/tests."""
    global _STORE, _STORE_PATH
    with _STORE_LOCK:
        _STORE = None
        _STORE_PATH = ""


def _not_found(resource: str, resource_id: str) -> AssistantAPIError:
    return AssistantAPIError(
        f"{resource} not found: {resource_id}",
        status=404,
        detail={"resource": resource, "id": resource_id},
    )


def create_assistant_response(body: Json, *, client_id: str | None = None) -> Json:
    """Create and persist an OpenAI-compatible Assistant object."""
    from .gateway_config import _upstream_config

    upstream_model = str(_upstream_config().get("model") or "")
    model = str(body.get("model") or upstream_model or "gateway-default").strip()
    if not model:
        raise BadRequestError("assistant model must not be empty")
    response: Json = {
        "id": _id("asst"),
        "object": "assistant",
        "created_at": _now(),
        "name": body.get("name"),
        "description": body.get("description"),
        "model": model,
        "instructions": body.get("instructions"),
        "tools": body.get("tools") if isinstance(body.get("tools"), list) else [],
        "metadata": _metadata(body.get("metadata")),
        "response_format": body.get("response_format", "auto"),
    }
    if "temperature" in body:
        response["temperature"] = body.get("temperature")
    if "top_p" in body:
        response["top_p"] = body.get("top_p")
    if "tool_resources" in body:
        response["tool_resources"] = _metadata(body.get("tool_resources"))
    get_assistant_store().put_resource("assistants", _tenant_key(client_id), response)
    return response


def create_thread_response(body: Json, *, client_id: str | None = None) -> Json:
    """Create and persist a Thread plus any initial messages."""
    tenant = _tenant_key(client_id)
    thread: Json = {
        "id": _id("thread"),
        "object": "thread",
        "created_at": _now(),
        "metadata": _metadata(body.get("metadata")),
        "tool_resources": _metadata(body.get("tool_resources")),
    }
    store = get_assistant_store()
    store.put_resource("threads", tenant, thread)
    messages = body.get("messages")
    if messages is not None and not isinstance(messages, list):
        store.delete_resource("threads", tenant, str(thread["id"]))
        raise BadRequestError("thread messages must be a list")
    try:
        for item in messages or []:
            if not isinstance(item, dict):
                raise BadRequestError("thread messages must contain objects")
            create_message(str(thread["id"]), item, client_id=client_id)
    except Exception:
        store.delete_resource("threads", tenant, str(thread["id"]))
        raise
    if isinstance(messages, list):
        # Retain the historical non-content debug count while persisting the
        # actual messages behind the authenticated thread resource.
        thread["gateway_message_count"] = len(messages)
        store.put_resource("threads", tenant, thread)
    return thread


def create_message(thread_id: str, body: Json, *, client_id: str | None = None, run_id: str | None = None) -> Json:
    tenant = _tenant_key(client_id)
    store = get_assistant_store()
    if store.get_resource("threads", tenant, thread_id) is None:
        raise _not_found("thread", thread_id)
    role = str(body.get("role") or "user").strip().lower()
    if role not in {"user", "assistant"}:
        raise BadRequestError("message role must be user or assistant")
    message: Json = {
        "id": _id("msg"),
        "object": "thread.message",
        "created_at": _now(),
        "assistant_id": body.get("assistant_id"),
        "thread_id": thread_id,
        "run_id": run_id or body.get("run_id"),
        "role": role,
        "content": _normalize_message_content(body.get("content")),
        "attachments": body.get("attachments") if isinstance(body.get("attachments"), list) else [],
        "metadata": _metadata(body.get("metadata")),
        "status": "completed",
        "completed_at": _now(),
        "incomplete_at": None,
        "incomplete_details": None,
    }
    store.put_message(tenant, message)
    return message


def _extract_chat_message(response: Json) -> Json:
    raw_choices = response.get("choices")
    choices: list[Any] = raw_choices if isinstance(raw_choices, list) else []
    if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict):
        return dict(choices[0]["message"])
    raw_output = response.get("output")
    output: list[Any] = raw_output if isinstance(raw_output, list) else []
    text_parts: list[str] = []
    tool_calls: list[Json] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"function_call", "custom_tool_call"}:
            tool_calls.append({
                "id": item.get("call_id") or item.get("id") or _id("call"),
                "type": "function",
                "function": {
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or item.get("input") or "{}",
                },
            })
        raw_content = item.get("content")
        content: list[Any] = raw_content if isinstance(raw_content, list) else []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                text_parts.append(str(block.get("text") or ""))
    message: Json = {"role": "assistant", "content": "\n".join(filter(None, text_parts))}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _tool_calls(message: Json) -> list[Json]:
    raw_value = message.get("tool_calls")
    raw: list[Any] = raw_value if isinstance(raw_value, list) else []
    calls: list[Json] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_function = item.get("function")
        function: Json = raw_function if isinstance(raw_function, dict) else {}
        calls.append({
            "id": str(item.get("id") or _id("call")),
            "type": "function",
            "function": {
                "name": str(function.get("name") or ""),
                "arguments": function.get("arguments") if isinstance(function.get("arguments"), str) else _json_dump(function.get("arguments") or {}),
            },
        })
    function_call = message.get("function_call") if isinstance(message.get("function_call"), dict) else None
    if function_call:
        calls.append({
            "id": _id("call"),
            "type": "function",
            "function": {
                "name": str(function_call.get("name") or ""),
                "arguments": function_call.get("arguments") if isinstance(function_call.get("arguments"), str) else _json_dump(function_call.get("arguments") or {}),
            },
        })
    return calls


def _run_step(run: Json, details: Json, *, status: str) -> Json:
    now = _now()
    step: Json = {
        "id": _id("step"),
        "object": "thread.run.step",
        "created_at": now,
        "assistant_id": run["assistant_id"],
        "thread_id": run["thread_id"],
        "run_id": run["id"],
        "type": str(details.get("type") or "message_creation"),
        "status": status,
        "step_details": details,
        "last_error": None,
        "expired_at": None,
        "cancelled_at": None,
        "failed_at": None,
        "completed_at": now if status == "completed" else None,
        "metadata": {},
        "usage": None,
    }
    return step


def _run_request(run: Json, assistant: Json, messages: list[Json]) -> Json:
    instructions = run.get("instructions")
    if instructions is None:
        instructions = assistant.get("instructions")
    chat_messages: list[Json] = []
    if instructions:
        chat_messages.append({"role": "system", "content": str(instructions)})
    chat_messages.extend(messages)
    additional = str(run.get("additional_instructions") or "").strip()
    if additional:
        chat_messages.append({"role": "system", "content": additional})
    request: Json = {
        "model": run.get("model") or assistant.get("model") or "gateway-default",
        "messages": chat_messages,
        "stream": False,
        "metadata": {
            "session_id": str(run["thread_id"]),
            "assistant_id": str(run["assistant_id"]),
            "run_id": str(run["id"]),
        },
    }
    tools = run.get("tools") if isinstance(run.get("tools"), list) else assistant.get("tools")
    if isinstance(tools, list) and tools:
        request["tools"] = tools
    for key in ("temperature", "top_p", "max_prompt_tokens", "max_completion_tokens", "response_format", "tool_choice", "parallel_tool_calls"):
        value = run.get(key)
        if value is not None:
            request[key] = value
    return request


def _thread_chat_messages(store: AssistantStore, tenant: str, thread_id: str) -> list[Json]:
    listed = store.list_messages(tenant, thread_id, {"limit": 100, "order": "asc"})
    messages: list[Json] = []
    for item in listed.get("data") or []:
        if not isinstance(item, dict):
            continue
        text = _message_text(item)
        if text:
            messages.append({"role": str(item.get("role") or "user"), "content": text})
    return messages


def _default_run_executor(request: Json, *, client_id: str | None = None) -> Json:
    from .gateway_tool_runtime import run_tool_orchestration

    return run_tool_orchestration("/v1/chat/completions", request, client_id=client_id)


def _finalize_run_response(
    store: AssistantStore,
    tenant: str,
    run: Json,
    state: Json,
    response: Json,
) -> Json:
    message = _extract_chat_message(response)
    calls = _tool_calls(message)
    run["usage"] = response.get("usage") if isinstance(response.get("usage"), dict) else None
    if calls:
        run["status"] = "requires_action"
        run["required_action"] = {
            "type": "submit_tool_outputs",
            "submit_tool_outputs": {"tool_calls": calls},
        }
        run["expires_at"] = _now() + 600
        state["pending_assistant_message"] = message
        step = _run_step(
            run,
            {"type": "tool_calls", "tool_calls": calls},
            status="in_progress",
        )
        return store.save_run_if_status(tenant, run, state, expected={"in_progress"}, step=step)

    text = str(message.get("content") or "")
    assistant_message: Json = {
        "id": _id("msg"),
        "object": "thread.message",
        "created_at": _now(),
        "assistant_id": run["assistant_id"],
        "thread_id": run["thread_id"],
        "run_id": run["id"],
        "role": "assistant",
        "content": _normalize_message_content(text),
        "attachments": [],
        "metadata": {},
        "status": "completed",
        "completed_at": _now(),
        "incomplete_at": None,
        "incomplete_details": None,
    }
    run["status"] = "completed"
    run["completed_at"] = _now()
    run["required_action"] = None
    run["expires_at"] = None
    state.pop("pending_assistant_message", None)
    step = _run_step(
        run,
        {"type": "message_creation", "message_creation": {"message_id": assistant_message["id"]}},
        status="completed",
    )
    return store.save_run_if_status(
        tenant,
        run,
        state,
        expected={"in_progress"},
        step=step,
        message=assistant_message,
    )


def _execute_run(
    store: AssistantStore,
    tenant: str,
    run: Json,
    state: Json,
    *,
    client_id: str | None,
    executor: RunExecutor | None,
) -> Json:
    assistant = store.get_resource("assistants", tenant, str(run["assistant_id"]))
    if assistant is None:
        raise _not_found("assistant", str(run["assistant_id"]))
    messages = state.get("chat_messages") if isinstance(state.get("chat_messages"), list) else None
    if messages is None:
        messages = _thread_chat_messages(store, tenant, str(run["thread_id"]))
        state["chat_messages"] = messages
    request = _run_request(run, assistant, list(messages))
    try:
        response = executor(request) if executor is not None else _default_run_executor(request, client_id=client_id)
        if not isinstance(response, dict):
            raise RuntimeError("run executor returned a non-object response")
        return _finalize_run_response(store, tenant, run, state, response)
    except Exception as exc:
        run["status"] = "failed"
        run["failed_at"] = _now()
        run["last_error"] = {
            "code": "gateway_run_failed",
            "message": str(exc),
        }
        run["required_action"] = None
        run["expires_at"] = None
        return store.save_run_if_status(tenant, run, state, expected={"in_progress"})


def create_run(
    thread_id: str,
    body: Json,
    *,
    client_id: str | None = None,
    executor: RunExecutor | None = None,
) -> Json:
    tenant = _tenant_key(client_id)
    store = get_assistant_store()
    if store.get_resource("threads", tenant, thread_id) is None:
        raise _not_found("thread", thread_id)
    assistant_id = _require_id(body.get("assistant_id"), "assistant_id")
    assistant = store.get_resource("assistants", tenant, assistant_id)
    if assistant is None:
        raise _not_found("assistant", assistant_id)
    additional_messages = body.get("additional_messages")
    if additional_messages is not None:
        if not isinstance(additional_messages, list):
            raise BadRequestError("additional_messages must be a list")
        for item in additional_messages:
            if not isinstance(item, dict):
                raise BadRequestError("additional_messages must contain objects")
            create_message(thread_id, item, client_id=client_id)
    now = _now()
    run: Json = {
        "id": _id("run"),
        "object": "thread.run",
        "created_at": now,
        "thread_id": thread_id,
        "assistant_id": assistant_id,
        "status": "queued",
        "required_action": None,
        "last_error": None,
        "expires_at": None,
        "started_at": None,
        "cancelled_at": None,
        "failed_at": None,
        "completed_at": None,
        "incomplete_details": None,
        "model": body.get("model") or assistant.get("model"),
        "instructions": body.get("instructions") if "instructions" in body else assistant.get("instructions"),
        "tools": body.get("tools") if isinstance(body.get("tools"), list) else assistant.get("tools", []),
        "metadata": _metadata(body.get("metadata")),
        "usage": None,
        "temperature": body.get("temperature", assistant.get("temperature")),
        "top_p": body.get("top_p", assistant.get("top_p")),
        "max_prompt_tokens": body.get("max_prompt_tokens"),
        "max_completion_tokens": body.get("max_completion_tokens"),
        "truncation_strategy": body.get("truncation_strategy", {"type": "auto", "last_messages": None}),
        "tool_choice": body.get("tool_choice", "auto"),
        "parallel_tool_calls": body.get("parallel_tool_calls", True),
        "response_format": body.get("response_format", assistant.get("response_format", "auto")),
        "additional_instructions": body.get("additional_instructions"),
    }
    state: Json = {}
    store.create_run(tenant, run, state)

    def start(current: Json, current_state: Json) -> tuple[Json, Json]:
        current["status"] = "in_progress"
        current["started_at"] = _now()
        return current, current_state

    run, state = store.transition_run(
        tenant,
        thread_id,
        str(run["id"]),
        expected={"queued"},
        mutate=start,
    )
    return _execute_run(store, tenant, run, state, client_id=client_id, executor=executor)


def submit_tool_outputs(
    thread_id: str,
    run_id: str,
    body: Json,
    *,
    client_id: str | None = None,
    executor: RunExecutor | None = None,
) -> Json:
    raw_outputs = body.get("tool_outputs")
    if not isinstance(raw_outputs, list) or not raw_outputs:
        raise BadRequestError("tool_outputs must be a non-empty list")
    outputs: list[Json] = []
    for item in raw_outputs:
        if not isinstance(item, dict):
            raise BadRequestError("tool_outputs must contain objects")
        outputs.append({
            "tool_call_id": _require_id(item.get("tool_call_id"), "tool_call_id"),
            "output": str(item.get("output") or ""),
        })
    tenant = _tenant_key(client_id)
    store = get_assistant_store()

    def resume(run: Json, state: Json) -> tuple[Json, Json]:
        raw_required = run.get("required_action")
        required: Json = raw_required if isinstance(raw_required, dict) else {}
        raw_submit = required.get("submit_tool_outputs")
        submit: Json = raw_submit if isinstance(raw_submit, dict) else {}
        raw_calls = submit.get("tool_calls")
        calls: list[Any] = raw_calls if isinstance(raw_calls, list) else []
        expected_ids = {str(item.get("id") or "") for item in calls if isinstance(item, dict)}
        received_ids = {str(item.get("tool_call_id") or "") for item in outputs}
        if expected_ids != received_ids:
            raise BadRequestError(
                "tool_outputs must include every required tool_call_id exactly once",
                detail={"expected": sorted(expected_ids), "received": sorted(received_ids)},
            )
        raw_messages = state.get("chat_messages")
        messages: list[Any] = raw_messages if isinstance(raw_messages, list) else []
        pending = state.get("pending_assistant_message")
        if not isinstance(pending, dict):
            raise AssistantAPIError("run is missing pending tool-call state", status=409)
        messages = messages + [pending]
        for output in outputs:
            messages.append({
                "role": "tool",
                "tool_call_id": output["tool_call_id"],
                "content": output["output"],
            })
        state["chat_messages"] = messages
        state.pop("pending_assistant_message", None)
        run["status"] = "in_progress"
        run["required_action"] = None
        run["expires_at"] = None
        return run, state

    run, state = store.transition_run(
        tenant,
        thread_id,
        run_id,
        expected={"requires_action"},
        mutate=resume,
    )
    store.complete_pending_tool_step(tenant, thread_id, run_id, outputs)
    return _execute_run(store, tenant, run, state, client_id=client_id, executor=executor)


def cancel_run(thread_id: str, run_id: str, *, client_id: str | None = None) -> Json:
    tenant = _tenant_key(client_id)
    store = get_assistant_store()

    def cancel(run: Json, state: Json) -> tuple[Json, Json]:
        run["status"] = "cancelled"
        run["cancelled_at"] = _now()
        run["required_action"] = None
        run["expires_at"] = None
        return run, state

    run, _state = store.transition_run(
        tenant,
        thread_id,
        run_id,
        expected={"queued", "in_progress", "requires_action", "cancelling"},
        mutate=cancel,
    )
    store.cancel_steps(tenant, thread_id, run_id)
    return run


def _modify_fields(resource: Json, body: Json, fields: tuple[str, ...]) -> Json:
    updated = dict(resource)
    for field in fields:
        if field not in body:
            continue
        value = body.get(field)
        if field in {"metadata", "tool_resources"}:
            value = _metadata(value)
        elif field == "tools":
            value = value if isinstance(value, list) else []
        updated[field] = value
    return updated


def _query_value(query: Json, key: str) -> Any:
    value = query.get(key)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _query_object(query: Json) -> Json:
    return {key: _query_value(query, key) for key in ("limit", "order", "after", "before")}


def is_assistants_api_path(path: str) -> bool:
    return path == "/v1/assistants" or path.startswith("/v1/assistants/") or path == "/v1/threads" or path.startswith("/v1/threads/")


def handle_assistants_request(
    method: str,
    path: str,
    body: Json | None = None,
    *,
    query: Json | None = None,
    client_id: str | None = None,
    executor: RunExecutor | None = None,
) -> AssistantRouteResult:
    """Route one authenticated Assistants/Threads request."""
    body = body if isinstance(body, dict) else {}
    query = _query_object(query if isinstance(query, dict) else {})
    method = method.upper()
    parts = [item for item in path.strip("/").split("/") if item]
    tenant = _tenant_key(client_id)
    store = get_assistant_store()

    if parts == ["v1", "assistants"]:
        if method == "POST":
            return AssistantRouteResult(200, create_assistant_response(body, client_id=client_id), "assistants", "create")
        if method == "GET":
            return AssistantRouteResult(200, store.list_resources("assistants", tenant, query), "assistants", "list")
    if len(parts) == 3 and parts[:2] == ["v1", "assistants"]:
        assistant_id = parts[2]
        assistant = store.get_resource("assistants", tenant, assistant_id)
        if assistant is None:
            raise _not_found("assistant", assistant_id)
        if method == "GET":
            return AssistantRouteResult(200, assistant, "assistants", "retrieve")
        if method == "POST":
            modified = _modify_fields(
                assistant,
                body,
                ("model", "name", "description", "instructions", "tools", "tool_resources", "metadata", "temperature", "top_p", "response_format"),
            )
            store.put_resource("assistants", tenant, modified)
            return AssistantRouteResult(200, modified, "assistants", "modify")
        if method == "DELETE":
            store.delete_resource("assistants", tenant, assistant_id)
            return AssistantRouteResult(200, {"id": assistant_id, "object": "assistant.deleted", "deleted": True}, "assistants", "delete")

    if parts == ["v1", "threads"] and method == "POST":
        return AssistantRouteResult(200, create_thread_response(body, client_id=client_id), "threads", "create")
    if parts == ["v1", "threads", "runs"] and method == "POST":
        raw_thread_body = body.get("thread")
        thread_body: Json = raw_thread_body if isinstance(raw_thread_body, dict) else {}
        created_thread = create_thread_response(thread_body, client_id=client_id)
        run_body = dict(body)
        run_body.pop("thread", None)
        run = create_run(str(created_thread["id"]), run_body, client_id=client_id, executor=executor)
        return AssistantRouteResult(200, run, "runs", "create_thread_and_run")
    if len(parts) == 3 and parts[:2] == ["v1", "threads"]:
        thread_id = parts[2]
        existing_thread = store.get_resource("threads", tenant, thread_id)
        if existing_thread is None:
            raise _not_found("thread", thread_id)
        if method == "GET":
            return AssistantRouteResult(200, existing_thread, "threads", "retrieve")
        if method == "POST":
            modified = _modify_fields(existing_thread, body, ("metadata", "tool_resources"))
            store.put_resource("threads", tenant, modified)
            return AssistantRouteResult(200, modified, "threads", "modify")
        if method == "DELETE":
            store.delete_resource("threads", tenant, thread_id)
            return AssistantRouteResult(200, {"id": thread_id, "object": "thread.deleted", "deleted": True}, "threads", "delete")

    if len(parts) >= 4 and parts[:2] == ["v1", "threads"]:
        thread_id = parts[2]
        if store.get_resource("threads", tenant, thread_id) is None:
            raise _not_found("thread", thread_id)

        if parts[3] == "messages":
            if len(parts) == 4:
                if method == "POST":
                    return AssistantRouteResult(200, create_message(thread_id, body, client_id=client_id), "messages", "create")
                if method == "GET":
                    return AssistantRouteResult(200, store.list_messages(tenant, thread_id, query), "messages", "list")
            if len(parts) == 5:
                message_id = parts[4]
                message = store.get_message(tenant, thread_id, message_id)
                if message is None:
                    raise _not_found("message", message_id)
                if method == "GET":
                    return AssistantRouteResult(200, message, "messages", "retrieve")
                if method == "POST":
                    modified = _modify_fields(message, body, ("metadata",))
                    store.put_message(tenant, modified)
                    return AssistantRouteResult(200, modified, "messages", "modify")
                if method == "DELETE":
                    store.delete_message(tenant, thread_id, message_id)
                    return AssistantRouteResult(200, {"id": message_id, "object": "thread.message.deleted", "deleted": True}, "messages", "delete")

        if parts[3] == "runs":
            if len(parts) == 4:
                if method == "POST":
                    return AssistantRouteResult(200, create_run(thread_id, body, client_id=client_id, executor=executor), "runs", "create")
                if method == "GET":
                    return AssistantRouteResult(200, store.list_runs(tenant, thread_id, query), "runs", "list")
            if len(parts) >= 5:
                run_id = parts[4]
                run_record = store.get_run(tenant, thread_id, run_id)
                if run_record is None:
                    raise _not_found("run", run_id)
                run, state = run_record
                if len(parts) == 5:
                    if method == "GET":
                        return AssistantRouteResult(200, run, "runs", "retrieve")
                    if method == "POST":
                        modified = _modify_fields(run, body, ("metadata",))
                        saved = store.save_run_if_status(
                            tenant,
                            modified,
                            state,
                            expected={str(run.get("status") or "")},
                        )
                        return AssistantRouteResult(200, saved, "runs", "modify")
                if len(parts) == 6 and parts[5] == "cancel" and method == "POST":
                    return AssistantRouteResult(200, cancel_run(thread_id, run_id, client_id=client_id), "runs", "cancel")
                if len(parts) == 6 and parts[5] == "submit_tool_outputs" and method == "POST":
                    return AssistantRouteResult(
                        200,
                        submit_tool_outputs(thread_id, run_id, body, client_id=client_id, executor=executor),
                        "runs",
                        "submit_tool_outputs",
                    )
                if len(parts) == 6 and parts[5] == "steps" and method == "GET":
                    return AssistantRouteResult(200, store.list_steps(tenant, thread_id, run_id, query), "run_steps", "list")
                if len(parts) == 7 and parts[5] == "steps" and method == "GET":
                    step = store.get_step(tenant, thread_id, run_id, parts[6])
                    if step is None:
                        raise _not_found("run_step", parts[6])
                    return AssistantRouteResult(200, step, "run_steps", "retrieve")

    raise AssistantAPIError(
        f"unsupported Assistants route: {method} {path}",
        status=404,
        detail={"method": method, "path": path},
    )


def handle_assistants_or_threads(path: str, body: Json) -> Json | None:
    """Backward-compatible exact-create helper retained for older callers."""
    if path not in {"/v1/assistants", "/v1/threads"}:
        return None
    return handle_assistants_request("POST", path, body).payload


__all__ = [
    "AssistantAPIError",
    "AssistantRouteResult",
    "AssistantStore",
    "cancel_run",
    "create_assistant_response",
    "create_message",
    "create_run",
    "create_thread_response",
    "get_assistant_store",
    "handle_assistants_or_threads",
    "handle_assistants_request",
    "is_assistants_api_path",
    "reset_assistant_store",
    "submit_tool_outputs",
]
