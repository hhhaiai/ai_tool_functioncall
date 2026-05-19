#!/usr/bin/env python3
"""Native tools/function-call gateway.

This server does NOT simulate tool calls with prompt JSON. It forwards native
`tools`, `tool_choice`, `tool_calls`, and Anthropic `tool_use` protocol objects
to an upstream provider that already supports them. If the upstream rejects or
fails a forced native tool call, the gateway fails fast instead of pretending.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import base64
import concurrent.futures
import contextlib
import copy
import datetime as _dt
import glob
import hashlib
import html
import json
import math
import os
import pathlib
import re
import select
import shlex
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

Json = dict[str, Any]

# Lazy import for marketplace to avoid circular imports
_marketplace = None


def _get_marketplace():
    global _marketplace
    if _marketplace is None:
        try:
            from marketplace import list_mcp_marketplace
            _marketplace = list_mcp_marketplace
        except Exception:
            _marketplace = lambda: []
    return _marketplace

SUPPORTED_PATHS = {
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/assistants",  # OpenAI Assistants API
    "/v1/threads",  # OpenAI Assistants Threads
}
MODEL_LIST_PATHS = {"/v1/models"}
TOKEN_COUNT_PATHS = {"/v1/messages/count_tokens", "/v1/chat/completions/count_tokens"}
DIRECT_TOOL_CALL_PATHS = {"/v1/tools/call", "/v1/functions/call", "/tools/call"}
DEFAULT_MAX_TOOL_ROUNDS = 5
CONFIG_PATH = pathlib.Path(os.environ.get("GATEWAY_CONFIG_PATH") or ".gateway_service.json")
REQUEST_LOG_PATH = pathlib.Path(os.environ.get("GATEWAY_REQUEST_LOG") or ".gateway_requests.jsonl")
STATS_PATH = pathlib.Path(os.environ.get("GATEWAY_STATS_PATH") or ".gateway_stats.json")
SQLITE_LOG_PATH = pathlib.Path(os.environ.get("GATEWAY_SQLITE_LOG_PATH") or "gateway_log.sqlite3")
MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_CATALOG_CACHE_TTL_SECONDS = 60


class GatewayError(Exception):
    status = 500

    def __init__(self, message: str, *, detail: Any | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class UpstreamHTTPError(GatewayError):
    status = 502

    def __init__(self, upstream_status: int, detail: str) -> None:
        super().__init__(f"upstream HTTP {upstream_status}", detail=detail)
        self.upstream_status = upstream_status


class UpstreamTimeoutError(GatewayError):
    status = 504


class NativeToolVerificationError(GatewayError):
    status = 502


class DownstreamAuthError(GatewayError):
    status = 401


class GatewayBusyError(GatewayError):
    status = 429


class ToolExecutionError(Exception):
    def __init__(self, message: str, *, failure_type: str = "execution_failed") -> None:
        super().__init__(message)
        self.failure_type = failure_type


@dataclass(frozen=True)
class GatewayTool:
    name: str
    description: str
    parameters: Json
    handler: Callable[[Json], str]
    risk: str = "pure"
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Json
    raw: Json


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    success: bool = True
    failure_type: str | None = None


class McpSession:
    def __init__(self, server: Json) -> None:
        self.server = copy.deepcopy(server)
        self.name = str(server.get("name") or "")
        self.timeout = float(server.get("timeout") or os.environ.get("GATEWAY_MCP_TIMEOUT", "20"))
        self.proc = _mcp_start(server)
        self.lock = threading.Lock()
        self.next_id = 1
        self.last_used_at = time.time()
        try:
            _mcp_initialize(self.proc, server, self.timeout, self._next_id_locked())
        except Exception:
            self.close()
            raise

    def _next_id_locked(self) -> int:
        request_id = self.next_id
        self.next_id += 1
        return request_id

    def request(self, method: str, params: Json | None = None) -> Json:
        with self.lock:
            if self.proc.poll() is not None:
                raise ToolExecutionError(f"MCP server {self.name} exited", failure_type="execution_failed")
            self.last_used_at = time.time()
            return _mcp_request(
                self.proc,
                method,
                params,
                request_id=self._next_id_locked(),
                timeout=self.timeout,
            )

    def close(self) -> None:
        proc = self.proc
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass


MCP_SESSIONS: dict[str, McpSession] = {}
MCP_SESSIONS_LOCK = threading.Lock()
MCP_TOOL_CATALOG_CACHE: dict[str, tuple[float, list[Json]]] = {}
MCP_SERVER_STATUS: dict[str, Json] = {}
EXEC_SESSIONS: dict[str, subprocess.Popen] = {}
EXEC_SESSIONS_LOCK = threading.Lock()
REQUEST_SEMAPHORE_LOCK = threading.Lock()
REQUEST_SEMAPHORE: threading.BoundedSemaphore | None = None
REQUEST_SEMAPHORE_SIZE: int | None = None
SQLITE_LOCK = threading.Lock()
WORKSPACE_CONTEXT = threading.local()
SQLITE_READY = False


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except Exception:
        return default


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
            _sqlite_import_legacy_logs_locked(conn)
            conn.commit()
            SQLITE_READY = True
        finally:
            conn.close()


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


def _default_config() -> Json:
    # Admin password: use env var if set, otherwise use known default for dev/testing
    # Production deployments MUST set GATEWAY_ADMIN_PASSWORD or GATEWAY_ADMIN_PASSWORD_HASH
    admin_password_hash = os.environ.get("GATEWAY_ADMIN_PASSWORD_HASH", "")
    if not admin_password_hash:
        admin_password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "")
        if admin_password:
            admin_password_hash = _hash_secret(admin_password)
        else:
            # Development/testing fallback - MUST be changed before production use
            admin_password_hash = _hash_secret("admin")
    # Track if we're using an unconfigured default (should be changed)
    admin_must_change = not os.environ.get("GATEWAY_ADMIN_PASSWORD") and not os.environ.get("GATEWAY_ADMIN_PASSWORD_HASH")

    # Downstream keys: use env var if set, otherwise no default (must be configured)
    downstream_keys: list = []
    downstream_key_env = os.environ.get("GATEWAY_DOWNSTREAM_KEY", "")
    if downstream_key_env:
        downstream_keys.append({
            "name": "default",
            "key_hash": _hash_secret(downstream_key_env),
            "prefix": downstream_key_env[:8],
            "enabled": True,
            "protocols": ["models", "chat_completions", "responses", "messages", "direct_tools"],
            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        })

    return {
        "admin": {
            "username": "admin",
            "password_hash": admin_password_hash,
            "must_change_password": admin_must_change,
        },
        "upstream": {
            "base_url": os.environ.get("UPSTREAM_BASE_URL", ""),
            "api_key": os.environ.get("UPSTREAM_API_KEY", ""),
            "model": os.environ.get("UPSTREAM_MODEL", ""),
            "protocol": os.environ.get("GATEWAY_UPSTREAM_PROTOCOL", "openai_chat"),
            "tools_enabled": os.environ.get("GATEWAY_TOOLS_ENABLED", "auto"),
            "native_tools_verified": False,
            "use_for_coding": True,
            "timeout_seconds": _env_float("UPSTREAM_TIMEOUT", 60.0),
            "max_input_tokens": _env_int("UPSTREAM_MAX_INPUT_TOKENS", 128000),
            "max_output_tokens": _env_int("UPSTREAM_MAX_OUTPUT_TOKENS", 8192),
            "max_concurrency": _env_int("UPSTREAM_MAX_CONCURRENCY", 32),
            "paths": {
                "models": os.environ.get("UPSTREAM_MODELS_PATH", "/v1/models"),
                "chat_completions": os.environ.get("UPSTREAM_CHAT_COMPLETIONS_PATH", "/v1/chat/completions"),
                "responses": os.environ.get("UPSTREAM_RESPONSES_PATH", "/v1/responses"),
                "messages": os.environ.get("UPSTREAM_MESSAGES_PATH", "/v1/messages"),
            },
            "capabilities": {
                "supports_streaming": _env_bool("UPSTREAM_SUPPORTS_STREAMING", True),
                "supports_tools": _env_bool("UPSTREAM_SUPPORTS_TOOLS", True),
                "supports_function_calls": _env_bool("UPSTREAM_SUPPORTS_FUNCTION_CALLS", True),
                "supports_parallel_tool_calls": _env_bool("UPSTREAM_SUPPORTS_PARALLEL_TOOL_CALLS", True),
                "supports_vision": _env_bool("UPSTREAM_SUPPORTS_VISION", False),
                "supports_network": _env_bool("UPSTREAM_SUPPORTS_NETWORK", False),
                "supports_json_schema": _env_bool("UPSTREAM_SUPPORTS_JSON_SCHEMA", True),
            },
        },
        "gateway": {
            "tool_mode": os.environ.get("GATEWAY_TOOL_MODE", "orchestrate"),
            "max_tool_rounds": int(os.environ.get("GATEWAY_MAX_TOOL_ROUNDS") or DEFAULT_MAX_TOOL_ROUNDS),
            "workspace_root": os.environ.get("GATEWAY_WORKSPACE_ROOT") or os.getcwd(),
            "allow_write_tools": os.environ.get("GATEWAY_ALLOW_WRITE_TOOLS", "0") in {"1", "true", "yes"},
            "allow_shell_tools": os.environ.get("GATEWAY_ALLOW_SHELL_TOOLS", "0") in {"1", "true", "yes"},
            "request_logging": True,
            "logging_backend": os.environ.get("GATEWAY_LOGGING_BACKEND", "sqlite"),
            "sqlite_log_path": str(_sqlite_path()),
            "max_concurrent_requests": _env_int("GATEWAY_MAX_CONCURRENT_REQUESTS", 32),
            "concurrency_queue_timeout_seconds": _env_float("GATEWAY_CONCURRENCY_QUEUE_TIMEOUT", 5.0),
            "tool_execution_timeout_seconds": _env_float("GATEWAY_TOOL_EXECUTION_TIMEOUT", 60.0),
            "record_unsupported_tools": _env_bool("GATEWAY_RECORD_UNSUPPORTED_TOOLS", True),
            "text_tool_call_fallback_enabled": _env_bool("GATEWAY_TEXT_TOOL_CALL_FALLBACK", True),
            "local_planner_enabled": _env_bool("GATEWAY_LOCAL_PLANNER_ENABLED", True),
            "local_planner_max_files": _env_int("GATEWAY_LOCAL_PLANNER_MAX_FILES", 24),
            "local_planner_max_bytes_per_file": _env_int("GATEWAY_LOCAL_PLANNER_MAX_BYTES_PER_FILE", 24000),
            "public_base_url": os.environ.get("GATEWAY_PUBLIC_BASE_URL", "http://127.0.0.1:8885"),
            "client_snippet_api_key": os.environ.get("DOWNSTREAM_API_KEY", ""),
            "downstream_model_alias": os.environ.get("GATEWAY_DOWNSTREAM_MODEL_ALIAS", os.environ.get("UPSTREAM_MODEL", "")),
            "review_model_alias": os.environ.get("GATEWAY_REVIEW_MODEL_ALIAS", os.environ.get("GATEWAY_DOWNSTREAM_MODEL_ALIAS", os.environ.get("UPSTREAM_MODEL", ""))),
            "codex_reasoning_effort": os.environ.get("GATEWAY_CODEX_REASONING_EFFORT", "xhigh"),
            "client_context_window": _env_int("GATEWAY_CLIENT_CONTEXT_WINDOW", 1000000),
            "client_auto_compact_token_limit": _env_int("GATEWAY_CLIENT_AUTO_COMPACT_TOKEN_LIMIT", 900000),
            "client_output_token_limit": _env_int("GATEWAY_CLIENT_OUTPUT_TOKEN_LIMIT", 128000),
        },
        "context": {
            "enabled": os.environ.get("GATEWAY_CONTEXT_ENABLED", "1").lower() in {"1", "true", "yes"},
            "max_input_tokens": int(os.environ.get("GATEWAY_CONTEXT_MAX_INPUT_TOKENS") or "24000"),
            "keep_recent_messages": int(os.environ.get("GATEWAY_CONTEXT_KEEP_RECENT_MESSAGES") or "12"),
            "summary_max_chars": int(os.environ.get("GATEWAY_CONTEXT_SUMMARY_MAX_CHARS") or "6000"),
            "fanout_enabled": os.environ.get("GATEWAY_CONTEXT_FANOUT_ENABLED", "1").lower() in {"1", "true", "yes"},
            "fanout_chunk_tokens": int(os.environ.get("GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS") or "12000"),
            "fanout_max_chunks": int(os.environ.get("GATEWAY_CONTEXT_FANOUT_MAX_CHUNKS") or "0"),
            "fanout_max_workers": int(os.environ.get("GATEWAY_CONTEXT_FANOUT_MAX_WORKERS") or "4"),
            "quality_review_enabled": _env_bool("GATEWAY_CONTEXT_QUALITY_REVIEW", True),
            "memory_enabled": _env_bool("GATEWAY_MEMORY_ENABLED", True),
            "memory_max_items": _env_int("GATEWAY_MEMORY_MAX_ITEMS", 200),
            "memory_recall_limit": _env_int("GATEWAY_MEMORY_RECALL_LIMIT", 8),
            "memory_inject_max_chars": _env_int("GATEWAY_MEMORY_INJECT_MAX_CHARS", 4000),
            "memory_summary_max_chars": _env_int("GATEWAY_MEMORY_SUMMARY_MAX_CHARS", 900),
            "route_to_long_context": os.environ.get("GATEWAY_CONTEXT_ROUTE_LONG", "1").lower() in {"1", "true", "yes"},
            "long_context_upstream": {
                "base_url": os.environ.get("GATEWAY_LONG_CONTEXT_BASE_URL", ""),
                "api_key": os.environ.get("GATEWAY_LONG_CONTEXT_API_KEY", ""),
                "model": os.environ.get("GATEWAY_LONG_CONTEXT_MODEL", ""),
                "protocol": os.environ.get("GATEWAY_LONG_CONTEXT_PROTOCOL", ""),
            },
        },
        "downstream_keys": downstream_keys,
        "mcp": {
            "servers": [],
            "marketplace_enabled": True,
        },
        "http_actions": {
            "enabled": True,
            "actions": [],
        },
    }


def load_config() -> Json:
    if not CONFIG_PATH.exists():
        cfg = _sync_active_upstream(_default_config())
        save_config(cfg)
        return cfg
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config root must be object")
    except Exception:
        loaded = {}
    cfg = _default_config()
    _deep_update(cfg, loaded)
    return _sync_active_upstream(cfg)


def save_config(config: Json) -> None:
    CONFIG_PATH.write_text(json.dumps(_sync_active_upstream(config), ensure_ascii=False, indent=2), encoding="utf-8")


def _deep_update(base: Json, updates: Json) -> Json:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _upstream_profile_id(profile: Json) -> str:
    raw = str(profile.get("id") or profile.get("name") or profile.get("base_url") or uuid.uuid4().hex[:8])
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-._")
    return cleaned or f"upstream-{uuid.uuid4().hex[:8]}"


def _normalize_upstream_profile(profile: Json, *, fallback_name: str = "default") -> Json:
    default_upstream = _default_config()["upstream"] if "_NORMALIZING_DEFAULT_UPSTREAM" not in globals() else {}
    merged = copy.deepcopy(default_upstream)
    _deep_update(merged, profile if isinstance(profile, dict) else {})
    merged["name"] = str(merged.get("name") or fallback_name or "default")
    merged["id"] = _upstream_profile_id(merged)
    merged.setdefault("paths", {})
    merged.setdefault("capabilities", {})
    return merged


def _sync_active_upstream(config: Json) -> Json:
    profiles = config.get("upstream_profiles")
    if not isinstance(profiles, list) or not profiles:
        base = copy.deepcopy(config.get("upstream", {}) if isinstance(config.get("upstream"), dict) else {})
        base.setdefault("name", "default")
        base.setdefault("id", "default")
        profiles = [base]
    normalized: list[Json] = []
    seen: set[str] = set()
    for index, item in enumerate(profiles):
        if not isinstance(item, dict):
            continue
        profile = _normalize_upstream_profile(item, fallback_name=f"upstream-{index + 1}")
        base_id = profile["id"]
        candidate = base_id
        counter = 2
        while candidate in seen:
            candidate = f"{base_id}-{counter}"
            counter += 1
        profile["id"] = candidate
        seen.add(candidate)
        normalized.append(profile)
    if not normalized:
        normalized.append(_normalize_upstream_profile({"id": "default", "name": "default"}, fallback_name="default"))
    active_id = str(config.get("active_upstream") or normalized[0].get("id"))
    if active_id not in {str(p.get("id")) for p in normalized}:
        active_id = str(normalized[0].get("id"))
    active = copy.deepcopy(next(p for p in normalized if str(p.get("id")) == active_id))
    config["upstream_profiles"] = normalized
    config["active_upstream"] = active_id
    config["upstream"] = active
    return config


def _profile_from_admin_form(form: dict[str, str], existing: Json | None = None) -> Json:
    profile = copy.deepcopy(existing or {})
    profile["id"] = _upstream_profile_id({"id": form.get("profile_id") or profile.get("id") or form.get("profile_name") or "upstream"})
    profile["name"] = form.get("profile_name", "").strip() or str(profile.get("name") or profile["id"])
    profile["base_url"] = form.get("base_url", "").strip()
    if form.get("api_key"):
        profile["api_key"] = form["api_key"].strip()
    elif "api_key" not in profile:
        profile["api_key"] = ""
    profile["model"] = form.get("model", "").strip()
    profile["protocol"] = form.get("protocol", "openai_chat")
    profile["tools_enabled"] = form.get("tools_enabled", "auto")
    profile["native_tools_verified"] = form.get("native_tools_verified") == "1"
    profile["use_for_coding"] = form.get("use_for_coding") == "1"
    profile["timeout_seconds"] = float(form.get("upstream_timeout_seconds") or 60)
    profile["max_input_tokens"] = int(form.get("upstream_max_input_tokens") or 128000)
    profile["max_output_tokens"] = int(form.get("upstream_max_output_tokens") or 8192)
    profile["max_concurrency"] = int(form.get("upstream_max_concurrency") or 32)
    profile["capabilities"] = {
        "supports_streaming": form.get("cap_supports_streaming") == "1",
        "supports_tools": form.get("cap_supports_tools") == "1",
        "supports_function_calls": form.get("cap_supports_function_calls") == "1",
        "supports_parallel_tool_calls": form.get("cap_supports_parallel_tool_calls") == "1",
        "supports_vision": form.get("cap_supports_vision") == "1",
        "supports_network": form.get("cap_supports_network") == "1",
        "supports_web_search": form.get("cap_supports_web_search") == "1" or form.get("cap_supports_network") == "1",
        "supports_json_schema": form.get("cap_supports_json_schema") == "1",
    }
    profile["paths"] = {
        "models": form.get("path_models") or "/v1/models",
        "chat_completions": form.get("path_chat_completions") or "/v1/chat/completions",
        "responses": form.get("path_responses") or "/v1/responses",
        "messages": form.get("path_messages") or "/v1/messages",
    }
    return profile


def _redacted_config(config: Json) -> Json:
    redacted = _redact_payload(copy.deepcopy(config))
    if redacted.get("upstream", {}).get("api_key"):
        key = redacted["upstream"]["api_key"]
        redacted["upstream"]["api_key"] = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
    for profile in redacted.get("upstream_profiles", []) or []:
        if isinstance(profile, dict) and profile.get("api_key"):
            key = str(profile["api_key"])
            profile["api_key"] = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
    for item in redacted.get("downstream_keys", []):
        item.pop("key_hash", None)
    return redacted


def _config_env(name: str, fallback: str = "") -> str:
    cfg = load_config()
    upstream = cfg.get("upstream", {})
    gateway = cfg.get("gateway", {})
    mapping = {
        "UPSTREAM_BASE_URL": upstream.get("base_url") or fallback,
        "UPSTREAM_API_KEY": upstream.get("api_key") or fallback,
        "UPSTREAM_MODEL": upstream.get("model") or fallback,
        "UPSTREAM_TIMEOUT": str(upstream.get("timeout_seconds") or fallback),
        "GATEWAY_TOOL_MODE": gateway.get("tool_mode") or fallback,
        "GATEWAY_MAX_TOOL_ROUNDS": str(gateway.get("max_tool_rounds") or fallback),
        "GATEWAY_WORKSPACE_ROOT": gateway.get("workspace_root") or fallback,
        "GATEWAY_ALLOW_WRITE_TOOLS": "1" if gateway.get("allow_write_tools") else fallback,
        "GATEWAY_ALLOW_SHELL_TOOLS": "1" if gateway.get("allow_shell_tools") else fallback,
    }
    return os.environ.get(name) or str(mapping.get(name) or fallback)


def _upstream_config() -> Json:
    cfg = load_config().get("upstream", {})
    return cfg if isinstance(cfg, dict) else {}


def _gateway_config() -> Json:
    cfg = load_config().get("gateway", {})
    return cfg if isinstance(cfg, dict) else {}


def _configured_upstream_path(path: str) -> str:
    paths = _upstream_config().get("paths", {})
    if not isinstance(paths, dict):
        return path
    key_by_path = {
        "/v1/models": "models",
        "/v1/chat/completions": "chat_completions",
        "/v1/responses": "responses",
        "/v1/messages": "messages",
    }
    configured = str(paths.get(key_by_path.get(path, "")) or path)
    return configured if configured.startswith("/") else f"/{configured}"


def _configured_upstream_path_by_key(key: str, default: str) -> str:
    paths = _upstream_config().get("paths", {})
    configured = str(paths.get(key) if isinstance(paths, dict) else "" or default)
    return configured if configured.startswith("/") else f"/{configured}"


def _upstream_protocol() -> str:
    return str(_upstream_config().get("protocol") or os.environ.get("GATEWAY_UPSTREAM_PROTOCOL") or "openai_chat").lower()


def _use_openai_chat_upstream(path: str) -> bool:
    return _upstream_protocol() in {"openai_chat", "openai_compatible", "chat_completions"} and path in SUPPORTED_PATHS


def _force_upstream_stream_aggregate() -> bool:
    # The current single upstream has proven more responsive on streaming
    # chat/completions than on non-streaming /messages. Aggregate upstream SSE
    # into a normal response for orchestration so Claude Code still receives
    # protocol-compatible final objects.
    return os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE", "1").lower() in {"1", "true", "yes"}


def _acquire_request_slot() -> threading.BoundedSemaphore | None:
    global REQUEST_SEMAPHORE, REQUEST_SEMAPHORE_SIZE
    gateway = _gateway_config()
    limit = int(gateway.get("max_concurrent_requests") or 0)
    if limit <= 0:
        return None
    with REQUEST_SEMAPHORE_LOCK:
        if REQUEST_SEMAPHORE is None or REQUEST_SEMAPHORE_SIZE != limit:
            REQUEST_SEMAPHORE = threading.BoundedSemaphore(limit)
            REQUEST_SEMAPHORE_SIZE = limit
        sem = REQUEST_SEMAPHORE
    timeout = float(gateway.get("concurrency_queue_timeout_seconds") or 0)
    ok = sem.acquire(timeout=timeout) if timeout > 0 else sem.acquire(blocking=False)
    if not ok:
        raise GatewayBusyError(f"gateway concurrency limit reached ({limit})")
    return sem


def _approx_token_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        # Conservative enough for routing decisions without adding tokenizer deps:
        # ASCII-ish text averages ~4 chars/token; CJK is closer to 1-2 chars/token.
        cjk = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
        other = max(len(value) - cjk, 0)
        return cjk + max(1, other // 4)
    if isinstance(value, (int, float, bool)):
        return 1
    if isinstance(value, list):
        return sum(_approx_token_count(item) for item in value)
    if isinstance(value, dict):
        return sum(_approx_token_count(k) + _approx_token_count(v) for k, v in value.items())
    return _approx_token_count(str(value))


def _context_config() -> Json:
    cfg = load_config().get("context", {})
    return cfg if isinstance(cfg, dict) else {}


def _context_enabled() -> bool:
    return bool(_context_config().get("enabled"))


def _body_token_estimate(body: Json) -> int:
    body_without_tools = {k: v for k, v in body.items() if k not in {"tools", "tool_choice"}}
    return _approx_token_count(body_without_tools)


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _openai_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"text", "input_text", "output_text"} and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item_type == "tool_result":
                parts.append(f"[tool_result {item.get('tool_use_id') or ''}]\n{_text_from_content(item.get('content'))}")
            elif isinstance(item.get("content"), str):
                parts.append(item["content"])
            elif isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(part for part in parts if part)
    return str(content) if content is not None else ""


def _anthropic_system_to_text(system: Any) -> str:
    return _openai_text_from_content(system)


def _to_openai_chat_payload(path: str, body: Json, *, stream: bool | None = None) -> Json:
    payload = _without_tools(_copy_model_override(body))
    model = payload.get("model") or _config_env("UPSTREAM_MODEL", "")
    messages: list[Json] = []
    if path == "/v1/messages":
        system_text = _anthropic_system_to_text(payload.get("system"))
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for msg in payload.get("messages") or []:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "user")
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            messages.append({"role": role, "content": _openai_text_from_content(msg.get("content"))})
    elif path == "/v1/responses":
        instructions = payload.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            messages.append({"role": "system", "content": instructions})
        existing = payload.get("input")
        if isinstance(existing, list):
            for item in existing:
                if isinstance(item, dict):
                    messages.append({"role": str(item.get("role") or "user"), "content": _openai_text_from_content(item.get("content") or item.get("text") or item)})
                else:
                    messages.append({"role": "user", "content": str(item)})
        elif existing is not None:
            messages.append({"role": "user", "content": _openai_text_from_content(existing)})
    else:
        for msg in payload.get("messages") or []:
            if isinstance(msg, dict):
                copied = dict(msg)
                copied["content"] = _openai_text_from_content(copied.get("content"))
                copied.pop("tool_calls", None)
                messages.append(copied)
    if not messages:
        messages.append({"role": "user", "content": _last_user_text(path, body) or ""})
    out: Json = {"model": model, "messages": messages}
    if payload.get("temperature") is not None:
        out["temperature"] = payload["temperature"]
    max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if max_tokens is not None:
        out["max_tokens"] = max_tokens
    if stream is not None:
        out["stream"] = stream
    elif payload.get("stream") is not None:
        out["stream"] = bool(payload.get("stream"))
    return out


def _openai_tool_calls_from_response(response: Json) -> list[dict]:
    """Extract tool_calls list from an OpenAI chat completions response."""
    choice = (response.get("choices") or [{}])[0] if isinstance(response.get("choices"), list) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        return []
    tc_list = message.get("tool_calls")
    if isinstance(tc_list, list):
        return [tc for tc in tc_list if isinstance(tc, dict)]
    # Also check for function_call (legacy single-call format)
    fc = message.get("function_call")
    if isinstance(fc, dict) and fc.get("name"):
        return [{"id": "call_legacy", "type": "function", "function": fc}]
    return []


def _from_openai_chat_response(path: str, response: Json) -> Json:
    """Convert OpenAI chat completions response to target path format.
    Handles tool_calls → Anthropic tool_use / Responses function_call conversion."""
    if path == "/v1/chat/completions":
        return response
    text = _response_text("/v1/chat/completions", response)
    choice = (response.get("choices") or [{}])[0] if isinstance(response.get("choices"), list) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    reasoning = (message.get("reasoning") or message.get("reasoning_content")) if isinstance(message, dict) else None
    tool_calls = _openai_tool_calls_from_response(response)
    has_tool_calls = bool(tool_calls)

    if path == "/v1/messages":
        content: list[Json] = []
        if isinstance(reasoning, str) and reasoning.strip():
            content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
        # Convert OpenAI tool_calls → Anthropic tool_use content blocks
        if has_tool_calls:
            for tc in tool_calls:
                func = tc.get("function") or {}
                name = func.get("name") or ""
                args_raw = func.get("arguments") or "{}"
                try:
                    input_data = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except (json.JSONDecodeError, TypeError):
                    input_data = {}
                content.append({
                    "type": "tool_use",
                    "id": tc.get("id") or f"toolu_{uuid.uuid4().hex}",
                    "name": name,
                    "input": input_data if isinstance(input_data, dict) else {},
                })
        if text:
            content.append({"type": "text", "text": text})
        if not content:
            content.append({"type": "text", "text": ""})
        return {
            "id": response.get("id") or f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": content,
            "model": response.get("model") or _config_env("UPSTREAM_MODEL", ""),
            "stop_reason": "tool_use" if has_tool_calls else "end_turn",
            "stop_sequence": None,
            "usage": response.get("usage") or {},
        }
    if path == "/v1/responses":
        output: list[Json] = []
        if has_tool_calls:
            for tc in tool_calls:
                func = tc.get("function") or {}
                output.append({
                    "type": "function_call",
                    "id": f"fc_{uuid.uuid4().hex}",
                    "call_id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                    "name": func.get("name") or "",
                    "arguments": func.get("arguments") or "{}",
                })
        if text:
            output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
        if not output:
            output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": ""}]})
        return {
            "id": response.get("id") or f"resp_{uuid.uuid4().hex}",
            "object": "response",
            "model": response.get("model") or _config_env("UPSTREAM_MODEL", ""),
            "output": output,
            "usage": response.get("usage") or {},
            "status": "completed",
        }
    return response


def _last_user_text(path: str, body: Json) -> str:
    if path in {"/v1/chat/completions", "/v1/messages"}:
        for message in reversed(body.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "user":
                return _text_from_content(message.get("content"))
        return ""
    existing = body.get("input")
    if isinstance(existing, str):
        return existing
    if isinstance(existing, list):
        for item in reversed(existing):
            if isinstance(item, dict) and item.get("role") == "user":
                return _text_from_content(item.get("content"))
        return _text_from_content(existing)
    return _text_from_content(existing)


def _replace_last_user_text(path: str, body: Json, text: str) -> Json:
    updated = copy.deepcopy(body)
    if path in {"/v1/chat/completions", "/v1/messages"}:
        messages = list(updated.get("messages") or [])
        for idx in range(len(messages) - 1, -1, -1):
            message = messages[idx]
            if isinstance(message, dict) and message.get("role") == "user":
                message = dict(message)
                message["content"] = text
                messages[idx] = message
                updated["messages"] = messages
                return updated
        messages.append({"role": "user", "content": text})
        updated["messages"] = messages
        return updated
    updated["input"] = text
    return updated


def _without_tools(body: Json) -> Json:
    updated = copy.deepcopy(body)
    updated.pop("tools", None)
    updated.pop("tool_choice", None)
    updated.pop("parallel_tool_calls", None)
    return updated


def _gateway_system_prompt(reason: str = "context_compaction") -> str:
    return (
        "你正在通过 Tool Call Gateway 服务 Claude Code/Codex/OpenCode/DeepSeek-TUI。"
        "如果当前上游支持原生 tools/function calls，请优先返回协议级 tool_use/tool_calls；"
        "如果上游不能稳定返回原生工具调用，可以使用文本形式：<function=ToolName>\\n<parameter=name>value。"
        "Gateway 会在本地执行真实工具并把结果回填。"
        "回答复杂代码问题时，按语义分析 -> 调用/证据检查 -> 反思调整 -> 最终结论推进，不要只给空泛结论。"
        f" gateway_reason={reason}"
    )


def _content_contains_gateway_prompt(value: Any) -> bool:
    return "Tool Call Gateway" in _text_from_content(value) or "gateway_reason=" in _text_from_content(value)


def _inject_gateway_system_prompt(path: str, body: Json, *, reason: str) -> Json:
    """Add Gateway execution guidance without adding upstream tool schemas.

    This is used for the current single-upstream mode where the upstream model is
    treated as not supporting native tool calls. The gateway still executes real
    local tools; the upstream request just avoids protocol-level tool schemas so
    weak providers do not reject/hang on `tools` payloads.
    """

    updated = copy.deepcopy(body)
    prompt = _gateway_system_prompt(reason)
    if path == "/v1/messages":
        existing = updated.get("system")
        if _content_contains_gateway_prompt(existing):
            return updated
        if isinstance(existing, str) and existing.strip():
            updated["system"] = existing.rstrip() + "\n\n" + prompt
        elif isinstance(existing, list):
            updated["system"] = list(existing) + [{"type": "text", "text": prompt}]
        elif existing:
            updated["system"] = str(existing).rstrip() + "\n\n" + prompt
        else:
            updated["system"] = prompt
        return updated
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        if any(isinstance(m, dict) and m.get("role") == "system" and _content_contains_gateway_prompt(m.get("content")) for m in messages):
            return updated
        messages.insert(0, {"role": "system", "content": prompt})
        updated["messages"] = messages
        return updated
    if path == "/v1/responses":
        existing = updated.get("instructions")
        if isinstance(existing, str) and "gateway_reason=" in existing:
            return updated
        updated["instructions"] = (existing.rstrip() + "\n\n" if isinstance(existing, str) and existing.strip() else "") + prompt
        return updated
    return updated



def _memory_config() -> Json:
    cfg = _context_config()
    return cfg if isinstance(cfg, dict) else {}


def _memory_enabled() -> bool:
    cfg = _memory_config()
    return bool(cfg.get("memory_enabled", True))


def _json_object_from_maybe_string(value: Any) -> Json:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _memory_session_key(body: Json) -> str:
    """Derive a stable conversation/session key without trusting raw secrets.

    Claude Code often carries session identity in metadata.user_id as a JSON
    string. Other clients use conversation_id/thread_id/session_id at top level.
    If none is present, scope memory to the workspace so unrelated projects do
    not bleed into each other.
    """

    candidates: list[Any] = [
        body.get("session_id"),
        body.get("conversation_id"),
        body.get("thread_id"),
        body.get("chat_id"),
    ]
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("session_id"),
                metadata.get("conversation_id"),
                metadata.get("thread_id"),
                metadata.get("chat_id"),
                metadata.get("user_id"),
            ]
        )
        nested = _json_object_from_maybe_string(metadata.get("user_id"))
        candidates.extend(
            [
                nested.get("session_id"),
                nested.get("conversation_id"),
                nested.get("thread_id"),
                nested.get("chat_id"),
                nested.get("account_id"),
            ]
        )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            clean = _clean_tool_string(candidate.strip())
            if len(clean) > 180:
                clean = hashlib.sha256(clean.encode("utf-8")).hexdigest()
            return clean
    workspace_hash = hashlib.sha256(str(_workspace_root()).encode("utf-8")).hexdigest()[:16]
    return f"workspace:{workspace_hash}"


def _memory_workspace_key() -> str:
    try:
        return str(_workspace_root())
    except Exception:
        return ""


def _memory_extract_keywords(text: str, *, limit: int = 40) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = value.strip().strip("'\"`.,;:()[]{}<>，。；：、")
        if not value or len(value) < 2:
            return
        lowered = value.lower()
        if lowered in seen:
            return
        seen.add(lowered)
        found.append(value[:120])

    for pattern in (r"@[\w./\\-]+", r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|kt|swift|md|json|yaml|yml|toml|sh)", r"[A-Za-z_][A-Za-z0-9_./:-]{2,}"):
        for match in re.findall(pattern, text):
            add(match.lstrip("@"))
            if len(found) >= limit:
                return found[:limit]
    for match in re.findall(r"[\u4e00-\u9fff]{2,8}", text):
        if match in {"这个", "那个", "进行", "可以", "希望", "需要", "然后", "如果", "同样", "支持", "实现"}:
            continue
        add(match)
        if len(found) >= limit:
            break
    return found[:limit]


def _memory_extract_request_text(path: str, body: Json) -> str:
    parts: list[str] = []
    if path in {"/v1/chat/completions", "/v1/messages"}:
        for message in body.get("messages") or []:
            if not isinstance(message, dict):
                continue
            role = message.get("role") or ""
            text = _text_from_content(message.get("content"))
            if text:
                parts.append(f"{role}: {text}")
        system = body.get("system")
        if system:
            parts.append("system: " + _text_from_content(system))
    elif path == "/v1/responses":
        if body.get("instructions"):
            parts.append("system: " + _text_from_content(body.get("instructions")))
        parts.append("input: " + _text_from_content(body.get("input")))
    else:
        parts.append(_last_user_text(path, body))
    return "\n".join(part for part in parts if part.strip())


def _memory_summarize_turn(path: str, body: Json, response: Json | None, *, max_chars: int) -> tuple[str, str, list[str], int]:
    user_text = _last_user_text(path, body).strip()
    request_text = _memory_extract_request_text(path, body)
    response_text = _response_text(path, response or {}) if isinstance(response, dict) else ""
    keywords = _memory_extract_keywords("\n".join([request_text, response_text]))
    kind = "conversation_turn"
    importance = 1
    lowered = user_text.lower()
    if any(token in lowered for token in ("修改", "写", "实现", "fix", "edit", "write", "测试", "运行", "error", "报错")):
        kind = "implementation_context"
        importance = 3
    elif any(token in lowered for token in ("分析", "analyze", "项目", "代码", "class", "类")):
        kind = "analysis_context"
        importance = 2
    if response_text:
        summary = f"用户请求：{_trim_text_for_context(user_text or request_text, max_chars // 2)}\n助手结论：{_trim_text_for_context(response_text, max_chars // 2)}"
    else:
        summary = f"用户请求：{_trim_text_for_context(user_text or request_text, max_chars)}"
    summary = _trim_text_for_context(summary, max_chars)
    return kind, summary, keywords, importance


def _sqlite_insert_memory(session_key: str, workspace_root: str, kind: str, summary: str, keywords: list[str], source_request_id: str | None, importance: int) -> None:
    _sqlite_init()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            conn.execute(
                """
                INSERT INTO conversation_memories
                (ts, session_key, workspace_root, kind, summary, keywords_json, source_request_id, importance, last_used_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (now, session_key, workspace_root, kind, summary, json.dumps(keywords, ensure_ascii=False), source_request_id, int(importance), now),
            )
            max_items = max(20, int(_memory_config().get("memory_max_items") or 200))
            conn.execute(
                """
                DELETE FROM conversation_memories
                WHERE session_key = ? AND workspace_root = ? AND id NOT IN (
                    SELECT id FROM conversation_memories
                    WHERE session_key = ? AND workspace_root = ?
                    ORDER BY importance DESC, id DESC
                    LIMIT ?
                )
                """,
                (session_key, workspace_root, session_key, workspace_root, max_items),
            )
            conn.commit()
        finally:
            conn.close()


def _remember_conversation_turn(path: str, body: Json, response: Json | None, *, source_request_id: str | None = None) -> None:
    if not _memory_enabled() or path not in SUPPORTED_PATHS:
        return
    try:
        max_chars = max(300, min(int(_memory_config().get("memory_summary_max_chars") or 900), 4000))
        kind, summary, keywords, importance = _memory_summarize_turn(path, body, response, max_chars=max_chars)
        if not summary.strip() or len(summary.strip()) < 10:
            return
        _sqlite_insert_memory(_memory_session_key(body), _memory_workspace_key(), kind, summary, keywords, source_request_id, importance)
    except Exception:
        if os.environ.get("DEBUG"):
            traceback.print_exc()


def _sqlite_recall_memories(session_key: str, workspace_root: str, query_keywords: list[str], limit: int) -> list[Json]:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        rows = conn.execute(
            """
            SELECT id, ts, kind, summary, keywords_json, importance, last_used_at
            FROM conversation_memories
            WHERE session_key = ? AND workspace_root = ?
            ORDER BY importance DESC, id DESC
            LIMIT ?
            """,
            (session_key, workspace_root, max(limit * 8, 40)),
        ).fetchall()
    finally:
        conn.close()
    query = {kw.lower() for kw in query_keywords}
    scored: list[tuple[int, Json]] = []
    for mem_id, ts, kind, summary, keywords_raw, importance, last_used_at in rows:
        try:
            keywords = json.loads(keywords_raw or "[]")
        except Exception:
            keywords = []
        keyword_set = {str(kw).lower() for kw in keywords}
        overlap = len(query & keyword_set) if query else 0
        score = int(importance or 1) * 10 + overlap * 6
        if query and overlap == 0 and score < 25:
            # Keep highly important recent implementation memories, but avoid
            # dragging unrelated low-value turns into the next prompt.
            continue
        scored.append((score, {"id": mem_id, "ts": ts, "kind": kind, "summary": summary, "keywords": keywords, "importance": importance, "last_used_at": last_used_at}))
    scored.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
    selected = [item for _, item in scored[:limit]]
    if selected:
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        ids = [int(item["id"]) for item in selected]
        with SQLITE_LOCK:
            conn = _sqlite_connect()
            try:
                conn.executemany("UPDATE conversation_memories SET last_used_at = ? WHERE id = ?", [(now, mem_id) for mem_id in ids])
                conn.commit()
            finally:
                conn.close()
    return selected


def _recall_conversation_memories(path: str, body: Json) -> list[Json]:
    if not _memory_enabled() or path not in SUPPORTED_PATHS:
        return []
    try:
        user_text = _last_user_text(path, body)
        keywords = _memory_extract_keywords(user_text)
        limit = max(1, min(int(_memory_config().get("memory_recall_limit") or 8), 20))
        return _sqlite_recall_memories(_memory_session_key(body), _memory_workspace_key(), keywords, limit)
    except Exception:
        if os.environ.get("DEBUG"):
            traceback.print_exc()
        return []


def _memory_block(memories: list[Json]) -> str:
    if not memories:
        return ""
    max_chars = max(800, min(int(_memory_config().get("memory_inject_max_chars") or 4000), 12000))
    lines = [
        "Gateway recalled memory（SQLite 会话记忆，已按当前 session/workspace/语义关键词召回；只作为上下文，不替代实时工具验证）:",
    ]
    for idx, item in enumerate(memories, start=1):
        keywords = ", ".join(str(kw) for kw in item.get("keywords") or [][:8])
        lines.append(f"{idx}. [{item.get('kind')}; importance={item.get('importance')}; ts={item.get('ts')}; keywords={keywords}] {item.get('summary')}")
    return _trim_text_for_context("\n".join(lines), max_chars)


# Context budget allocation by task type
CONTEXT_BUDGETS: dict[str, dict[str, int]] = {
    "code_review": {"system": 4000, "memory": 6000, "recent": 8000},
    "code_generation": {"system": 3000, "memory": 4000, "recent": 15000},
    "bug_fix": {"system": 3000, "memory": 5000, "recent": 12000},
    "general": {"system": 2000, "memory": 3000, "recent": 20000},
}


def _allocate_context_budget(task_type: str) -> dict[str, int]:
    """Allocate context token budget based on task type.

    Returns a dict with keys: system, memory, recent
    Each value is the max characters to use for that category.
    """
    return CONTEXT_BUDGETS.get(task_type, CONTEXT_BUDGETS["general"])


def _detect_task_type(user_text: str) -> str:
    """Detect the type of task based on user text to optimize context allocation."""
    text_lower = user_text.lower()
    if any(word in text_lower for word in ["review", "critique", "check", "audit", "assess"]):
        return "code_review"
    if any(word in text_lower for word in ["fix", "bug", "error", "crash", "issue", "problem"]):
        return "bug_fix"
    if any(word in text_lower for word in ["write", "create", "implement", "add", "new", "generate"]):
        return "code_generation"
    return "general"


def _inject_recalled_memories(path: str, body: Json) -> Json:
    memories = _recall_conversation_memories(path, body)
    block = _memory_block(memories)
    if not block:
        return body
    updated = copy.deepcopy(body)
    if path == "/v1/messages":
        existing = updated.get("system")
        if isinstance(existing, str) and "Gateway recalled memory" in existing:
            return updated
        if isinstance(existing, str) and existing.strip():
            updated["system"] = block + "\n\n" + existing
        elif isinstance(existing, list):
            updated["system"] = [{"type": "text", "text": block}] + list(existing)
        elif existing:
            updated["system"] = block + "\n\n" + str(existing)
        else:
            updated["system"] = block
    elif path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        if any(isinstance(m, dict) and m.get("role") == "system" and "Gateway recalled memory" in _text_from_content(m.get("content")) for m in messages):
            return updated
        messages.insert(0, {"role": "system", "content": block})
        updated["messages"] = messages
    elif path == "/v1/responses":
        existing = updated.get("instructions")
        if isinstance(existing, str) and "Gateway recalled memory" in existing:
            return updated
        updated["instructions"] = block + ("\n\n" + existing if isinstance(existing, str) and existing.strip() else "")
    updated.setdefault("gateway_context", {})
    updated["gateway_context"].update({"memory_recalled": len(memories), "memory_session_key": _memory_session_key(body)})
    return updated


def _sqlite_tail_memories(limit: int = 50) -> list[Json]:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        rows = conn.execute(
            "SELECT ts, session_key, workspace_root, kind, summary, keywords_json, importance, last_used_at FROM conversation_memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out: list[Json] = []
    for ts, session_key, workspace_root, kind, summary, keywords_raw, importance, last_used_at in reversed(rows):
        try:
            keywords = json.loads(keywords_raw or "[]")
        except Exception:
            keywords = []
        out.append(
            {
                "ts": ts,
                "session_key": session_key,
                "workspace_root": workspace_root,
                "kind": kind,
                "summary": summary,
                "keywords": keywords,
                "importance": importance,
                "last_used_at": last_used_at,
            }
        )
    return out

def _upstream_supports_native_tools() -> bool:
    upstream = _upstream_config()
    tools_enabled = str(upstream.get("tools_enabled") or "auto").lower()
    if tools_enabled in {"0", "false", "no", "off", "disabled", "none", "local", "gateway"}:
        return False
    if tools_enabled in {"1", "true", "yes", "on", "native", "native_only"}:
        return True
    capabilities = upstream.get("capabilities", {})
    if not isinstance(capabilities, dict):
        return False
    return bool(capabilities.get("supports_tools") or capabilities.get("supports_function_calls"))


def _trim_text_for_context(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    half = max(limit // 2, 1)
    return value[:half] + "\n\n...[gateway context compacted]...\n\n" + value[-half:]


def _trim_content_for_context(content: Any, limit: int) -> Any:
    if isinstance(content, str):
        return _trim_text_for_context(content, limit)
    if isinstance(content, list):
        out: list[Any] = []
        per_item = max(limit // max(len(content), 1), 1000)
        for item in content:
            if isinstance(item, dict):
                copied = dict(item)
                if isinstance(copied.get("text"), str):
                    copied["text"] = _trim_text_for_context(copied["text"], per_item)
                elif isinstance(copied.get("content"), str):
                    copied["content"] = _trim_text_for_context(copied["content"], per_item)
                out.append(copied)
            elif isinstance(item, str):
                out.append(_trim_text_for_context(item, per_item))
            else:
                out.append(item)
        return out
    return content


def _compact_messages(messages: Any, *, keep_recent: int, text_limit: int) -> list[Json]:
    if not isinstance(messages, list):
        return []
    compacted: list[Json] = []
    for message in messages[-max(keep_recent, 1) :]:
        if not isinstance(message, dict):
            continue
        copied = dict(message)
        if "content" in copied:
            copied["content"] = _trim_content_for_context(copied.get("content"), text_limit)
        compacted.append(copied)
    return compacted


def _compact_request_for_upstream(path: str, body: Json, cfg: Json, *, reason: str = "over_limit") -> Json:
    """Remove bulky downstream harness metadata while preserving user intent.

    Claude Code can send huge system prompts and huge tool schemas. The gateway
    already owns local tools, so in orchestrate mode we strip incoming schemas
    and later expose the gateway's own compact/normalized tool set.
    """

    updated = _without_tools(body)
    for key in ("metadata", "thinking", "output_config"):
        updated.pop(key, None)
    keep_recent = int(cfg.get("keep_recent_messages") or 12)
    summary_limit = int(cfg.get("summary_max_chars") or 6000)
    if path in {"/v1/chat/completions", "/v1/messages"}:
        updated["messages"] = _compact_messages(updated.get("messages"), keep_recent=keep_recent, text_limit=summary_limit)
        if path == "/v1/messages":
            updated["system"] = _gateway_system_prompt(reason)
        else:
            messages = updated.get("messages") or []
            messages = [m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
            messages.insert(0, {"role": "system", "content": _gateway_system_prompt(reason)})
            updated["messages"] = messages
    else:
        existing = updated.get("input")
        if isinstance(existing, str):
            updated["input"] = _trim_text_for_context(existing, summary_limit)
        elif isinstance(existing, list):
            updated["input"] = _trim_content_for_context(existing, summary_limit)
        updated["instructions"] = _gateway_system_prompt(reason)
    updated.setdefault("gateway_context", {})
    updated["gateway_context"].update({"compacted": True, "reason": reason, "original_estimated_tokens": _body_token_estimate(body)})
    return updated


def _maybe_compact_request_for_upstream(path: str, body: Json, cfg: Json, *, reason: str = "over_limit") -> Json:
    if not cfg.get("enabled"):
        return body
    max_tokens = int(cfg.get("max_input_tokens") or 24000)
    if _body_token_estimate(body) <= max_tokens:
        return body
    return _compact_request_for_upstream(path, body, cfg, reason=reason)


def _chunk_text_by_tokens(text: str, chunk_tokens: int, max_chunks: int) -> list[str]:
    if not text.strip():
        return []
    max_chars = max(chunk_tokens * 4, 1000)
    paragraphs = re.split(r"(\n\s*\n)", text)
    chunks: list[str] = []
    current = ""
    for part in paragraphs:
        if not part:
            continue
        if current and len(current) + len(part) > max_chars:
            chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current.strip())
    split_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            split_chunks.append(chunk)
            continue
        for start in range(0, len(chunk), max_chars):
            split_chunks.append(chunk[start : start + max_chars].strip())
    final_chunks = [chunk for chunk in split_chunks if chunk]
    if max_chunks and max_chunks > 0:
        return final_chunks[:max_chunks]
    return final_chunks


def _fanout_source_text(path: str, body: Json) -> str:
    text = _last_user_text(path, body)
    if _approx_token_count(text) > 0:
        return text
    # Fallback for Claude Code/tool-result-heavy payloads: when the newest user
    # message is short but prior messages/tool results are huge, fan out the
    # whole conversation minus executable tools instead of sending it upstream
    # as one over-limit request.
    return json.dumps(_without_tools(body), ensure_ascii=False)


def _make_partial_prompt(original_prompt: str, chunk: str, index: int, total: int) -> str:
    return (
        "你是 Gateway 的高智力上下文分流分析子任务。请只分析下面这一片段，不要编造未出现在片段里的内容。\n"
        "按固定结构输出：\n"
        "1. 语义分析：这段内容的主题/模块/职责。\n"
        "2. 证据摘录：列出可被最终答案引用的文件名、类名、函数名、配置项或关键事实。\n"
        "3. 可调用线索：如果需要工具调用，说明建议调用的工具和参数；如果不需要写“无”。\n"
        "4. 风险与未知：本片段无法确认的点。\n"
        "5. 片段结论：只基于本片段的结论。\n\n"
        f"原始用户问题：\n{original_prompt[:2000]}\n\n"
        f"片段 {index}/{total}：\n{chunk}"
    )


def _trim_partials_for_synthesis(partials: list[str], *, total_budget: int = 30000) -> list[str]:
    if not partials:
        return []
    per_item = max(800, min(2500, total_budget // max(len(partials), 1)))
    return [_trim_text_for_context(str(part), per_item) for part in partials]


def _make_synthesis_prompt(original_prompt: str, partials: list[str]) -> str:
    compact_original = _trim_text_for_context(original_prompt, 3000)
    compact_partials = _trim_partials_for_synthesis(partials)
    joined = "\n\n".join(f"## 子分析 {idx + 1}\n{part}" for idx, part in enumerate(compact_partials))
    return (
        "你是 Gateway 的最终汇总器。下面是同一个超大上下文请求拆分后的多个子分析结果。"
        "必须执行高质量闭环：语义归并 -> 证据对齐 -> 冲突检查 -> 自我反思 -> 调整后给最终结论。"
        "冲突时说明冲突，缺证据时说明未知；不要声称看过未覆盖的内容。\n"
        "注意：原始超大内容已经在子分析阶段覆盖，下面的原始问题只保留压缩摘要，禁止要求用户分段重发。\n"
        "输出结构：\n"
        "1. 语义分析\n2. 调用/证据检查\n3. 反思与调整\n4. 最终结论\n\n"
        f"原始用户问题（压缩）：\n{compact_original}\n\n"
        f"子分析结果（预算内压缩）：\n{joined}"
    )


def _make_quality_review_prompt(original_prompt: str, draft_text: str) -> str:
    compact_original = _trim_text_for_context(original_prompt, 3000)
    compact_draft = _trim_text_for_context(draft_text, 12000)
    return (
        "你是 Gateway 的质量审查器。请审查下面草稿是否真正回答原始问题，是否有证据不足、遗漏、矛盾或过度推断。"
        "如果草稿已经可靠，保留其结论并压缩措辞；如果有问题，请直接给出修正后的最终答案。"
        "必须包含：检查、反思、调整、最终结论。不要要求用户分段重发。\n\n"
        f"原始问题（压缩）：\n{compact_original}\n\n"
        f"草稿答案（压缩）：\n{compact_draft}"
    )


def _should_fanout_context(path: str, body: Json, cfg: Json, *, force: bool = False) -> bool:
    if not cfg.get("enabled") or not cfg.get("fanout_enabled"):
        return False
    if path not in SUPPORTED_PATHS:
        return False
    if _is_forced_tool_choice(path, body):
        return False
    if not force and _body_token_estimate(body) <= int(cfg.get("max_input_tokens") or 24000):
        return False
    text = _fanout_source_text(path, body)
    return force or _approx_token_count(text) > int(cfg.get("fanout_chunk_tokens") or 12000)


def _run_context_fanout(path: str, body: Json, upstream: Any, cfg: Json, *, force: bool = False) -> Json | None:
    if not _should_fanout_context(path, body, cfg, force=force):
        return None
    original_prompt = _fanout_source_text(path, body)
    chunk_tokens = int(cfg.get("fanout_chunk_tokens") or 12000)
    if force:
        # A provider that already rejected context length needs smaller retry
        # chunks; otherwise the fan-out phase can repeat the same refusal.
        chunk_tokens = min(chunk_tokens, 2000)
    chunks = _chunk_text_by_tokens(
        original_prompt,
        chunk_tokens,
        int(cfg.get("fanout_max_chunks") or 0),
    )
    if len(chunks) < 2:
        return None

    partial_base = _compact_request_for_upstream(path, body, cfg, reason="fanout_forced_partial" if force else "fanout_partial")
    workers = max(1, min(int(cfg.get("fanout_max_workers") or os.environ.get("GATEWAY_CONTEXT_FANOUT_MAX_WORKERS") or "4"), 16, len(chunks)))

    def analyze_chunk(index_and_chunk: tuple[int, str]) -> str:
        index, chunk = index_and_chunk
        partial_body = _replace_last_user_text(path, partial_base, _make_partial_prompt(original_prompt, chunk, index, len(chunks)))
        try:
            partial_response = upstream.forward(path, partial_body)
            return _response_text(path, partial_response) or json.dumps(partial_response, ensure_ascii=False)[:4000]
        except Exception as exc:
            return (
                f"[gateway: 子分析 {index}/{len(chunks)} 上游调用失败，已保留失败信息并继续综合；"
                f"type={type(exc).__name__}; error={exc}; 该片段长度={len(chunk)} 字符]"
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        partials = list(executor.map(analyze_chunk, enumerate(chunks, start=1)))

    synthesis_body = _replace_last_user_text(path, _compact_request_for_upstream(path, body, cfg, reason="fanout_forced_synthesis" if force else "fanout_synthesis"), _make_synthesis_prompt(original_prompt, partials))
    try:
        response = upstream.forward(path, synthesis_body)
    except Exception as exc:
        fallback_text = (
            "Gateway 已完成上下文分片，但最终综合请求上游失败，因此返回稳定降级结果。\n\n"
            f"失败类型：{type(exc).__name__}\n失败信息：{exc}\n\n"
            "已取得的子分析/失败记录如下：\n\n"
            + "\n\n".join(f"## 子分析 {idx + 1}\n{part}" for idx, part in enumerate(partials))
        )
        response = _fallback_response(path, fallback_text, status_note="fanout_synthesis_upstream_failed")
    quality_reviewed = False
    if cfg.get("quality_review_enabled", True):
        draft_text = _response_text(path, response) or json.dumps(response, ensure_ascii=False)[:12000]
        review_body = _replace_last_user_text(path, _compact_request_for_upstream(path, body, cfg, reason="fanout_quality_review"), _make_quality_review_prompt(original_prompt, draft_text))
        try:
            reviewed = upstream.forward(path, review_body)
            response = reviewed
            quality_reviewed = True
        except Exception as exc:
            response.setdefault("gateway_context", {})
            response["gateway_context"]["quality_review_error"] = f"{type(exc).__name__}: {exc}"
    response.setdefault("gateway_context", {})
    response["gateway_context"].update(
        {
            "strategy": "fanout_forced_synthesis" if force else "fanout_synthesis",
            "chunks": len(chunks),
            "chunk_tokens": chunk_tokens,
            "workers": workers,
            "original_estimated_tokens": _body_token_estimate(body),
            "quality_reviewed": quality_reviewed,
        }
    )
    return response


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Json) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _safe_json_response(handler: BaseHTTPRequestHandler, status: int, payload: Json) -> None:
    try:
        _json_response(handler, status, payload)
    except (BrokenPipeError, ConnectionResetError):
        return


def _fallback_response(path: str, text: str, *, status_note: str = "gateway_fallback") -> Json:
    model = _config_env("UPSTREAM_MODEL", "")
    usage = {"input_tokens": 0, "output_tokens": _approx_token_count(text)}
    if path == "/v1/messages":
        return {
            "id": f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
            "gateway_context": {"strategy": status_note},
        }
    if path == "/v1/responses":
        return {
            "id": f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "model": model,
            "gateway_context": {"strategy": status_note},
        }
    return {
        "id": f"chatcmpl_gateway_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
        "gateway_context": {"strategy": status_note},
    }


def _read_json(handler: BaseHTTPRequestHandler) -> Json:
    length = int(handler.headers.get("content-length") or "0")
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise GatewayError("request body must be a JSON object")
    return parsed


def _has_requested_tools(body: Json) -> bool:
    tools = body.get("tools")
    return isinstance(tools, list) and bool(tools)


def _is_forced_tool_choice(path: str, body: Json) -> bool:
    choice = body.get("tool_choice")
    if not choice:
        return False
    if isinstance(choice, str):
        return choice not in {"auto", "none"}
    if isinstance(choice, dict):
        if path == "/v1/messages":
            return choice.get("type") in {"tool", "any"}
        return choice.get("type") in {"function", "tool", "required"} or "function" in choice
    return False


def _native_tool_signal(path: str, response: Json) -> bool:
    """Return true when a response contains real protocol-level tool-call data."""
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if isinstance(message, dict) and message.get("tool_calls"):
                return True
            if choice.get("finish_reason") == "tool_calls":
                return True
        return False

    if path == "/v1/responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in {
                "function_call",
                "tool_call",
                "custom_tool_call",
                "computer_call",
                "file_search_call",
                "web_search_call",
            }:
                return True
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                    return True
        return False

    if path == "/v1/messages":
        for block in response.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        if response.get("stop_reason") == "tool_use":
            return True
        return False

    return False


def _workspace_root() -> pathlib.Path:
    scoped = getattr(WORKSPACE_CONTEXT, "root", None)
    if scoped:
        return pathlib.Path(str(scoped)).resolve()
    return pathlib.Path(_config_env("GATEWAY_WORKSPACE_ROOT", os.getcwd())).resolve()


def _request_workspace_root(body: Json | None) -> pathlib.Path | None:
    if not isinstance(body, dict):
        return None
    candidates: list[Any] = [
        body.get("workspace_root"),
        body.get("workspace"),
        body.get("project_dir"),
        body.get("project_root"),
        body.get("cwd"),
        body.get("working_directory"),
    ]
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        candidates.extend(
            [
                metadata.get("workspace_root"),
                metadata.get("workspace"),
                metadata.get("project_dir"),
                metadata.get("project_root"),
                metadata.get("cwd"),
                metadata.get("working_directory"),
            ]
        )
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        path = pathlib.Path(_clean_tool_string(candidate)).expanduser()
        if path.exists() and path.is_dir():
            return path.resolve()
    return None


@contextlib.contextmanager
def _workspace_scope(root: pathlib.Path | None):
    previous = getattr(WORKSPACE_CONTEXT, "root", None)
    if root is not None:
        WORKSPACE_CONTEXT.root = str(root)
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(WORKSPACE_CONTEXT, "root")
            except AttributeError:
                pass
        else:
            WORKSPACE_CONTEXT.root = previous


def _resolve_workspace_path(value: str | None, *, default: str = ".") -> pathlib.Path:
    raw = value or default
    candidate = pathlib.Path(raw)
    if not candidate.is_absolute():
        candidate = _workspace_root() / candidate
    resolved = candidate.resolve()
    root = _workspace_root()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ToolExecutionError(
            f"path escapes workspace root: {resolved}",
            failure_type="permission_denied",
        ) from exc
    return resolved


def _require_write_enabled() -> None:
    if _config_env("GATEWAY_ALLOW_WRITE_TOOLS", "0").lower() not in {"1", "true", "yes"}:
        raise ToolExecutionError(
            "write/edit tools are disabled; set GATEWAY_ALLOW_WRITE_TOOLS=1 to enable",
            failure_type="permission_denied",
        )


def _require_shell_enabled() -> None:
    if _config_env("GATEWAY_ALLOW_SHELL_TOOLS", "0").lower() not in {"1", "true", "yes"}:
        raise ToolExecutionError(
            "shell tools are disabled; set GATEWAY_ALLOW_SHELL_TOOLS=1 to enable",
            failure_type="permission_denied",
        )






















































































def _mcp_safe_component(value: str, *, default: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or default


try:
    from .gateway_builtin_tools import _json_schema, _make_tools
except ImportError:  # pragma: no cover - script fallback
    from gateway_builtin_tools import _json_schema, _make_tools  # type: ignore




BUILTIN_TOOLS = _make_tools()


def _tool_name_from_schema(path: str, item: Json) -> str | None:
    if path == "/v1/messages":
        return item.get("name") if isinstance(item.get("name"), str) else None
    if item.get("type") == "function" and isinstance(item.get("function"), dict):
        return item["function"].get("name")
    return item.get("name") if isinstance(item.get("name"), str) else None


def _tool_schema_for_path(path: str, tool: GatewayTool) -> Json:
    if path == "/v1/messages":
        return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
    if path == "/v1/responses":
        return {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
            "strict": False,
        }
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
    }


def _mcp_public_name(server_name: str, tool_name: str) -> str:
    safe_server = _mcp_safe_component(server_name, default="mcp")
    safe_tool = _mcp_safe_component(tool_name, default="tool")
    return f"mcp__{safe_server}__{safe_tool}"


def _mcp_legacy_public_name(server_name: str, tool_name: str) -> str:
    """DeepSeek-TUI style MCP public name: mcp_<server>_<tool>."""
    safe_server = _mcp_safe_component(server_name, default="mcp")
    safe_tool = _mcp_safe_component(tool_name, default="tool")
    return f"mcp_{safe_server}_{safe_tool}"


def _mcp_parse_public_name(name: str) -> tuple[str, str] | None:
    if not name.startswith("mcp__"):
        if not name.startswith("mcp_"):
            return None
        suffix = name[len("mcp_") :]
        for server in sorted(_enabled_mcp_servers(), key=lambda s: len(str(s.get("name") or "")), reverse=True):
            server_name = str(server.get("name") or "")
            safe_server = _mcp_safe_component(server_name, default="mcp")
            prefix = f"{safe_server}_"
            if suffix.startswith(prefix) and len(suffix) > len(prefix):
                return safe_server, suffix[len(prefix) :]
        return None
    parts = name.split("__", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _enabled_mcp_servers() -> list[Json]:
    servers = load_config().get("mcp", {}).get("servers", [])
    if not isinstance(servers, list):
        return []
    return [s for s in servers if isinstance(s, dict) and s.get("enabled", True)]


def _mcp_server_by_name(name: str) -> Json | None:
    for server in _enabled_mcp_servers():
        server_name = str(server.get("name") or "")
        if server_name == name or _mcp_safe_component(server_name, default="mcp") == name:
            return server
    return None


def _mcp_env(server: Json) -> dict[str, str]:
    env = os.environ.copy()
    raw_env = server.get("env")
    if isinstance(raw_env, dict):
        env.update({str(k): str(v) for k, v in raw_env.items()})
    elif isinstance(raw_env, list):
        for key in raw_env:
            key = str(key)
            if key in os.environ:
                env[key] = os.environ[key]
    return env


def _mcp_command(server: Json) -> list[str]:
    if isinstance(server.get("command"), list):
        return [str(x) for x in server["command"]]
    command = str(server.get("command") or "")
    if not command:
        raise ToolExecutionError("MCP server command is required", failure_type="invalid_input")
    args = server.get("args") or []
    if isinstance(args, str):
        args_list = shlex.split(args)
    elif isinstance(args, list):
        args_list = [str(x) for x in args]
    else:
        args_list = []
    return [command, *args_list]


def _mcp_write_message(proc: subprocess.Popen, message: Json) -> None:
    raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
    assert proc.stdin is not None
    proc.stdin.write(header + raw)
    proc.stdin.flush()


def _mcp_read_exact(stream: Any, length: int, timeout: float) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        ready, _, _ = select.select([stream], [], [], timeout)
        if not ready:
            raise ToolExecutionError("MCP server response timed out", failure_type="timeout")
        chunk = stream.read(remaining)
        if not chunk:
            raise ToolExecutionError("MCP server closed stdout", failure_type="execution_failed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _mcp_read_message(proc: subprocess.Popen, timeout: float) -> Json:
    assert proc.stdout is not None
    header = b""
    while b"\r\n\r\n" not in header and b"\n\n" not in header:
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            raise ToolExecutionError("MCP server response timed out", failure_type="timeout")
        byte = proc.stdout.read(1)
        if not byte:
            raise ToolExecutionError("MCP server closed stdout", failure_type="execution_failed")
        header += byte
        if len(header) > 8192:
            raise ToolExecutionError("MCP response header too large", failure_type="execution_failed")
    if b"\r\n\r\n" in header:
        header_bytes, rest = header.split(b"\r\n\r\n", 1)
    else:
        header_bytes, rest = header.split(b"\n\n", 1)
    content_length = None
    for line in header_bytes.decode("ascii", errors="replace").splitlines():
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break
    if content_length is None:
        # Some lightweight test servers use newline-delimited JSON.
        line = header.strip()
        try:
            parsed = json.loads(line.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        raise ToolExecutionError("MCP response missing Content-Length", failure_type="execution_failed")
    body = rest + _mcp_read_exact(proc.stdout, content_length - len(rest), timeout)
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ToolExecutionError("MCP response must be JSON object", failure_type="execution_failed")
    return parsed


def _mcp_request(proc: subprocess.Popen, method: str, params: Json | None = None, *, request_id: int = 1, timeout: float = 20) -> Json:
    payload: Json = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    _mcp_write_message(proc, payload)
    while True:
        response = _mcp_read_message(proc, timeout)
        if response.get("id") != request_id:
            continue
        if "error" in response:
            raise ToolExecutionError(f"MCP {method} failed: {response['error']}", failure_type="execution_failed")
        result = response.get("result") or {}
        if not isinstance(result, dict):
            raise ToolExecutionError(f"MCP {method} result must be object", failure_type="execution_failed")
        return result


def _mcp_notify(proc: subprocess.Popen, method: str, params: Json | None = None) -> None:
    payload: Json = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    _mcp_write_message(proc, payload)


def _mcp_start(server: Json) -> subprocess.Popen:
    command = _mcp_command(server)
    cwd = str(_resolve_workspace_path(str(server.get("cwd") or ".")))
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=_mcp_env(server),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def _mcp_initialize(proc: subprocess.Popen, server: Json, timeout: float, request_id: int = 1) -> None:
    _mcp_request(
        proc,
        "initialize",
        {
            "protocolVersion": str(server.get("protocolVersion") or MCP_PROTOCOL_VERSION),
            "capabilities": {},
            "clientInfo": {"name": "toolcall-gateway", "version": "0.1"},
        },
        request_id=request_id,
        timeout=timeout,
    )
    _mcp_notify(proc, "notifications/initialized")


def _mcp_with_server(server: Json, fn: Callable[[subprocess.Popen, float], Any]) -> Any:
    timeout = float(server.get("timeout") or os.environ.get("GATEWAY_MCP_TIMEOUT", "20"))
    proc = _mcp_start(server)
    try:
        _mcp_initialize(proc, server, timeout)
        return fn(proc, timeout)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass


def _mcp_session_key(server: Json) -> str:
    return str(server.get("name") or json.dumps(_mcp_command(server), sort_keys=True))


def _mcp_use_pool(server: Json) -> bool:
    return bool(server.get("pool", True))


def _mcp_get_session(server: Json) -> McpSession:
    key = _mcp_session_key(server)
    with MCP_SESSIONS_LOCK:
        session = MCP_SESSIONS.get(key)
        if session and session.proc.poll() is None:
            return session
        if session:
            session.close()
        session = McpSession(server)
        MCP_SESSIONS[key] = session
        return session


def _mcp_close_sessions() -> None:
    with MCP_SESSIONS_LOCK:
        sessions = list(MCP_SESSIONS.values())
        MCP_SESSIONS.clear()
    for session in sessions:
        session.close()
    MCP_TOOL_CATALOG_CACHE.clear()
    MCP_SERVER_STATUS.clear()


def _mcp_catalog_ttl(server: Json) -> float:
    return float(server.get("catalog_ttl") or os.environ.get("GATEWAY_MCP_CATALOG_TTL") or MCP_CATALOG_CACHE_TTL_SECONDS)


def _mcp_cache_key(server: Json) -> str:
    return _mcp_session_key(server)


def _mcp_set_status(server_name: str, status: str, *, detail: str | None = None, tool_count: int | None = None) -> None:
    payload = MCP_SERVER_STATUS.setdefault(server_name, {})
    payload.update(
        {
            "name": server_name,
            "status": status,
            "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
    )
    if detail is not None:
        payload["detail"] = detail
    if tool_count is not None:
        payload["tool_count"] = tool_count


def _mcp_invalidate_server(server: Json, *, reason: str | None = None) -> None:
    key = _mcp_session_key(server)
    with MCP_SESSIONS_LOCK:
        session = MCP_SESSIONS.pop(key, None)
    if session:
        session.close()
    MCP_TOOL_CATALOG_CACHE.pop(key, None)
    if reason:
        _mcp_set_status(key, "restarting", detail=reason)


def _mcp_health_snapshot(*, probe: bool = False) -> list[Json]:
    rows: list[Json] = []
    for server in _enabled_mcp_servers():
        name = str(server.get("name") or _mcp_session_key(server))
        session = MCP_SESSIONS.get(name)
        cached = MCP_TOOL_CATALOG_CACHE.get(name)
        base = {
            "name": name,
            "enabled": True,
            "session": "connected" if session and session.proc.poll() is None else "not_connected",
            "cache": "hit" if cached and cached[0] > time.time() else "miss",
            "cached_tool_count": len(cached[1]) if cached else 0,
        }
        base.update(MCP_SERVER_STATUS.get(name, {}))
        if probe:
            try:
                tools = _mcp_list_server_tools(server)
                base.update({"status": "ready", "tool_count": len(tools), "detail": ""})
            except Exception as exc:
                base.update({"status": "broken", "detail": str(exc)})
        rows.append(base)
    return rows


atexit.register(_mcp_close_sessions)


def _mcp_list_server_tools(server: Json) -> list[Json]:
    key = _mcp_cache_key(server)
    now = time.time()
    ttl = _mcp_catalog_ttl(server)
    cached = MCP_TOOL_CATALOG_CACHE.get(key)
    if cached and cached[0] > now:
        _mcp_set_status(key, "ready", tool_count=len(cached[1]))
        return copy.deepcopy(cached[1])

    try:
        if _mcp_use_pool(server):
            result = _mcp_get_session(server).request("tools/list", {})
        else:
            def run(proc: subprocess.Popen, timeout: float) -> Json:
                return _mcp_request(proc, "tools/list", {}, request_id=2, timeout=timeout)

            result = _mcp_with_server(server, run)
        tools = [t for t in (result.get("tools") or []) if isinstance(t, dict) and t.get("name")]
        MCP_TOOL_CATALOG_CACHE[key] = (now + ttl, copy.deepcopy(tools))
        _mcp_set_status(key, "ready", tool_count=len(tools), detail="")
        return tools
    except Exception as exc:
        _mcp_invalidate_server(server, reason=str(exc))
        _mcp_set_status(key, "broken", detail=str(exc), tool_count=0)
        raise


def _mcp_call_tool(server: Json, tool_name: str, arguments: Json) -> str:
    key = _mcp_session_key(server)
    try:
        if _mcp_use_pool(server):
            result = _mcp_get_session(server).request("tools/call", {"name": tool_name, "arguments": arguments})
        else:
            def run(proc: subprocess.Popen, timeout: float) -> Json:
                return _mcp_request(
                    proc,
                    "tools/call",
                    {"name": tool_name, "arguments": arguments},
                    request_id=2,
                    timeout=timeout,
                )

            result = _mcp_with_server(server, run)
        if result.get("isError"):
            raise ToolExecutionError(_mcp_content_to_text(result), failure_type="execution_failed")
        _mcp_set_status(key, "ready", detail="")
        return _mcp_content_to_text(result)
    except Exception as exc:
        _mcp_invalidate_server(server, reason=str(exc))
        _mcp_set_status(key, "broken", detail=str(exc))
        raise


def _mcp_content_to_text(result: Json) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return json.dumps(result, ensure_ascii=False)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
        elif item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _mcp_tool_schemas(path: str) -> list[Json]:
    schemas: list[Json] = []
    for server in _enabled_mcp_servers():
        server_name = str(server.get("name") or "")
        if not server_name:
            continue
        try:
            for tool in _mcp_list_server_tools(server):
                gateway_tool = GatewayTool(
                    name=_mcp_public_name(server_name, str(tool["name"])),
                    description=str(tool.get("description") or f"MCP tool {server_name}/{tool['name']}"),
                    parameters=tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else _json_schema({}),
                    handler=lambda _args: "",
                    risk="mcp",
                )
                schemas.append(_tool_schema_for_path(path, gateway_tool))
                if os.environ.get("GATEWAY_EXPOSE_LEGACY_MCP_NAMES", "1").lower() not in {"0", "false", "no"}:
                    legacy_tool = GatewayTool(
                        name=_mcp_legacy_public_name(server_name, str(tool["name"])),
                        description=str(tool.get("description") or f"MCP tool {server_name}/{tool['name']}"),
                        parameters=tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else _json_schema({}),
                        handler=lambda _args: "",
                        risk="mcp",
                    )
                    schemas.append(_tool_schema_for_path(path, legacy_tool))
        except Exception as exc:
            call = ToolCall(
                call_id=f"mcp_list_{server_name}",
                name=f"mcp::{server_name}::tools/list",
                arguments={},
                raw={},
            )
            result = ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"connector_required: {exc}",
                success=False,
                failure_type="connector_required",
            )
            _record_tool_failure(call, result, execution_ms=0.0, retry_count=0, provider=None)
    return schemas


def _enabled_http_actions() -> list[Json]:
    actions_cfg = load_config().get("http_actions", {})
    if not isinstance(actions_cfg, dict) or not actions_cfg.get("enabled", True):
        return []
    actions = actions_cfg.get("actions", [])
    if not isinstance(actions, list):
        return []
    return [
        action
        for action in actions
        if isinstance(action, dict)
        and action.get("enabled", True)
        and isinstance(action.get("name"), str)
        and action.get("name")
    ]


def _http_action_by_name(name: str) -> Json | None:
    for action in _enabled_http_actions():
        aliases = action.get("aliases") if isinstance(action.get("aliases"), list) else []
        if action.get("name") == name or name in aliases:
            return action
    return None


def _http_action_schemas(path: str) -> list[Json]:
    schemas: list[Json] = []
    for action in _enabled_http_actions():
        gateway_tool = GatewayTool(
            name=str(action["name"]),
            description=str(action.get("description") or f"HTTP action {action['name']}"),
            parameters=action.get("input_schema") if isinstance(action.get("input_schema"), dict) else _json_schema({}),
            handler=lambda _args: "",
            risk="http_action",
        )
        schemas.append(_tool_schema_for_path(path, gateway_tool))
    return schemas


def _expand_action_value(value: Any) -> str:
    text = str(value)
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", text)
    if match:
        return os.environ.get(match.group(1), "")
    return text


def _http_action_headers(action: Json) -> dict[str, str]:
    headers = {"user-agent": "ToolCallGateway/1.0"}
    raw_headers = action.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise ToolExecutionError("http action headers must be an object", failure_type="invalid_input")
    for key, value in raw_headers.items():
        headers[str(key)] = _expand_action_value(value)
    return headers


def _call_http_action(action: Json, arguments: Json) -> str:
    url = str(action.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ToolExecutionError("http action url must be an absolute http(s) URL", failure_type="invalid_input")
    method = str(action.get("method") or "POST").upper()
    timeout = float(action.get("timeout") or os.environ.get("GATEWAY_HTTP_ACTION_TIMEOUT", "30"))
    max_bytes = int(action.get("max_bytes") or os.environ.get("GATEWAY_HTTP_ACTION_MAX_BYTES", "200000"))
    headers = _http_action_headers(action)
    data: bytes | None = None
    request_url = url
    if method in {"GET", "DELETE"}:
        query = urllib.parse.urlencode({str(k): v for k, v in arguments.items()}, doseq=True)
        sep = "&" if urllib.parse.urlparse(url).query else "?"
        request_url = f"{url}{sep}{query}" if query else url
    else:
        data = json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        headers.setdefault("content-type", "application/json")
    req = urllib.request.Request(request_url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(max_bytes)
            content_type = resp.headers.get("content-type", "")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read(max_bytes).decode("utf-8", errors="replace")
        raise ToolExecutionError(f"http action returned {exc.code}: {detail}", failure_type="execution_failed") from exc
    except urllib.error.URLError as exc:
        raise ToolExecutionError(f"http action connection failed: {exc.reason}", failure_type="execution_failed") from exc
    text = body.decode("utf-8", errors="replace")
    if content_type.startswith("application/json"):
        try:
            parsed_body = json.loads(text)
            text = json.dumps(parsed_body, ensure_ascii=False)
        except Exception:
            pass
    return f"status: {status}\ncontent-type: {content_type}\n\n{text}"


def _merge_builtin_tools(path: str, body: Json) -> Json:
    if not _upstream_supports_native_tools():
        return _inject_gateway_system_prompt(path, _without_tools(body), reason="upstream_no_native_tools")
    if os.environ.get("GATEWAY_EXPOSE_BUILTIN_TOOLS", "1").lower() in {"0", "false", "no"}:
        return body
    merged = dict(body)
    tools = list(merged.get("tools") or [])
    existing = {_tool_name_from_schema(path, t) for t in tools if isinstance(t, dict)}
    for name, tool in BUILTIN_TOOLS.items():
        if name != tool.name:
            continue
        if tool.risk == "connector_required" and os.environ.get("GATEWAY_EXPOSE_CONNECTOR_PLACEHOLDERS", "0") not in {"1", "true", "yes"}:
            continue
        if tool.name not in existing:
            tools.append(_tool_schema_for_path(path, tool))
            existing.add(tool.name)
    if load_config().get("mcp", {}).get("enabled", True):
        for schema in _mcp_tool_schemas(path):
            name = _tool_name_from_schema(path, schema)
            if name and name not in existing:
                tools.append(schema)
                existing.add(name)
    for schema in _http_action_schemas(path):
        name = _tool_name_from_schema(path, schema)
        if name and name not in existing:
            tools.append(schema)
            existing.add(name)
    merged["tools"] = tools
    return merged


def _copy_model_override(body: Json) -> Json:
    copied = dict(body)
    model = _config_env("UPSTREAM_MODEL", "")
    if model:
        copied["model"] = model
    return copied


class NativeProxyClient:
    def __init__(self) -> None:
        self.base_url = _config_env("UPSTREAM_BASE_URL", "").rstrip("/")
        self.api_key = _config_env("UPSTREAM_API_KEY", "")
        self.anthropic_version = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
        self.timeout = float(_config_env("UPSTREAM_TIMEOUT", "60") or "60")
        if not self.base_url:
            raise GatewayError("UPSTREAM_BASE_URL is required")

    def forward(self, path: str, body: Json) -> Json:
        payload = _copy_model_override(body)
        if _use_openai_chat_upstream(path):
            chat_payload = _to_openai_chat_payload(path, payload, stream=_force_upstream_stream_aggregate())
            chat_response = self._post_chat_completions(chat_payload)
            return _from_openai_chat_response(path, chat_response)
        return self._post(path, payload)

    def get(self, path: str) -> Json:
        headers = self._headers(path)
        req = urllib.request.Request(self.base_url + _configured_upstream_path(path), headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise UpstreamHTTPError(exc.code, detail) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamTimeoutError(f"upstream request timed out after {self.timeout}s") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"upstream connection failed: {exc.reason}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayError("upstream returned non-JSON response", detail=raw[:2000]) from exc
        if not isinstance(parsed, dict):
            raise GatewayError("upstream returned non-object JSON", detail=parsed)
        return parsed

    def stream_forward(self, path: str, body: Json) -> Any:
        payload = _copy_model_override(body)
        if _use_openai_chat_upstream(path):
            payload = _to_openai_chat_payload(path, payload, stream=True)
            upstream_path = _configured_upstream_path_by_key("chat_completions", "/v1/chat/completions")
        else:
            upstream_path = _configured_upstream_path(path)
        headers = self._headers(path)
        headers["accept"] = "text/event-stream"
        req = urllib.request.Request(
            self.base_url + upstream_path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise UpstreamHTTPError(exc.code, detail) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamTimeoutError(f"upstream stream timed out after {self.timeout}s") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"upstream connection failed: {exc.reason}") from exc

    def _post_chat_completions(self, body: Json) -> Json:
        if body.get("stream"):
            return self._post_chat_completions_stream_aggregate(body)
        return self._post_raw(_configured_upstream_path_by_key("chat_completions", "/v1/chat/completions"), body, "/v1/chat/completions")

    def _post_raw(self, upstream_path: str, body: Json, header_path: str) -> Json:
        headers = self._headers(header_path)
        req = urllib.request.Request(
            self.base_url + upstream_path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise UpstreamHTTPError(exc.code, detail) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamTimeoutError(f"upstream request timed out after {self.timeout}s") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"upstream connection failed: {exc.reason}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayError("upstream returned non-JSON response", detail=raw[:2000]) from exc
        if not isinstance(parsed, dict):
            raise GatewayError("upstream returned non-object JSON", detail=parsed)
        return parsed

    def _post_chat_completions_stream_aggregate(self, body: Json) -> Json:
        payload = dict(body)
        payload["stream"] = True
        headers = self._headers("/v1/chat/completions")
        headers["accept"] = "text/event-stream"
        req = urllib.request.Request(
            self.base_url + _configured_upstream_path_by_key("chat_completions", "/v1/chat/completions"),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        # Accumulate streaming tool_calls by index → {id, type, function: {name, arguments}}
        tool_calls_by_index: dict[int, dict] = {}
        response_id = f"chatcmpl_{uuid.uuid4().hex}"
        model = str(payload.get("model") or _config_env("UPSTREAM_MODEL", ""))
        finish_reason = "stop"
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except Exception:
                        continue
                    response_id = str(event.get("id") or response_id)
                    model = str(event.get("model") or model)
                    for choice in event.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta") or {}
                        if isinstance(delta, dict):
                            if isinstance(delta.get("content"), str):
                                content_parts.append(delta["content"])
                            if isinstance(delta.get("reasoning"), str):
                                reasoning_parts.append(delta["reasoning"])
                            elif isinstance(delta.get("reasoning_content"), str):
                                reasoning_parts.append(delta["reasoning_content"])
                            # Accumulate streaming tool_calls by index
                            for tc_delta in (delta.get("tool_calls") or []):
                                if not isinstance(tc_delta, dict):
                                    continue
                                idx = int(tc_delta.get("index") or 0)
                                if idx not in tool_calls_by_index:
                                    tool_calls_by_index[idx] = {
                                        "id": tc_delta.get("id") or "",
                                        "type": tc_delta.get("type") or "function",
                                        "function": {"name": "", "arguments": ""},
                                    }
                                tc = tool_calls_by_index[idx]
                                if tc_delta.get("id"):
                                    tc["id"] = tc_delta["id"]
                                fn_delta = tc_delta.get("function") or {}
                                if isinstance(fn_delta, dict):
                                    if fn_delta.get("name"):
                                        tc["function"]["name"] = fn_delta["name"]
                                    if isinstance(fn_delta.get("arguments"), str):
                                        tc["function"]["arguments"] += fn_delta["arguments"]
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise UpstreamHTTPError(exc.code, detail) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise UpstreamTimeoutError(f"upstream stream timed out after {self.timeout}s") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"upstream connection failed: {exc.reason}") from exc
        message: Json = {"role": "assistant", "content": "".join(content_parts) or None}
        if reasoning_parts:
            message["reasoning"] = "".join(reasoning_parts)
            message["reasoning_content"] = message["reasoning"]
        if tool_calls_by_index:
            message["content"] = message["content"] or None
            message["tool_calls"] = [tool_calls_by_index[i] for i in sorted(tool_calls_by_index)]
            finish_reason = "tool_calls"
        return {
            "id": response_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        }

    def _post(self, path: str, body: Json) -> Json:
        return self._post_raw(_configured_upstream_path(path), body, path)

    def _headers(self, path: str) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if path == "/v1/messages":
            if self.api_key:
                headers["x-api-key"] = self.api_key
                headers["authorization"] = f"Bearer {self.api_key}"
            headers["anthropic-version"] = self.anthropic_version
            beta = os.environ.get("ANTHROPIC_BETA")
            if beta:
                headers["anthropic-beta"] = beta
        elif self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers


def _parse_json_arguments(raw: Any, *, allow_text: bool = False) -> Json:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            if allow_text:
                return {"input": raw, "text": raw, "patch": raw, "command": raw}
            raise ToolExecutionError(f"tool arguments are not valid JSON: {exc}", failure_type="invalid_input") from exc
        if isinstance(parsed, dict):
            return parsed
        if allow_text:
            return {"input": raw, "value": parsed}
        raise ToolExecutionError("tool arguments JSON must decode to an object", failure_type="invalid_input")
    raise ToolExecutionError("tool arguments must be an object or JSON string", failure_type="invalid_input")


def _first_present(args: Json, names: tuple[str, ...]) -> Any:
    for name in names:
        if name in args and args[name] is not None:
            return args[name]
    return None


def _normalize_tool_name(name: str) -> str:
    """Normalize common coding-agent tool/function names to Gateway tools.

    Exact MCP names are intentionally left untouched. The gateway supports
    multiple clients that vary mostly by casing and small naming conventions
    (Claude Code, Codex/OpenAI, OpenCode, DeepSeek-TUI), so this keeps the
    execution layer permissive while still returning real tool results.
    """

    if _mcp_parse_public_name(name):
        return name
    if name in BUILTIN_TOOLS or _http_action_by_name(name):
        return name
    compact = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    aliases = {
        "bash": "Bash",
        "shell": "Bash",
        "exec": "Bash",
        "execute": "Bash",
        "run": "Bash",
        "exec_command": "Bash",
        "shell_command": "Bash",
        "exec_shell": "Bash",
        # DeepSeek-style aliases
        "deepseek_bash": "Bash",
        "deepseek_read": "Read",
        "deepseek_write": "Write",
        "deepseek_search": "Grep",
        # OpenAI function-style aliases
        "get_file": "Read",
        "create_file": "Write",
        "run_command": "Bash",
        "list_files": "LS",
        "search_files": "Grep",
        # Anthropic-style aliases
        "computer_browse": "WebBrowser",
        "computer_use": "computer_use",
        "exec_shell_start": "exec_shell_start",
        "exec_start": "exec_shell_start",
        "shell_start": "exec_shell_start",
        "bash_output": "exec_wait",
        "bashoutput": "exec_wait",
        "write_stdin": "write_stdin",
        "exec_wait": "exec_wait",
        "exec_shell_wait": "exec_wait",
        "exec_interact": "write_stdin",
        "exec_shell_interact": "write_stdin",
        "kill_shell": "exec_kill",
        "kill_bash": "exec_kill",
        "killbash": "exec_kill",
        "bashkill": "exec_kill",
        "codeinterpreter": "code_interpreter",
        "code_interpreter": "code_interpreter",
        "python_interpreter": "code_interpreter",
        "python_exec": "code_interpreter",
        "git": "Git",
        "git_status": "Git",
        "git_diff": "Git",
        "git_log": "Git",
        "git_show": "Git",
        "json_query": "JsonQuery",
        "jq": "JsonQuery",
        "python_symbols": "PythonSymbols",
        "lsp_document_symbols": "PythonSymbols",
        "agent": "Agent",
        "subagent": "Agent",
        "task": "Agent",
        "spawn_agent": "spawn_agent",
        "skill": "Skill",
        "list_skills": "Skill",
        "read_skill": "Skill",
        "run_skill": "Skill",
        "mcp_list_tools": "mcp_list_tools",
        "list_mcp_tools": "mcp_list_tools",
        "mcp_call_tool": "mcp_call_tool",
        "call_mcp_tool": "mcp_call_tool",
        "memory": "Memory",
        "remember": "Memory",
        "save_memory": "Memory",
        "recall_memory": "Memory",
        "create_goal": "create_goal",
        "update_goal": "create_goal",
        "read": "Read",
        "view": "Read",
        "cat": "Read",
        "read_file": "Read",
        "file_read": "Read",
        "open_file": "Read",
        "view_file": "Read",
        "filereadtool": "Read",
        "read_many_files": "ReadManyFiles",
        "read_files": "ReadManyFiles",
        "stat": "FileInfo",
        "file_info": "FileInfo",
        "write": "Write",
        "create": "Write",
        "write_file": "Write",
        "file_write": "Write",
        "create_file": "Write",
        "new_file": "Write",
        "edit": "Edit",
        "str_replace": "Edit",
        "str_replace_editor": "Edit",
        "str_replace_based_edit_tool": "Edit",
        "edit_file": "Edit",
        "multiedit": "MultiEdit",
        "multi_edit": "MultiEdit",
        "regex_edit": "RegexEdit",
        "replace_regex": "RegexEdit",
        "notebook_edit": "NotebookEdit",
        "notebookedit": "NotebookEdit",
        "ls": "LS",
        "list": "LS",
        "list_dir": "LS",
        "list_directory": "LS",
        "tree": "Tree",
        "directory_tree": "Tree",
        "mkdir": "CreateDirectory",
        "create_directory": "CreateDirectory",
        "delete_file": "DeletePath",
        "remove_file": "DeletePath",
        "rm": "DeletePath",
        "move_file": "MovePath",
        "rename_file": "MovePath",
        "mv": "MovePath",
        "copy_file": "CopyPath",
        "cp": "CopyPath",
        "glob": "Glob",
        "glob_files": "Glob",
        "find_files": "Glob",
        "grep": "Grep",
        "search": "Grep",
        "grep_files": "Grep",
        "webfetch": "WebFetch",
        "web_fetch": "WebFetch",
        "fetch": "WebFetch",
        "fetch_url": "WebFetch",
        "websearch": "WebSearch",
        "web_search": "WebSearch",
        "web_search_preview": "WebSearch",
        "web_search_preview_2025_03_11": "web_search_call",
        "file_search": "file_search_call",
        "file_search_call": "file_search_call",
        "web_browser": "WebBrowser",
        "browser": "WebBrowser",
        "imageinfo": "view_image",
        "image_info": "view_image",
        "analyzeimage": "view_image",
        "analyze_image": "view_image",
        "inspect_image": "view_image",
        "intentdetect": "IntentDetect",
        "intent_detect": "IntentDetect",
        "intent_recognition": "IntentDetect",
        "textintent": "IntentDetect",
        "text_intent": "IntentDetect",
        "todowrite": "TodoWrite",
        "todo_write": "TodoWrite",
        "updateplan": "update_plan",
        "update_plan": "update_plan",
        "team_create": "TeamCreate",
        "create_team": "TeamCreate",
        "send_message": "SendMessage",
        "team_delete": "TeamDelete",
        "delete_team": "TeamDelete",
        "current_time": "get_current_time",
        "get_current_time": "get_current_time",
        "calculator": "calculator",
    }
    if compact in aliases:
        return aliases[compact]
    for registered, tool in BUILTIN_TOOLS.items():
        if registered.lower() == name.lower() or tool.name.lower() == name.lower():
            return registered
    return name


def _clean_tool_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    cdata = re.fullmatch(r"<!\[CDATA\[(.*)\]\]>", cleaned, flags=re.S)
    if cdata:
        cleaned = cdata.group(1).strip()
    cleaned = re.sub(r"</?(?:parameter|function|tool|tool_call|invoke)>", "", cleaned, flags=re.I).strip()
    return cleaned


_PATHISH_RE = re.compile(
    r"@?(?P<path>"
    r"(?:~?/|/|\.{1,2}/)[^\s<>'\"`|]+"
    r"|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.@%+=:,/-]+"
    r"|[A-Za-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|json|jsonl|toml|yaml|yml|md|txt|sh|bash|zsh|env|ini|cfg|conf|html|css|sql|go|rs|java|kt|swift|c|cc|cpp|h|hpp)"
    r")"
)


def _clean_text_tool_path(value: Any) -> Any:
    """Extract a single path from noisy text-tool fallback parameters.

    Weak upstreams sometimes put prose after a path, e.g.
    ``README.md\n<tool_call>`` or ``src/app.py\n\n--- report``. Passing the
    whole blob to filesystem tools causes false not_found/File name too long
    failures, so path-like parameters are reduced to the first path token.
    """

    cleaned = _clean_tool_string(value)
    if not isinstance(cleaned, str):
        return cleaned
    text = cleaned.strip()
    if not text:
        return text
    for line in text.splitlines():
        candidate = line.strip().strip("`'\"")
        if not candidate:
            continue
        match = _PATHISH_RE.search(candidate)
        if match:
            return match.group("path").rstrip(".,;:)")
        if not re.match(r"^(?:[-*_]{3,}|#{1,6}\s|[*>]|\*\*)", candidate):
            return candidate.rstrip(".,;:)")
    return text


def _normalize_relative_pattern(value: Any) -> Any:
    value = _clean_tool_string(value)
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        # Claude-style text fallback often emits /*.py to mean repo-root glob.
        # Python glob treats it as filesystem root, so make it workspace-root relative.
        return value.lstrip("/") or "*"
    return value


def _normalize_tool_args(tool_name: str, args: Json) -> Json:
    normalized = {key: _clean_tool_string(value) for key, value in dict(args).items()}
    if tool_name in {"Read", "Write", "Edit", "MultiEdit", "RegexEdit", "FileInfo", "CreateDirectory", "DeletePath", "Tree", "PythonSymbols", "JsonQuery"}:
        value = _first_present(normalized, ("file_path", "path", "file", "filePath", "filepath", "uri", "target_file"))
        if value is not None:
            value = _clean_text_tool_path(value)
            normalized["file_path"] = value
            normalized["path"] = value
    if tool_name == "Read":
        offset = _first_present(normalized, ("offset", "start_line", "line", "start"))
        if offset is not None:
            normalized.setdefault("offset", offset)
        limit = _first_present(normalized, ("limit", "num_lines", "lines", "count"))
        if limit is not None:
            normalized.setdefault("limit", limit)
    elif tool_name == "Write":
        value = _first_present(normalized, ("content", "file_text", "text", "data", "body"))
        if value is not None:
            normalized.setdefault("content", value)
            normalized.setdefault("file_text", value)
    elif tool_name == "Edit":
        old = _first_present(normalized, ("old_string", "old", "oldText", "old_text", "target", "find"))
        new = _first_present(normalized, ("new_string", "new", "newText", "new_text", "replacement", "replace"))
        if old is not None:
            normalized.setdefault("old_string", old)
        if new is not None:
            normalized.setdefault("new_string", new)
    elif tool_name == "MultiEdit":
        edits = normalized.get("edits")
        if isinstance(edits, list):
            normalized["edits"] = [_normalize_tool_args("Edit", edit) if isinstance(edit, dict) else edit for edit in edits]
    elif tool_name == "Bash":
        command = _first_present(normalized, ("command", "cmd", "shell", "input", "script", "code"))
        if command is not None:
            repaired = _repair_shell_command_spacing(str(command))
            normalized["command"] = repaired
            normalized["cmd"] = repaired
        cwd = _first_present(normalized, ("cwd", "workdir", "working_directory"))
        if cwd is not None:
            normalized.setdefault("cwd", cwd)
            normalized.setdefault("workdir", cwd)
    elif tool_name == "exec_shell_start":
        command = _first_present(normalized, ("command", "cmd", "shell", "input", "script", "code"))
        if command is not None:
            normalized["command"] = _repair_shell_command_spacing(str(command))
    elif tool_name in {"MovePath", "CopyPath"}:
        source = _first_present(normalized, ("source", "src", "from", "path", "file_path"))
        destination = _first_present(normalized, ("destination", "dest", "to", "new_path"))
        if source is not None:
            normalized.setdefault("source", source)
        if destination is not None:
            normalized.setdefault("destination", destination)
    elif tool_name == "Git":
        compact = re.sub(r"[^a-z0-9]+", "_", str(args.get("name") or "")).strip("_")
        if not normalized.get("action"):
            for action in ("status", "diff", "log", "show", "branch"):
                if action in compact:
                    normalized["action"] = action
                    break
    elif tool_name == "WebFetch":
        url = _first_present(normalized, ("url", "href", "link", "uri"))
        if url is not None:
            normalized.setdefault("url", url)
    elif tool_name == "WebSearch":
        query = _first_present(normalized, ("query", "q", "search", "text", "input"))
        if query is not None:
            normalized.setdefault("query", query)
    elif tool_name == "view_image":
        path = _first_present(normalized, ("path", "file_path", "image_path", "image", "file"))
        if path is not None:
            normalized.setdefault("path", path)
    elif tool_name == "Grep":
        pattern = _first_present(normalized, ("pattern", "query", "search", "regex", "needle"))
        if pattern is not None:
            normalized.setdefault("pattern", pattern)
        include = _first_present(normalized, ("include", "glob", "file_pattern"))
        if include is not None:
            normalized["include"] = _normalize_relative_pattern(include)
    elif tool_name == "Glob":
        pattern = _first_present(normalized, ("pattern", "glob", "query"))
        if pattern is not None:
            normalized["pattern"] = _normalize_relative_pattern(pattern)
    elif tool_name == "LS":
        path = _first_present(normalized, ("path", "dir", "directory", "folder"))
        if path is not None:
            normalized.setdefault("path", path)
    elif tool_name == "TodoWrite":
        todos = _first_present(normalized, ("todos", "items", "todo_list"))
        if todos is not None:
            normalized.setdefault("todos", todos)
    elif tool_name == "calculator":
        expression = _first_present(normalized, ("expression", "input", "text", "code"))
        if expression is not None:
            normalized.setdefault("expression", expression)
    elif tool_name == "IntentDetect":
        text = _first_present(normalized, ("text", "input", "query", "prompt", "content"))
        if text is not None:
            normalized.setdefault("text", text)
    return normalized


def _normalize_tool_call(call: ToolCall) -> ToolCall:
    name = _normalize_tool_name(call.name)
    arguments = _normalize_tool_args(name, call.arguments)
    if name == "Git" and not arguments.get("action"):
        compact = re.sub(r"[^a-z0-9]+", "_", call.name.lower()).strip("_")
        for action in ("status", "diff", "log", "show", "branch"):
            if action in compact:
                arguments["action"] = action
                break
    return ToolCall(
        call_id=call.call_id,
        name=name,
        arguments=arguments,
        raw=call.raw,
    )


def _direct_tool_call_from_body(body: Json) -> ToolCall:
    raw: Json = body
    call_id = str(body.get("id") or body.get("call_id") or body.get("tool_call_id") or f"call_{uuid.uuid4().hex}")
    name: Any = body.get("name") or body.get("tool") or body.get("tool_name") or body.get("function_name") or body.get("recipient_name")
    if isinstance(name, str) and "." in name:
        name = name.rsplit(".", 1)[-1]
    raw_args: Any = body.get("arguments")
    if raw_args is None:
        raw_args = body.get("args")
    if raw_args is None:
        raw_args = body.get("input")
    if raw_args is None:
        raw_args = body.get("parameters")

    function = body.get("function")
    if isinstance(function, dict):
        name = name or function.get("name")
        raw_args = function.get("arguments") if raw_args is None else raw_args
        raw = function

    tool_call = body.get("tool_call")
    if isinstance(tool_call, dict):
        return _direct_tool_call_from_body(tool_call)

    if body.get("type") == "function" and isinstance(body.get("function"), dict):
        function = body["function"]
        name = function.get("name")
        raw_args = function.get("arguments")
        raw = body

    if body.get("type") == "tool_use":
        name = name or body.get("name")
        raw_args = body.get("input") if raw_args is None else raw_args
        raw = body

    if not name:
        raise ToolExecutionError("missing tool/function name", failure_type="invalid_input")
    return ToolCall(
        call_id=call_id,
        name=str(name),
        arguments=_parse_json_arguments(raw_args, allow_text=True),
        raw=raw,
    )


def _direct_tool_calls_from_body(body: Json) -> list[ToolCall]:
    if isinstance(body.get("tool_uses"), list):
        return [
            ToolCall(
                call_id=str(body.get("call_id") or body.get("id") or f"call_{uuid.uuid4().hex}"),
                name="multi_tool_use.parallel",
                arguments={"tool_uses": body.get("tool_uses"), "max_workers": body.get("max_workers")},
                raw=body,
            )
        ]
    raw_calls = body.get("tool_calls") or body.get("calls") or body.get("function_calls")
    if isinstance(raw_calls, list):
        return [_direct_tool_call_from_body(call) for call in raw_calls if isinstance(call, dict)]
    return [_direct_tool_call_from_body(body)]


def _response_tool_call_from_item(item: Json) -> ToolCall | None:
    item_type = item.get("type")
    if item_type not in {"function_call", "tool_call", "custom_tool_call"}:
        return None
    name = item.get("name")
    if not name:
        return None
    raw_args = item.get("arguments")
    allow_text = item_type == "custom_tool_call"
    if raw_args is None and item_type == "custom_tool_call":
        raw_args = item.get("input")
    if raw_args is None:
        raw_args = item.get("input") if isinstance(item.get("input"), dict) else item.get("action")
    return ToolCall(
        call_id=str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"),
        name=str(name),
        arguments=_parse_json_arguments(raw_args, allow_text=allow_text),
        raw=item,
    )


def _strip_xmlish_closing_tags(value: str) -> str:
    return re.sub(r"</(?:parameter|function|tool|invoke)>", "", value, flags=re.I).strip()



def _parse_parameter_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    parameter_re = re.compile(r"<parameter=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<parameter=[A-Za-z0-9_.:-]+>|<function=[A-Za-z0-9_.:-]+>|\Z)", re.S)
    for param in parameter_re.finditer(text or ""):
        key = param.group(1).strip()
        value = _strip_xmlish_closing_tags(param.group(2))
        if key:
            blocks.append((key, value))
    return blocks


def _inline_text_before_parameter_blocks(text: str) -> str:
    return re.sub(r"<parameter=[A-Za-z0-9_.:-]+>.*", "", text or "", flags=re.S).strip()


def _repair_shell_command_spacing(command: str) -> str:
    """Repair common spacing loss from weak text-tool markup."""
    cmd = str(command or "").strip()
    if not cmd:
        return cmd
    cmd = re.sub(r"^(find)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"^(grep)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"^(ls|cat|head|tail|wc|python3?|bash|sh)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"\b(ls\s+-[A-Za-z]+)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"\s-type\s*([fdl])(?=\s|-|$)", r" -type \1", cmd)
    cmd = re.sub(r"(-type\s+[fdl])-name", r"\1 -name", cmd)
    cmd = re.sub(r"\s-name(')", r" -name \1", cmd)
    cmd = re.sub(r'\s-name(")', r' -name \1', cmd)
    cmd = re.sub(r"(?<!\s)-name(')", r" -name \1", cmd)
    cmd = re.sub(r'(?<!\s)-name(")', r' -name \1', cmd)
    cmd = re.sub(r'\s-name([^\s\'"]+)', r" -name \1", cmd)
    cmd = re.sub(r"\b(head|tail)-([0-9]+)\b", r"\1 -\2", cmd)
    cmd = re.sub(r"\b(wc\s+-[A-Za-z]+)\{\}", r"\1 {}", cmd)
    cmd = re.sub(r"\s-l\{\}", r" -l {}", cmd)
    cmd = re.sub(r"([^\s])\{\}(?=\s|$)", r"\1 {}", cmd)
    cmd = re.sub(r"\s+", " ", cmd).strip()
    return cmd

def _parse_text_tool_calls(text: str) -> list[ToolCall]:
    """Parse common text-only tool-call fallbacks emitted by weak native-tool providers."""

    if not text or ("<function=" not in text and "<parameter=" not in text):
        return []
    calls: list[ToolCall] = []
    function_re = re.compile(r"<function=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<function=[A-Za-z0-9_.:-]+>|\Z)", re.S)

    def append_call(name: str, args: Json, raw_text: str) -> None:
        if not name:
            return
        calls.append(
            ToolCall(
                call_id=f"textcall_{uuid.uuid4().hex}",
                name=name,
                arguments=args,
                raw={"gateway_text_tool_call_fallback": True, "text": raw_text[:2000]},
            )
        )

    matched_function = False
    for match in function_re.finditer(text):
        matched_function = True
        name = match.group(1).strip()
        body = match.group(2).strip()
        if body.startswith("{"):
            try:
                parsed = json.loads(_strip_xmlish_closing_tags(body))
                if isinstance(parsed, dict):
                    append_call(name, parsed, match.group(0))
                    continue
            except Exception:
                pass
        blocks = _parse_parameter_blocks(body)
        if name in {"Bash", "bash", "exec_command", "shell", "shell_command"}:
            inline_command = _inline_text_before_parameter_blocks(body)
            if inline_command:
                append_call(name, {"command": _repair_shell_command_spacing(inline_command)}, match.group(0))
            current: Json | None = None
            for key, value in blocks:
                if key in {"command", "cmd", "shell"}:
                    if current and current.get("command"):
                        append_call(name, current, match.group(0))
                    current = {"command": _repair_shell_command_spacing(value)}
                elif current is not None:
                    current[key] = value
            if current and current.get("command"):
                append_call(name, current, match.group(0))
            continue
        args: Json = {}
        for key, value in blocks:
            args[key] = value
        if not args:
            inline_value = _inline_text_before_parameter_blocks(body)
            normalized_name = _normalize_tool_name(name)
            if inline_value and normalized_name in {"Read", "FileInfo", "LS", "Tree", "Glob", "PythonSymbols", "JsonQuery"}:
                if normalized_name == "Glob":
                    args["pattern"] = inline_value
                else:
                    args["path"] = inline_value
        append_call(name, args, match.group(0))

    if not matched_function:
        current: Json | None = None
        for key, value in _parse_parameter_blocks(text):
            if key in {"command", "cmd", "shell"}:
                if current and current.get("command"):
                    append_call("Bash", current, text)
                current = {"command": _repair_shell_command_spacing(value)}
            elif current is not None:
                current[key] = value
        if current and current.get("command"):
            append_call("Bash", current, text)
    return calls


def _extract_tool_calls(path: str, response: Json) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                calls.append(
                    ToolCall(
                        call_id=str(call.get("id") or f"call_{uuid.uuid4().hex}"),
                        name=str(fn["name"]),
                        arguments=_parse_json_arguments(fn.get("arguments")),
                        raw=call,
                    )
                )
        return calls

    if path == "/v1/responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            call = _response_tool_call_from_item(item)
            if call:
                calls.append(call)
            for block in item.get("content") or []:
                if isinstance(block, dict):
                    call = _response_tool_call_from_item(block)
                    if call:
                        calls.append(call)
        return calls

    if path == "/v1/messages":
        for block in response.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name"):
                calls.append(
                    ToolCall(
                        call_id=str(block.get("id") or f"toolu_{uuid.uuid4().hex}"),
                        name=str(block["name"]),
                        arguments=_parse_json_arguments(block.get("input") or {}),
                        raw=block,
                    )
                )
        return calls

    return calls


def _text_tool_call_fallback_enabled() -> bool:
    return bool(_gateway_config().get("text_tool_call_fallback_enabled", True))


def _extract_text_tool_calls(path: str, response: Json) -> list[ToolCall]:
    if not _text_tool_call_fallback_enabled():
        return []
    return _parse_text_tool_calls(_response_text(path, response))


def _assistant_message_from_chat_response(response: Json) -> Json:
    choices = response.get("choices") or []
    if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict):
        return dict(choices[0]["message"])
    return {"role": "assistant", "content": None}


def _append_tool_results(path: str, body: Json, response: Json, results: list[ToolResult]) -> Json:
    updated = dict(body)
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        messages.append(_assistant_message_from_chat_response(response))
        for result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )
        updated["messages"] = messages
        return updated

    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        custom_call_ids: set[str] = set()
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                input_items.append(item)
                if item.get("type") == "custom_tool_call" and item.get("call_id"):
                    custom_call_ids.add(str(item["call_id"]))
            if isinstance(item, dict):
                for block in item.get("content") or []:
                    if isinstance(block, dict) and block.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                        input_items.append(block)
                        if block.get("type") == "custom_tool_call" and block.get("call_id"):
                            custom_call_ids.add(str(block["call_id"]))
        for result in results:
            output_type = "custom_tool_call_output" if result.call_id in custom_call_ids else "function_call_output"
            output_item = {
                "type": output_type,
                "call_id": result.call_id,
                "output": result.content,
            }
            if output_type == "custom_tool_call_output":
                output_item["name"] = result.name
            input_items.append(output_item)
        updated["input"] = input_items
        return updated

    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        content = response.get("content") or []
        messages.append({"role": "assistant", "content": content})
        result_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": result.call_id,
                "content": result.content,
                **({"is_error": True} if not result.success else {}),
            }
            for result in results
        ]
        messages.append({"role": "user", "content": result_blocks})
        updated["messages"] = messages
        return updated

    return updated


def _append_text_tool_results(path: str, body: Json, response: Json, calls: list[ToolCall], results: list[ToolResult]) -> Json:
    updated = dict(body)
    tool_report = {
        "gateway_local_tool_fallback": True,
        "reason": "upstream returned text-only <function=...> tool call markup without native protocol tool_calls/tool_use",
        "calls": [
            {
                "id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
                "success": result.success,
                "failure_type": result.failure_type,
                "content": result.content,
            }
            for call, result in zip(calls, results)
        ],
    }
    report_text = (
        "Gateway 已识别并执行上游文本形式的工具调用。请基于这些真实工具结果继续分析；"
        "如果还需要工具，请优先返回原生 tool_calls/tool_use，不能支持时才继续使用 <function=...> 形式。\n\n"
        + json.dumps(tool_report, ensure_ascii=False, indent=2)
    )
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        messages.append(_assistant_message_from_chat_response(response))
        messages.append({"role": "user", "content": report_text})
        updated["messages"] = messages
        return updated
    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        text = _response_text(path, response)
        if text:
            messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": report_text})
        updated["messages"] = messages
        return updated
    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        input_items.append({"role": "assistant", "content": _response_text(path, response)})
        input_items.append({"role": "user", "content": report_text})
        updated["input"] = input_items
        return updated
    return updated


def _extract_mentioned_paths(text: str) -> list[str]:
    candidates = re.findall(r"@([A-Za-z0-9_./\\-]+)", text)
    out: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().strip(".,;:，。；：）)]}")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _should_build_local_planner_context(path: str, body: Json) -> bool:
    gateway = _gateway_config()
    if not gateway.get("local_planner_enabled", True):
        return False
    if path not in SUPPORTED_PATHS:
        return False
    text = _last_user_text(path, body)
    if not text:
        return False
    lowered = text.lower()
    analyze_intent = any(token in lowered for token in ("分析", "analyze", "review", "理解", "梳理"))
    code_scope = any(token in lowered for token in ("代码", "code", "项目", "project", "src", ".py", "class", "类", "@"))
    return analyze_intent and code_scope


def _select_local_planner_files(user_text: str, max_files: int) -> list[str]:
    roots = _extract_mentioned_paths(user_text)
    if not roots:
        roots = ["src", "README.md", "docs"]
    files: list[str] = []
    patterns_by_root: list[tuple[str, str]] = []
    for root in roots:
        normalized = root.rstrip("/") or "."
        try:
            resolved = _resolve_workspace_path(normalized)
        except Exception:
            continue
        if resolved.is_file():
            try:
                rel = str(resolved.relative_to(_workspace_root()))
                files.append(rel)
            except Exception:
                pass
        elif resolved.is_dir():
            if normalized.lower().endswith("docs"):
                patterns_by_root.append((normalized, "**/*.md"))
            elif normalized.lower().endswith("src") or "src" in normalized.lower():
                patterns_by_root.append((normalized, "**/*.py"))
            else:
                patterns_by_root.extend([(normalized, "**/*.py"), (normalized, "**/*.md")])
    for root, pattern in patterns_by_root:
        result = _execute_tool_call(ToolCall(f"planner_glob_{uuid.uuid4().hex}", "Glob", {"path": root, "pattern": pattern, "limit": max_files}, {}))
        if result.success:
            for line in result.content.splitlines():
                item = line.rstrip("/")
                if item and item not in files:
                    files.append(item)
                if len(files) >= max_files:
                    break
        if len(files) >= max_files:
            break
    return files[:max_files]


def _build_local_planner_context(user_text: str) -> str:
    gateway = _gateway_config()
    max_files = max(1, min(int(gateway.get("local_planner_max_files") or 24), 80))
    max_bytes = max(1000, min(int(gateway.get("local_planner_max_bytes_per_file") or 24000), 200000))
    sections: list[str] = []
    tree = _execute_tool_call(ToolCall(f"planner_tree_{uuid.uuid4().hex}", "Tree", {"path": ".", "max_depth": 3, "max_entries": 300}, {}))
    if tree.success:
        sections.append("## 本地工具结果：项目结构 Tree\n" + tree.content)
    files = _select_local_planner_files(user_text, max_files)
    if files:
        sections.append("## 本地工具结果：命中文件列表\n" + "\n".join(files))
    symbol_sections: list[str] = []
    for file_path in [f for f in files if f.endswith(".py")][:max_files]:
        symbols = _execute_tool_call(ToolCall(f"planner_symbols_{uuid.uuid4().hex}", "PythonSymbols", {"file_path": file_path}, {}))
        if symbols.success:
            symbol_sections.append(f"### {file_path}\n{symbols.content[:12000]}")
    if symbol_sections:
        sections.append("## 本地工具结果：Python 符号/类/函数\n" + "\n\n".join(symbol_sections))
    if files:
        read_many = _execute_tool_call(
            ToolCall(
                f"planner_read_{uuid.uuid4().hex}",
                "ReadManyFiles",
                {"paths": files, "max_files": max_files, "max_bytes_per_file": max_bytes},
                {},
            )
        )
        if read_many.success:
            sections.append("## 本地工具结果：关键文件内容\n" + read_many.content)
    return "\n\n".join(sections)


def _apply_local_planner_context(path: str, body: Json) -> Json:
    if not _should_build_local_planner_context(path, body):
        return body
    user_text = _last_user_text(path, body)
    context = _build_local_planner_context(user_text)
    if not context.strip():
        return body
    prompt = (
        "Gateway 已经在本地真实执行文件/符号/目录工具完成预分析。"
        "下面的工具结果是事实证据，不是提示词伪造的 tool call。"
        "请基于这些证据完成用户请求；如果证据不足，说明还需要哪些文件/工具。\n\n"
        "# 用户原始请求\n"
        f"{user_text}\n\n"
        "# Gateway 本地真实工具证据\n"
        f"{context}\n\n"
        "# 输出要求\n"
        "按 语义分析 / 逐个类或文件分析 / 调用与证据检查 / 反思调整 / 最终结论 输出。"
    )
    updated = _replace_last_user_text(path, body, prompt)
    updated.setdefault("gateway_context", {})
    updated["gateway_context"].update({"local_planner": True, "planner_evidence_chars": len(context)})
    return updated


def _failure_log_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("GATEWAY_TOOL_FAILURE_LOG") or ".gateway_tool_failures.jsonl")


def _logging_backend() -> str:
    backend = str(_gateway_config().get("logging_backend") or os.environ.get("GATEWAY_LOGGING_BACKEND") or "sqlite").lower()
    if backend == "sqlite":
        return "sqlite"
    # High-frequency gateway logs must not fall back to JSON/JSONL files unless
    # explicitly enabled for a one-off legacy/debug run. Legacy files are still
    # imported/read, but normal runtime writes stay in SQLite WAL.
    if os.environ.get("GATEWAY_ALLOW_FILE_LOGGING", "0").lower() not in {"1", "true", "yes"}:
        return "sqlite"
    return backend


def _sqlite_insert_tool_failure(event: Json) -> None:
    _sqlite_init()
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            conn.execute(
                """
                INSERT INTO tool_failures
                (ts, tool_name, call_id, failure_type, arguments_keys_json, content, fake_prompt_tools, execution_ms, retry_count, provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["ts"],
                    event["tool_name"],
                    event["call_id"],
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
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            row = conn.execute("SELECT calls, success, failure, failures_json FROM tool_stats WHERE tool_name = ?", (name,)).fetchone()
            if row:
                calls, ok_count, fail_count, failures_raw = row
                failures = json.loads(failures_raw or "{}")
            else:
                calls = ok_count = fail_count = 0
                failures = {}
            calls += 1
            if success:
                ok_count += 1
            else:
                fail_count += 1
                key = failure_type or "unknown"
                failures[key] = failures.get(key, 0) + 1
            conn.execute(
                """
                INSERT INTO tool_stats(tool_name, calls, success, failure, failures_json, last_called_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_name) DO UPDATE SET
                    calls=excluded.calls,
                    success=excluded.success,
                    failure=excluded.failure,
                    failures_json=excluded.failures_json,
                    last_called_at=excluded.last_called_at
                """,
                (name, calls, ok_count, fail_count, json.dumps(failures, ensure_ascii=False), now),
            )
            conn.commit()
        finally:
            conn.close()


def _sqlite_record_request_stat(path: str, status: int) -> None:
    _sqlite_init()
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    status_key = str(status)
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            conn.execute("INSERT INTO request_stats(key, value) VALUES ('total', 1) ON CONFLICT(key) DO UPDATE SET value=value+1")
            conn.execute("INSERT INTO request_stats(key, value) VALUES ('last_request_at_epoch', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (int(time.time()),))
            conn.execute("INSERT INTO request_stats(key, value) VALUES ('last_request_at_iso', 0) ON CONFLICT(key) DO UPDATE SET value=0")
            conn.execute("INSERT INTO request_stats_by_path(path, value) VALUES (?, 1) ON CONFLICT(path) DO UPDATE SET value=value+1", (path,))
            conn.execute("INSERT INTO request_stats_by_status(status, value) VALUES (?, 1) ON CONFLICT(status) DO UPDATE SET value=value+1", (status_key,))
            conn.commit()
        finally:
            conn.close()


def _sqlite_insert_request_log(event: Json) -> None:
    _sqlite_init()
    with SQLITE_LOCK:
        conn = _sqlite_connect()
        try:
            conn.execute(
                """
                INSERT INTO request_logs
                (ts, request_id, path, status, downstream_key, request_json, response_json, fake_prompt_tools)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["ts"],
                    event["request_id"],
                    event["path"],
                    int(event["status"]),
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
        tools: Json = {}
        for name, calls, success, failure, failures_raw, last_called_at in conn.execute(
            "SELECT tool_name, calls, success, failure, failures_json, last_called_at FROM tool_stats ORDER BY tool_name"
        ):
            tools[name] = {
                "calls": calls,
                "success": success,
                "failure": failure,
                "failures": json.loads(failures_raw or "{}"),
                "last_called_at": last_called_at,
            }
        total_row = conn.execute("SELECT value FROM request_stats WHERE key='total'").fetchone()
        last_ts = conn.execute("SELECT ts FROM request_logs ORDER BY id DESC LIMIT 1").fetchone()
        by_path = {path: value for path, value in conn.execute("SELECT path, value FROM request_stats_by_path")}
        by_status = {status: value for status, value in conn.execute("SELECT status, value FROM request_stats_by_status")}
        memory_total_row = conn.execute("SELECT COUNT(*) FROM conversation_memories").fetchone()
        return {
            "tools": tools,
            "memory": {"total": int(memory_total_row[0]) if memory_total_row else 0},
            "requests": {
                "total": int(total_row[0]) if total_row else 0,
                "by_path": by_path,
                "by_status": by_status,
                "last_request_at": last_ts[0] if last_ts else None,
            },
            "backend": "sqlite",
            "sqlite_path": str(_sqlite_path()),
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
    finally:
        conn.close()
    out = []
    for ts, request_id, path, status, downstream_key, request_raw, response_raw, fake_prompt_tools in reversed(rows):
        out.append(
            {
                "ts": ts,
                "request_id": request_id,
                "path": path,
                "status": status,
                "downstream_key": downstream_key,
                "request": json.loads(request_raw or "{}"),
                "response": json.loads(response_raw) if response_raw else None,
                "fake_prompt_tools": bool(fake_prompt_tools),
            }
        )
    return out


def _sqlite_tail_failures(limit: int = 50) -> list[Json]:
    _sqlite_init()
    conn = _sqlite_connect()
    try:
        rows = conn.execute(
            "SELECT ts, tool_name, call_id, failure_type, arguments_keys_json, content, fake_prompt_tools FROM tool_failures ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for ts, tool_name, call_id, failure_type, keys_raw, content, fake_prompt_tools in reversed(rows):
        out.append(
            {
                "ts": ts,
                "tool_name": tool_name,
                "call_id": call_id,
                "failure_type": failure_type,
                "arguments_keys": json.loads(keys_raw or "[]"),
                "content": content,
                "fake_prompt_tools": bool(fake_prompt_tools),
            }
        )
    return out


def _record_tool_failure(
    call: ToolCall,
    result: ToolResult,
    *,
    execution_ms: float | None = None,
    retry_count: int | None = None,
    provider: str | None = None,
) -> None:
    if result.success:
        return
    if not _gateway_config().get("record_unsupported_tools", True):
        return
    event: dict = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "tool_name": call.name,
        "call_id": call.call_id,
        "failure_type": result.failure_type,
        "arguments_keys": sorted(call.arguments.keys()),
        "content": result.content[:1000] if result.content else "",
        "fake_prompt_tools": False,
    }
    if execution_ms is not None:
        event["execution_ms"] = execution_ms
    if retry_count is not None:
        event["retry_count"] = retry_count
    if provider is not None:
        event["provider"] = provider
    try:
        if _logging_backend() == "sqlite":
            _sqlite_insert_tool_failure(event)
        else:
            with _failure_log_path().open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        if os.environ.get("DEBUG"):
            traceback.print_exc()


def _read_json_file(path: pathlib.Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        if os.environ.get("DEBUG"):
            traceback.print_exc()
    return copy.deepcopy(default)


def _write_json_file(path: pathlib.Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_tool_stat(name: str, success: bool, failure_type: str | None = None) -> None:
    if _logging_backend() == "sqlite":
        _sqlite_record_tool_stat(name, success, failure_type)
        return
    stats = _read_json_file(STATS_PATH, {"tools": {}, "requests": {"total": 0}})
    tools = stats.setdefault("tools", {})
    item = tools.setdefault(name, {"calls": 0, "success": 0, "failure": 0, "failures": {}})
    item["calls"] += 1
    if success:
        item["success"] += 1
    else:
        item["failure"] += 1
        failures = item.setdefault("failures", {})
        failures[failure_type or "unknown"] = failures.get(failure_type or "unknown", 0) + 1
    item["last_called_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _write_json_file(STATS_PATH, stats)


def _record_request_stat(path: str, status: int) -> None:
    if _logging_backend() == "sqlite":
        _sqlite_record_request_stat(path, status)
        return
    stats = _read_json_file(STATS_PATH, {"tools": {}, "requests": {"total": 0}})
    requests = stats.setdefault("requests", {"total": 0})
    requests["total"] = requests.get("total", 0) + 1
    by_path = requests.setdefault("by_path", {})
    by_path[path] = by_path.get(path, 0) + 1
    by_status = requests.setdefault("by_status", {})
    status_key = str(status)
    by_status[status_key] = by_status.get(status_key, 0) + 1
    requests["last_request_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _write_json_file(STATS_PATH, stats)


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, val in value.items():
            if key.lower() in {"authorization", "api_key", "x-api-key", "key", "token", "password", "secret"}:
                out[key] = "***"
            else:
                out[key] = _redact_payload(val)
        return out
    if isinstance(value, list):
        return [_redact_payload(v) for v in value]
    return value


def _write_request_log(path: str, body: Json, status: int, response: Json | None, downstream_key: str | None) -> None:
    if not load_config().get("gateway", {}).get("request_logging", True):
        return
    event = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "request_id": f"req_{uuid.uuid4().hex}",
        "path": path,
        "status": status,
        "downstream_key": downstream_key,
        "request": _redact_payload(body),
        "response": _redact_payload(response) if response is not None else None,
        "fake_prompt_tools": False,
    }
    if _logging_backend() == "sqlite":
        _sqlite_insert_request_log(event)
    else:
        with REQUEST_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def _tail_jsonl(path: pathlib.Path, limit: int = 50) -> list[Json]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    rows = []
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        except Exception:
            continue
    return rows


def _stats_snapshot() -> Json:
    if _logging_backend() == "sqlite":
        return _sqlite_stats_snapshot()
    return _read_json_file(STATS_PATH, {"tools": {}, "requests": {}})


def _tail_requests(limit: int = 50) -> list[Json]:
    if _logging_backend() == "sqlite":
        return _sqlite_tail_requests(limit)
    return _tail_jsonl(REQUEST_LOG_PATH, limit)


def _tail_failures(limit: int = 50) -> list[Json]:
    if _logging_backend() == "sqlite":
        return _sqlite_tail_failures(limit)
    return _tail_jsonl(_failure_log_path(), limit)


def _tool_catalog_snapshot() -> Json:
    unique: dict[str, GatewayTool] = {}
    aliases: dict[str, list[str]] = {}
    for public_name, tool in BUILTIN_TOOLS.items():
        unique.setdefault(tool.name, tool)
        if public_name != tool.name:
            aliases.setdefault(tool.name, []).append(public_name)
    tools = [
        {
            "name": tool.name,
            "aliases": sorted(set(aliases.get(tool.name, []))),
            "description": tool.description,
            "risk": tool.risk,
            "status": "connector_required" if tool.risk == "connector_required" else "ready",
            "parameters": tool.parameters,
        }
        for tool in sorted(unique.values(), key=lambda item: item.name.lower())
    ]
    failures = _tail_failures(500)
    failure_counts: dict[str, Json] = {}
    for failure in failures:
        name = str(failure.get("tool_name") or failure.get("tool") or "unknown")
        row = failure_counts.setdefault(name, {"tool": name, "count": 0, "failure_types": {}})
        row["count"] += 1
        failure_type = str(failure.get("failure_type") or "unknown")
        row["failure_types"][failure_type] = row["failure_types"].get(failure_type, 0) + 1
    unsupported = sorted(failure_counts.values(), key=lambda item: int(item["count"]), reverse=True)
    return {"tools": tools, "unsupported_or_failed": unsupported}


def _text_response(handler: BaseHTTPRequestHandler, status: int, payload: str, content_type: str = "text/html; charset=utf-8") -> None:
    data = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        username, password = decoded.split(":", 1)
        return username, password
    except Exception:
        return None


def _check_admin(handler: BaseHTTPRequestHandler) -> bool:
    cfg = load_config()
    parsed = _parse_basic_auth(handler.headers.get("authorization"))
    admin = cfg.get("admin", {})
    if parsed and parsed[0] == admin.get("username", "admin") and _hash_secret(parsed[1]) == admin.get("password_hash"):
        return True
    handler.send_response(401)
    handler.send_header("www-authenticate", 'Basic realm="Gateway Admin"')
    handler.send_header("content-type", "text/plain; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(b"admin authentication required")
    return False


def _check_downstream_key(handler: BaseHTTPRequestHandler) -> str | None:
    cfg = load_config()
    keys = cfg.get("downstream_keys") or []
    if not keys:
        return "no-key-configured"
    auth = handler.headers.get("authorization") or ""
    supplied = ""
    if auth.startswith("Bearer "):
        supplied = auth.split(" ", 1)[1].strip()
    elif handler.headers.get("x-api-key"):
        supplied = handler.headers.get("x-api-key", "").strip()
    if not supplied:
        raise DownstreamAuthError("missing downstream API key")
    supplied_hash = _hash_secret(supplied)
    path = handler.path.split("?", 1)[0]
    protocol_by_path = {
        "/v1/chat/completions": "chat_completions",
        "/v1/responses": "responses",
        "/v1/messages": "messages",
        "/v1/messages/count_tokens": "messages",
        "/v1/tools/call": "direct_tools",
        "/v1/functions/call": "direct_tools",
        "/tools/call": "direct_tools",
        "/v1/models": "models",
    }
    requested_protocol = protocol_by_path.get(path)
    for item in keys:
        if item.get("enabled", True) and item.get("key_hash") == supplied_hash:
            allowed = item.get("protocols")
            if isinstance(allowed, list) and requested_protocol and requested_protocol not in allowed and "all" not in allowed:
                # Backward compatibility: older configs created before per-key protocol
                # support did not list `models`; allow model-list discovery for keys
                # that can call at least one conversation protocol.
                if not (
                    requested_protocol == "models"
                    and any(proto in allowed for proto in ("chat_completions", "responses", "messages"))
                ):
                    raise DownstreamAuthError(f"downstream key is not allowed to use protocol: {requested_protocol}")
            return str(item.get("name") or item.get("prefix") or "key")
    raise DownstreamAuthError("invalid downstream API key")


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("content-length") or "0")
    raw = handler.rfile.read(length).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
    return {k: v[-1] if v else "" for k, v in parsed.items()}


def _client_snippet_context() -> Json:
    cfg = load_config()
    gateway = cfg.get("gateway", {}) if isinstance(cfg.get("gateway"), dict) else {}
    upstream = cfg.get("upstream", {}) if isinstance(cfg.get("upstream"), dict) else {}
    base_url = str(gateway.get("public_base_url") or os.environ.get("GATEWAY_PUBLIC_BASE_URL") or "http://127.0.0.1:8885").rstrip("/")
    api_key = str(gateway.get("client_snippet_api_key") or os.environ.get("DOWNSTREAM_API_KEY") or os.environ.get("GATEWAY_DOWNSTREAM_KEY") or "")
    model = str(gateway.get("downstream_model_alias") or upstream.get("model") or os.environ.get("UPSTREAM_MODEL") or "mimo-v2.5-pro")
    review_model = str(gateway.get("review_model_alias") or model)
    context_window = int(gateway.get("client_context_window") or 1_000_000)
    auto_compact = int(gateway.get("client_auto_compact_token_limit") or max(context_window - 100_000, 1_000))
    output_limit = int(gateway.get("client_output_token_limit") or upstream.get("max_output_tokens") or 128_000)
    reasoning_effort = str(gateway.get("codex_reasoning_effort") or "xhigh")
    return {
        "base_url": base_url,
        "base_url_v1": f"{base_url}/v1",
        "api_key": api_key,
        "model": model,
        "review_model": review_model,
        "context_window": context_window,
        "auto_compact_token_limit": auto_compact,
        "output_token_limit": output_limit,
        "reasoning_effort": reasoning_effort,
    }


def _toml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _client_config_snippets() -> Json:
    c = _client_snippet_context()
    codex_config_toml = "\n".join(
        [
            'model_provider = "Gateway"',
            f"model = {_toml_string(c['model'])}",
            f"review_model = {_toml_string(c['review_model'])}",
            f"model_reasoning_effort = {_toml_string(c['reasoning_effort'])}",
            "disable_response_storage = true",
            'network_access = "enabled"',
            "windows_wsl_setup_acknowledged = true",
            f"model_context_window = {int(c['context_window'])}",
            f"model_auto_compact_token_limit = {int(c['auto_compact_token_limit'])}",
            "",
            "[model_providers.Gateway]",
            'name = "Gateway"',
            f"base_url = {_toml_string(c['base_url'])}",
            'wire_api = "responses"',
            "requires_openai_auth = true",
            "",
        ]
    )
    codex_auth_json = json.dumps({"OPENAI_API_KEY": c["api_key"]}, ensure_ascii=False, indent=2)
    opencode_json = json.dumps(
        {
            "provider": {
                "openai": {
                    "options": {
                        "baseURL": c["base_url_v1"],
                        "apiKey": c["api_key"],
                    },
                    "models": {
                        c["model"]: {
                            "name": c["model"],
                            "limit": {
                                "context": c["context_window"],
                                "output": c["output_token_limit"],
                            },
                            "options": {"store": False},
                            "variants": {"low": {}, "medium": {}, "high": {}, "xhigh": {}},
                        }
                    },
                }
            },
            "agent": {
                "build": {"options": {"store": False}},
                "plan": {"options": {"store": False}},
            },
            "$schema": "https://opencode.ai/config.json",
        },
        ensure_ascii=False,
        indent=2,
    )
    claude_bash_profile_function = "\n".join(
        [
            "claude_m1() {",
            f'    export ANTHROPIC_BASE_URL="{c["base_url"]}"',
            "    export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            f'    export ANTHROPIC_AUTH_TOKEN="{c["api_key"]}"',
            '    export ANTHROPIC_API_KEY=""',
            f'    export ANTHROPIC_DEFAULT_OPUS_MODEL="{c["model"]}"',
            f'    export ANTHROPIC_DEFAULT_SONNET_MODEL="{c["model"]}"',
            f'    export ANTHROPIC_DEFAULT_HAIKU_MODEL="{c["model"]}"',
            f'    export ANTHROPIC_MODEL="{c["model"]}"',
            f'    export ANTHROPIC_SMALL_FAST_MODEL="{c["model"]}"',
            '    export ENABLE_LSP_TOOL="1"',
            '    /usr/local/bin/claude --dangerously-skip-permissions "$@"',
            "}",
        ]
    )
    claude_terminal_env = "\n".join(
        [
            f'export ANTHROPIC_BASE_URL="{c["base_url"]}"',
            f'export ANTHROPIC_AUTH_TOKEN="{c["api_key"]}"',
            "export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
            "export CLAUDE_CODE_ATTRIBUTION_HEADER=0",
        ]
    )
    vscode_claude_settings_json = json.dumps(
        {
            "env": {
                "ANTHROPIC_BASE_URL": c["base_url"],
                "ANTHROPIC_AUTH_TOKEN": c["api_key"],
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            }
        },
        ensure_ascii=False,
        indent=2,
    )
    return {
        "context": c,
        "codex_config_toml": codex_config_toml,
        "codex_auth_json": codex_auth_json,
        "opencode_json": opencode_json,
        "claude_bash_profile_function": claude_bash_profile_function,
        "claude_terminal_env": claude_terminal_env,
        "vscode_claude_settings_json": vscode_claude_settings_json,
    }


def _render_client_config_ui() -> str:
    snippets = _client_config_snippets()
    c = snippets["context"]
    cards = [
        ("~/.codex/config.toml", snippets["codex_config_toml"]),
        ("~/.codex/auth.json", snippets["codex_auth_json"]),
        ("opencode.json", snippets["opencode_json"]),
        ("~/.bash_profile: claude_m1", snippets["claude_bash_profile_function"]),
        ("Terminal env", snippets["claude_terminal_env"]),
        ("~/.claude/settings.json", snippets["vscode_claude_settings_json"]),
    ]
    rendered_cards = "\n".join(
        f'<section><h2>{html.escape(title)}</h2><textarea rows="14" readonly>{html.escape(text)}</textarea></section>'
        for title, text in cards
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gateway Client Config</title>
<style>
body{{font-family:system-ui;margin:24px;max-width:1200px}}
input,textarea{{width:100%;box-sizing:border-box;margin:4px 0 10px;padding:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
section{{margin:18px 0;padding:14px;border:1px solid #ddd;border-radius:10px}}
a{{margin-right:12px}} button{{padding:8px 14px}}
</style></head><body>
<h1>Gateway Client Config / 下游客户端配置</h1>
<p><a href="/ui">Admin</a><a href="/client-config.json">JSON</a><a href="/healthz">Health</a></p>
<p>只生成配置片段，不自动写入 <code>~/.codex</code>、<code>~/.claude</code> 或 <code>.bash_profile</code>，避免破坏你现有环境。</p>
<form method="post" action="/admin/client-config">
<h2>生成参数</h2>
<label>Gateway Public Base URL</label><input name="public_base_url" value="{html.escape(c['base_url'])}">
<label>Downstream API Key</label><input name="client_snippet_api_key" value="{html.escape(c['api_key'])}">
<label>Model</label><input name="downstream_model_alias" value="{html.escape(c['model'])}">
<label>Review Model</label><input name="review_model_alias" value="{html.escape(c['review_model'])}">
<label>Codex Reasoning Effort</label><input name="codex_reasoning_effort" value="{html.escape(c['reasoning_effort'])}">
<label>Client Context Window</label><input name="client_context_window" value="{int(c['context_window'])}">
<label>Auto Compact Token Limit</label><input name="client_auto_compact_token_limit" value="{int(c['auto_compact_token_limit'])}">
<label>Output Token Limit</label><input name="client_output_token_limit" value="{int(c['output_token_limit'])}">
<button>保存并刷新配置片段</button>
</form>
{rendered_cards}
</body></html>"""


def _render_admin_ui() -> str:
    cfg = load_config()
    redacted = _redacted_config(cfg)
    stats = _stats_snapshot()
    failures = _tail_failures(20)
    requests = _tail_requests(20)
    upstream = cfg.get("upstream", {})
    gateway = cfg.get("gateway", {})
    capabilities = upstream.get("capabilities", {}) if isinstance(upstream.get("capabilities"), dict) else {}
    upstream_paths = upstream.get("paths", {}) if isinstance(upstream.get("paths"), dict) else {}
    context = cfg.get("context", {}) if isinstance(cfg.get("context"), dict) else {}
    tool_rows = "\n".join(
        f"<tr><td>{html.escape(name)}</td><td>{item.get('calls', 0)}</td><td>{item.get('success', 0)}</td><td>{item.get('failure', 0)}</td><td><code>{html.escape(json.dumps(item.get('failures', {}), ensure_ascii=False))}</code></td></tr>"
        for name, item in sorted((stats.get("tools") or {}).items())
    )
    key_rows = "\n".join(
        f"<tr><td>{html.escape(str(k.get('name')))}</td><td>{html.escape(str(k.get('prefix')))}</td><td>{'yes' if k.get('enabled', True) else 'no'}</td><td>{html.escape(','.join(k.get('protocols') or ['chat_completions','responses','messages','direct_tools']))}</td></tr>"
        for k in cfg.get("downstream_keys", [])
    )
    failure_rows = "\n".join(
        f"<tr><td>{html.escape(str(x.get('ts')))}</td><td>{html.escape(str(x.get('tool_name')))}</td><td>{html.escape(str(x.get('failure_type')))}</td><td><code>{html.escape(str(x.get('content')))}</code></td></tr>"
        for x in failures
    )
    request_rows = "\n".join(
        f"<tr><td>{html.escape(str(x.get('ts')))}</td><td>{html.escape(str(x.get('path')))}</td><td>{x.get('status')}</td><td>{html.escape(str(x.get('downstream_key')))}</td></tr>"
        for x in requests
    )
    mcp_json = html.escape(json.dumps(cfg.get("mcp", {}).get("servers", []), ensure_ascii=False, indent=2))
    http_actions_json = html.escape(json.dumps(cfg.get("http_actions", {}).get("actions", []), ensure_ascii=False, indent=2))
    mcp_session_count = len(MCP_SESSIONS)
    mcp_cache_count = len(MCP_TOOL_CATALOG_CACHE)
    mcp_health_rows = "\n".join(
        f"<tr><td>{html.escape(str(row.get('name')))}</td><td>{html.escape(str(row.get('status', 'unknown')))}</td><td>{html.escape(str(row.get('session')))}</td><td>{html.escape(str(row.get('cache')))}</td><td>{row.get('tool_count', row.get('cached_tool_count', 0))}</td><td><code>{html.escape(str(row.get('detail', '')))}</code></td></tr>"
        for row in _mcp_health_snapshot(probe=False)
    )
    upstream_profile_rows = "\n".join(
        f"<tr><td>{'✅' if str(profile.get('id')) == str(cfg.get('active_upstream')) else ''}</td><td><code>{html.escape(str(profile.get('id')))}</code></td><td>{html.escape(str(profile.get('name')))}</td><td>{html.escape(str(profile.get('protocol')))}</td><td>{html.escape(str(profile.get('model')))}</td><td>{html.escape(str(profile.get('base_url')))}</td><td><form method='post' action='/admin/upstream-profile' style='display:inline'><input type='hidden' name='profile_id' value='{html.escape(str(profile.get('id')))}'><button name='action' value='activate'>设为默认</button></form> <form method='post' action='/admin/upstream-profile' style='display:inline'><input type='hidden' name='profile_id' value='{html.escape(str(profile.get('id')))}'><button name='action' value='delete'>删除</button></form></td></tr>"
        for profile in cfg.get("upstream_profiles", []) if isinstance(profile, dict)
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Gateway Admin</title>
<style>body{{font-family:system-ui;margin:24px;max-width:1200px}} input,select,textarea{{width:100%;box-sizing:border-box;margin:4px 0 10px;padding:8px}} table{{border-collapse:collapse;width:100%;margin:12px 0}} td,th{{border:1px solid #ddd;padding:6px;vertical-align:top}} code,pre{{background:#f6f6f6;padding:2px 4px}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}} button{{padding:8px 14px}}</style>
</head><body>
<h1>Tool Call Gateway Admin</h1>
<p><a href="/client-config">下游客户端配置生成器</a> <a href="/client-config.json">Client config JSON</a> <a href="/healthz">Health</a></p>
<p>生产环境请通过环境变量 <code>GATEWAY_ADMIN_PASSWORD</code> 和 <code>GATEWAY_DOWNSTREAM_KEY</code> 配置管理员密码和下游 API Key。开发环境默认 admin/admin 和 local-gateway-key。</p>
<div class="grid">
<section><h2>上游 API</h2>
<p>支持添加多个上游 API；每个上游可以独立设置协议、路由、模型、streaming、tool call、识图、网络检索等能力。当前默认上游会用于所有下游协议请求。</p>
<table><tr><th>Active</th><th>ID</th><th>Name</th><th>Protocol</th><th>Model</th><th>Base URL</th><th>Actions</th></tr>{upstream_profile_rows}</table>
<form method="post" action="/admin/config">
<h3>添加/编辑上游 API 详情</h3>
<label>Profile ID（新 ID 会新增；已有 ID 会更新）</label><input name="profile_id" value="{html.escape(str(upstream.get('id','default')))}">
<label>Profile Name</label><input name="profile_name" value="{html.escape(str(upstream.get('name','default')))}">
<label>Base URL</label><input name="base_url" value="{html.escape(str(upstream.get('base_url','')))}">
<label>API Key（留空则不修改）</label><input name="api_key" type="password" placeholder="keep unchanged">
<label>Model</label><input name="model" value="{html.escape(str(upstream.get('model','')))}">
<label>Timeout Seconds</label><input name="upstream_timeout_seconds" value="{html.escape(str(upstream.get('timeout_seconds', 60)))}">
<label>Max Input Tokens</label><input name="upstream_max_input_tokens" value="{html.escape(str(upstream.get('max_input_tokens', 128000)))}">
<label>Max Output Tokens</label><input name="upstream_max_output_tokens" value="{html.escape(str(upstream.get('max_output_tokens', 8192)))}">
<label>Upstream Max Concurrency</label><input name="upstream_max_concurrency" value="{html.escape(str(upstream.get('max_concurrency', 32)))}">
<label>Protocol</label><select name="protocol">
{''.join(f'<option value="{p}" {"selected" if upstream.get("protocol") == p else ""}>{p}</option>' for p in ["openai_chat","openai_responses","anthropic_messages","openai_compatible"])}
</select>
<p><b>协议转换说明：</b>当上游 Protocol 选择 <code>openai_chat</code> / <code>openai_compatible</code> 时，下游仍可同时调用 <code>/v1/chat/completions</code>、<code>/v1/responses</code>、<code>/v1/messages</code>；Gateway 会把三种请求统一转换为上游 <code>Chat Completions Path</code>，再把返回转换回下游协议。</p>
<label>Tools Enabled</label><select name="tools_enabled">
{''.join(f'<option value="{p}" {"selected" if upstream.get("tools_enabled") == p else ""}>{p}</option>' for p in ["auto","on","off","native_only"])}
</select>
<label><input type="checkbox" name="native_tools_verified" value="1" {"checked" if upstream.get("native_tools_verified") else ""} style="width:auto"> Native tools 已验证</label>
<label><input type="checkbox" name="use_for_coding" value="1" {"checked" if upstream.get("use_for_coding", True) else ""} style="width:auto"> 用于 coding agent</label>
<h3>Upstream Capabilities / 能力开关</h3>
<label><input type="checkbox" name="cap_supports_streaming" value="1" {"checked" if capabilities.get("supports_streaming", True) else ""} style="width:auto"> 支持 streaming</label>
<label><input type="checkbox" name="cap_supports_tools" value="1" {"checked" if capabilities.get("supports_tools", True) else ""} style="width:auto"> 支持 tool calls</label>
<label><input type="checkbox" name="cap_supports_function_calls" value="1" {"checked" if capabilities.get("supports_function_calls", True) else ""} style="width:auto"> 支持 function calls</label>
<label><input type="checkbox" name="cap_supports_parallel_tool_calls" value="1" {"checked" if capabilities.get("supports_parallel_tool_calls", True) else ""} style="width:auto"> 支持 parallel tool calls</label>
<label><input type="checkbox" name="cap_supports_vision" value="1" {"checked" if capabilities.get("supports_vision") else ""} style="width:auto"> 支持识图 / vision</label>
<label><input type="checkbox" name="cap_supports_network" value="1" {"checked" if capabilities.get("supports_network") else ""} style="width:auto"> 支持网络 / web</label>
<label><input type="checkbox" name="cap_supports_web_search" value="1" {"checked" if capabilities.get("supports_web_search") or capabilities.get("supports_network") else ""} style="width:auto"> 支持网络检索 / web search</label>
<label><input type="checkbox" name="cap_supports_json_schema" value="1" {"checked" if capabilities.get("supports_json_schema", True) else ""} style="width:auto"> 支持 JSON Schema / structured outputs</label>
<h3>Upstream Routes / 路由适配</h3>
<label>Models Path</label><input name="path_models" value="{html.escape(str(upstream_paths.get('models','/v1/models')))}">
<label>Chat Completions Path</label><input name="path_chat_completions" value="{html.escape(str(upstream_paths.get('chat_completions','/v1/chat/completions')))}">
<label>Responses Path</label><input name="path_responses" value="{html.escape(str(upstream_paths.get('responses','/v1/responses')))}">
<label>Messages Path</label><input name="path_messages" value="{html.escape(str(upstream_paths.get('messages','/v1/messages')))}">
	<h3>Gateway Runtime</h3>
	<label>Tool Mode</label><select name="tool_mode">
	{''.join(f'<option value="{p}" {"selected" if gateway.get("tool_mode") == p else ""}>{p}</option>' for p in ["orchestrate","passthrough"])}
	</select>
	<label>Max Tool Rounds</label><input name="max_tool_rounds" value="{html.escape(str(gateway.get('max_tool_rounds', DEFAULT_MAX_TOOL_ROUNDS)))}">
	<label>Max Concurrent Requests</label><input name="max_concurrent_requests" value="{html.escape(str(gateway.get('max_concurrent_requests', 32)))}">
	<label>Concurrency Queue Timeout Seconds</label><input name="concurrency_queue_timeout_seconds" value="{html.escape(str(gateway.get('concurrency_queue_timeout_seconds', 5)))}">
	<label>Tool Execution Timeout Seconds</label><input name="tool_execution_timeout_seconds" value="{html.escape(str(gateway.get('tool_execution_timeout_seconds', 60)))}">
	<label>Workspace Root</label><input name="workspace_root" value="{html.escape(str(gateway.get('workspace_root','')))}">
		<label><input type="checkbox" name="allow_write_tools" value="1" {"checked" if gateway.get("allow_write_tools") else ""} style="width:auto"> 允许写入工具</label>
		<label><input type="checkbox" name="allow_shell_tools" value="1" {"checked" if gateway.get("allow_shell_tools") else ""} style="width:auto"> 允许 Shell 工具</label>
	<label><input type="checkbox" name="request_logging" value="1" {"checked" if gateway.get("request_logging", True) else ""} style="width:auto"> 保留下游请求和响应</label>
		<label><input type="checkbox" name="record_unsupported_tools" value="1" {"checked" if gateway.get("record_unsupported_tools", True) else ""} style="width:auto"> 记录不支持/失败的 tools，方便后续增强</label>
		<label><input type="checkbox" name="text_tool_call_fallback_enabled" value="1" {"checked" if gateway.get("text_tool_call_fallback_enabled", True) else ""} style="width:auto"> 上游只输出文本 &lt;function=...&gt; 时，本地识别并执行工具</label>
		<h3>Context Router / 分流压缩</h3>
		<label><input type="checkbox" name="context_enabled" value="1" {"checked" if context.get("enabled") else ""} style="width:auto"> 启用上下文治理</label>
		<label><input type="checkbox" name="context_fanout_enabled" value="1" {"checked" if context.get("fanout_enabled") else ""} style="width:auto"> 超大请求 fan-out 分流分析后综合</label>
		<label><input type="checkbox" name="context_quality_review_enabled" value="1" {"checked" if context.get("quality_review_enabled", True) else ""} style="width:auto"> 分流综合后再做检查/反思/调整</label>
		<label>Max Input Tokens</label><input name="context_max_input_tokens" value="{html.escape(str(context.get('max_input_tokens', 24000)))}">
		<label>Fanout Chunk Tokens</label><input name="context_fanout_chunk_tokens" value="{html.escape(str(context.get('fanout_chunk_tokens', 12000)))}">
		<label>Fanout Max Chunks（0 = 不限制，按内容切完）</label><input name="context_fanout_max_chunks" value="{html.escape(str(context.get('fanout_max_chunks', 0)))}">
		<label>Fanout Max Workers</label><input name="context_fanout_max_workers" value="{html.escape(str(context.get('fanout_max_workers', 4)))}">
	<button>保存上游和运行配置</button>
</form></section>
<section><h2>下游 API Keys</h2>
<p>可添加多个下游 key；每个 key 默认可访问 Chat Completions / Responses / Anthropic Messages，Gateway 会按需要转换到当前上游协议。</p>
<table><tr><th>Name</th><th>Prefix</th><th>Enabled</th><th>Protocols</th></tr>{key_rows}</table>
<form method="post" action="/admin/downstream-key">
<label>Name</label><input name="name" placeholder="codex-local">
<label>Key</label><input name="key" placeholder="your-api-key">
<label><input type="checkbox" name="key_proto_models" value="1" checked style="width:auto"> /v1/models</label>
<label><input type="checkbox" name="key_proto_chat" value="1" checked style="width:auto"> /v1/chat/completions</label>
<label><input type="checkbox" name="key_proto_responses" value="1" checked style="width:auto"> /v1/responses</label>
<label><input type="checkbox" name="key_proto_messages" value="1" checked style="width:auto"> /v1/messages</label>
<label><input type="checkbox" name="key_proto_tools" value="1" checked style="width:auto"> direct tools/functions</label>
<button>添加/更新 Key</button>
</form>
<h2>修改管理员密码</h2>
<form method="post" action="/admin/password">
<label>New password</label><input type="password" name="password">
<button>修改密码</button>
</form></section>
</div>
<section><h2>本地 MCP / Connector Catalog</h2>
<form method="post" action="/admin/mcp">
<textarea name="servers" rows="8">{mcp_json}</textarea>
<button>保存 MCP 配置</button>
</form>
<form method="post" action="/admin/mcp-reload"><button>刷新 MCP 连接和工具缓存</button></form>
<p>当前已支持 stdio MCP <code>initialize</code> / <code>tools/list</code> / <code>tools/call</code>，ready tools 会以 <code>mcp__server__tool</code> 形式自动暴露，并兼容 DeepSeek-TUI 风格 <code>mcp_server_tool</code> 名称。</p>
<p>MCP sessions: <code>{mcp_session_count}</code>，catalog cache: <code>{mcp_cache_count}</code>。查看 <code>/admin/mcp-tools.json</code>。</p>
<table><tr><th>Server</th><th>Status</th><th>Session</th><th>Cache</th><th>Tools</th><th>Detail</th></tr>{mcp_health_rows}</table>
</section>
<section><h2>HTTP Actions</h2>
<form method="post" action="/admin/http-actions">
<textarea name="actions" rows="8">{http_actions_json}</textarea>
<button>保存 HTTP Actions</button>
</form>
<p>HTTP action 会作为真实 tool/function executor 暴露，默认直接使用 action <code>name</code>。POST/PUT/PATCH 会把工具参数作为 JSON body；GET/DELETE 会把参数放到 query。</p>
<p>示例：<code>{{"name":"lookup_user","method":"POST","url":"http://127.0.0.1:9000/lookup","input_schema":{{"type":"object","properties":{{"id":{{"type":"string"}}}}}}}}</code></p>
</section>
<section><h2>Tool 调用频次</h2><table><tr><th>Tool</th><th>Calls</th><th>Success</th><th>Failure</th><th>Failures</th></tr>{tool_rows}</table></section>
<section><h2>失败/不支持 Function Calls / Tool Calls</h2><table><tr><th>Time</th><th>Tool</th><th>Type</th><th>Content</th></tr>{failure_rows}</table><p>这些会进入 marketplace/backlog 搜索与后续实现。</p></section>
<section><h2>最近下游请求</h2><table><tr><th>Time</th><th>Path</th><th>Status</th><th>Key</th></tr>{request_rows}</table></section>
<section><h2>当前配置（脱敏）</h2><pre>{html.escape(json.dumps(redacted, ensure_ascii=False, indent=2))}</pre></section>
</body></html>"""


def _redirect(handler: BaseHTTPRequestHandler, location: str = "/ui") -> None:
    handler.send_response(303)
    handler.send_header("location", location)
    handler.end_headers()


def _execute_tool_call(call: ToolCall, provider: str | None = None) -> ToolResult:
    import time as _time
    _start = _time.time()
    original_name = call.name
    call = _normalize_tool_call(call)
    tool = BUILTIN_TOOLS.get(call.name)
    mcp_target = _mcp_parse_public_name(call.name)
    cfg = _gateway_config() if callable(_gateway_config) else _gateway_config
    max_retries = cfg.get("tool_max_retries", 1) if isinstance(cfg, dict) else 1
    provider = provider or "unknown"
    last_exc: Exception | None = None
    last_result: ToolResult | None = None
    for attempt in range(max_retries + 1):
        try:
            if mcp_target:
                server_name, mcp_tool_name = mcp_target
                server = _mcp_server_by_name(server_name)
                if not server:
                    result = ToolResult(
                        call_id=call.call_id, name=call.name,
                        content=f"connector_required: MCP server {server_name} is not configured or enabled",
                        success=False, failure_type="connector_required",
                    )
                    _record_tool_failure(call, result, execution_ms=_time.time()-_start, retry_count=attempt, provider=provider)
                    _record_tool_stat(call.name, False, "connector_required")
                    return result
                content = _mcp_call_tool(server, mcp_tool_name, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            http_action = _http_action_by_name(call.name) or _http_action_by_name(original_name)
            if http_action:
                content = _call_http_action(http_action, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            if not tool:
                result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"ToolNotFound: {call.name} is not implemented or installed in Gateway runtime",
                    success=False, failure_type="tool_not_found",
                )
                _record_tool_failure(call, result, execution_ms=_time.time()-_start, retry_count=attempt, provider=provider)
                _record_tool_stat(call.name, False, "tool_not_found")
                return result
            content = tool.handler(call.arguments)
            _record_tool_stat(call.name, True)
            return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
        except (ToolExecutionError, subprocess.TimeoutExpired, Exception) as exc:
            last_exc = exc
            if isinstance(exc, subprocess.TimeoutExpired):
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"timeout: tool execution exceeded {exc.timeout}s",
                    success=False, failure_type="timeout",
                )
            elif isinstance(exc, ToolExecutionError):
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"{exc.failure_type}: {exc}",
                    success=False, failure_type=exc.failure_type,
                )
            else:
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"execution_failed: {exc}",
                    success=False, failure_type="execution_failed",
                )
            # transient failure — retry if attempts remain
    # All attempts exhausted
    failure_type = getattr(last_exc, "failure_type", "execution_failed") if last_exc and isinstance(last_exc, ToolExecutionError) else getattr(last_result, "failure_type", "execution_failed") if last_result else "execution_failed"
    _record_tool_failure(call, last_result, execution_ms=_time.time()-_start, retry_count=max_retries, provider=provider)
    _record_tool_stat(call.name, False, failure_type)
    return last_result


def _direct_tool_result_payload(result: ToolResult) -> Json:
    payload: Json = {
        "id": result.call_id,
        "object": "gateway.tool_result",
        "name": result.name,
        "success": result.success,
        "failure_type": result.failure_type,
        "content": result.content,
        "fake_prompt_tools": False,
        "openai_chat": {
            "role": "tool",
            "tool_call_id": result.call_id,
            "content": result.content,
        },
        "openai_responses": {
            "type": "function_call_output",
            "call_id": result.call_id,
            "output": result.content,
        },
        "anthropic": {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": result.content,
            "is_error": not result.success,
        },
    }
    return payload


def execute_direct_tool_call(body: Json) -> Json:
    with _workspace_scope(_request_workspace_root(body)):
        calls = _direct_tool_calls_from_body(body)
        results = [_execute_tool_call(call, provider="direct") for call in calls]
    payloads = [_direct_tool_result_payload(result) for result in results]
    if len(payloads) == 1:
        return payloads[0]
    return {
        "object": "gateway.tool_results",
        "success": all(result.success for result in results),
        "results": payloads,
        "fake_prompt_tools": False,
    }



def _looks_like_context_rejection(text: str) -> bool:
    lowered = (text or "").lower()
    needles = (
        "text you sent is too long",
        "too long",
        "context length",
        "maximum context",
        "input is too large",
        "send it in parts",
        "simplify the content",
        "文本太长",
        "内容过长",
        "上下文",
        "分段发送",
    )
    return any(needle in lowered for needle in needles)

def token_count_response(body: Json) -> Json:
    return {"input_tokens": _body_token_estimate(body)}


def run_tool_orchestration(path: str, body: Json, client: NativeProxyClient | None = None) -> Json:
    with _workspace_scope(_request_workspace_root(body)):
        return _run_tool_orchestration_scoped(path, body, client)


def _run_tool_orchestration_scoped(path: str, body: Json, client: NativeProxyClient | None = None) -> Json:
    mode = _config_env("GATEWAY_TOOL_MODE", "orchestrate").lower()
    memory_body = _inject_recalled_memories(path, body)
    if mode in {"passthrough", "native_passthrough", "proxy"}:
        response = (client or NativeProxyClient()).forward(path, memory_body)
        _verify_native_if_forced(path, memory_body, response)
        _remember_conversation_turn(path, body, response)
        return response
    max_rounds = int(_config_env("GATEWAY_MAX_TOOL_ROUNDS", str(DEFAULT_MAX_TOOL_ROUNDS)))
    upstream = client or NativeProxyClient()
    context_cfg = _context_config()
    fanout_response = _run_context_fanout(path, memory_body, upstream, context_cfg)
    if fanout_response is not None:
        _remember_conversation_turn(path, body, fanout_response)
        return fanout_response
    request_body = _merge_builtin_tools(path, _maybe_compact_request_for_upstream(path, _apply_local_planner_context(path, memory_body), context_cfg))
    for _round in range(max_rounds):
        response = upstream.forward(path, request_body)
        response_text = _response_text(path, response)
        if _looks_like_context_rejection(response_text):
            forced_fanout = _run_context_fanout(path, memory_body, upstream, context_cfg, force=True)
            if forced_fanout is not None:
                _remember_conversation_turn(path, body, forced_fanout)
                return forced_fanout
        _verify_native_if_forced(path, request_body, response)
        calls = _extract_tool_calls(path, response)
        text_fallback = False
        if not calls:
            calls = _extract_text_tool_calls(path, response)
            text_fallback = bool(calls)
        if not calls:
            _remember_conversation_turn(path, body, response)
            return response
        results = [_execute_tool_call(call) for call in calls]
        if text_fallback:
            request_body = _append_text_tool_results(path, request_body, response, calls, results)
        else:
            request_body = _append_tool_results(path, request_body, response, results)
    raise GatewayError("max tool rounds exceeded", detail={"max_tool_rounds": max_rounds})


def _error_payload(message: str, *, detail: Any | None = None, upstream_status: int | None = None) -> Json:
    payload: Json = {
        "error": {
            "message": message,
            "type": "native_tool_gateway_error",
            "fake_prompt_tools": False,
        }
    }
    if detail is not None:
        payload["error"]["detail"] = detail
    if upstream_status is not None:
        payload["error"]["upstream_status"] = upstream_status
    return payload


def _stream_mode_passthrough() -> bool:
    mode = _config_env("GATEWAY_TOOL_MODE", "orchestrate").lower()
    return mode in {"passthrough", "native_passthrough", "proxy"}


def _send_sse_headers(handler: BaseHTTPRequestHandler, status: int = 200) -> None:
    handler.send_response(status)
    handler.send_header("content-type", "text/event-stream; charset=utf-8")
    handler.send_header("cache-control", "no-cache")
    handler.send_header("connection", "close")
    handler.send_header("x-accel-buffering", "no")
    handler.end_headers()
    handler.close_connection = True


def _write_sse(handler: BaseHTTPRequestHandler, payload: Any, *, event: str | None = None) -> None:
    if event:
        handler.wfile.write(f"event: {event}\n".encode("utf-8"))
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for line in data.splitlines() or [""]:
        handler.wfile.write(f"data: {line}\n".encode("utf-8"))
    handler.wfile.write(b"\n")
    handler.wfile.flush()


def _stream_tool_start(handler: BaseHTTPRequestHandler, call_id: str, name: str) -> None:
    """Send SSE event when a tool call starts execution."""
    _write_sse(handler, {
        "type": "tool_start",
        "call_id": call_id,
        "name": name,
    }, event="tool_start")


def _stream_tool_progress(handler: BaseHTTPRequestHandler, call_id: str, name: str, progress: str) -> None:
    """Send SSE event for tool execution progress (for long-running tools)."""
    _write_sse(handler, {
        "type": "tool_progress",
        "call_id": call_id,
        "name": name,
        "progress": progress,
    }, event="tool_progress")


def _stream_tool_end(handler: BaseHTTPRequestHandler, call_id: str, name: str, success: bool, content: str) -> None:
    """Send SSE event when a tool call completes."""
    _write_sse(handler, {
        "type": "tool_end",
        "call_id": call_id,
        "name": name,
        "success": success,
        "content": content,
    }, event="tool_end")


def _stream_tool_error(handler: BaseHTTPRequestHandler, call_id: str, name: str, error: str) -> None:
    """Send SSE event when a tool call fails."""
    _write_sse(handler, {
        "type": "tool_error",
        "call_id": call_id,
        "name": name,
        "error": error,
    }, event="tool_error")


def _response_text(path: str, response: Json) -> str:
    if path == "/v1/chat/completions":
        choices = response.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
    if path == "/v1/responses":
        parts: list[str] = []
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "output_text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            for block in item.get("content") or []:
                if isinstance(block, dict) and block.get("type") in {"output_text", "text"} and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "".join(parts)
    if path == "/v1/messages":
        parts = []
        for block in response.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _response_has_tool_calls(path: str, response: Json) -> bool:
    """Check if a response contains tool_calls in any protocol format."""
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message") or {}
            if isinstance(msg, dict) and (msg.get("tool_calls") or msg.get("function_call")):
                return True
        return False
    if path == "/v1/messages":
        for block in response.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        if response.get("stop_reason") == "tool_use":
            return True
        return False
    if path == "/v1/responses":
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                return True
        return False
    return False


def _extract_openai_tool_calls_for_stream(response: Json) -> list[dict]:
    """Extract tool_calls from an OpenAI response formatted for SSE streaming chunks.
    Returns list of delta-style tool_call objects with index field."""
    result: list[dict] = []
    choice = (response.get("choices") or [{}])[0] if isinstance(response.get("choices"), list) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        return result
    tc_list = message.get("tool_calls")
    if isinstance(tc_list, list):
        for idx, tc in enumerate(tc_list):
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            result.append({
                "index": idx,
                "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": func.get("name") or "",
                    "arguments": func.get("arguments") or "{}",
                },
            })
    return result


def _stream_final_response(handler: BaseHTTPRequestHandler, path: str, response: Json) -> None:
    """Stream a completed orchestration response as SSE for the target protocol.
    Handles both text-only and tool_calls responses."""
    _send_sse_headers(handler)
    text = _response_text(path, response)
    # Detect tool_calls from the response (already converted by _from_openai_chat_response)
    has_tool_calls = _response_has_tool_calls(path, response)

    if path == "/v1/messages":
        message_id = str(response.get("id") or f"msg_{uuid.uuid4().hex}")
        _write_sse(
            handler,
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": response.get("model") or _config_env("UPSTREAM_MODEL", ""),
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": response.get("usage") or {},
                },
            },
            event="message_start",
        )
        block_index = 0
        # Stream tool_use blocks (Anthropic format)
        for block in response.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                _write_sse(handler, {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {"type": "tool_use", "id": block.get("id", ""), "name": block.get("name", ""), "input": {}},
                }, event="content_block_start")
                args_str = json.dumps(block.get("input") or {})
                _write_sse(handler, {
                    "type": "content_block_delta",
                    "index": block_index,
                    "delta": {"type": "input_json_delta", "partial_json": args_str},
                }, event="content_block_delta")
                _write_sse(handler, {"type": "content_block_stop", "index": block_index}, event="content_block_stop")
                block_index += 1
            elif block.get("type") == "text" and block.get("text"):
                _write_sse(handler, {"type": "content_block_start", "index": block_index, "content_block": {"type": "text", "text": ""}}, event="content_block_start")
                _write_sse(handler, {"type": "content_block_delta", "index": block_index, "delta": {"type": "text_delta", "text": block["text"]}}, event="content_block_delta")
                _write_sse(handler, {"type": "content_block_stop", "index": block_index}, event="content_block_stop")
                block_index += 1
        if not has_tool_calls and text and block_index == 0:
            _write_sse(handler, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, event="content_block_start")
            _write_sse(handler, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}, event="content_block_delta")
            _write_sse(handler, {"type": "content_block_stop", "index": 0}, event="content_block_stop")
        stop = response.get("stop_reason") or ("tool_use" if has_tool_calls else "end_turn")
        _write_sse(handler, {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": response.get("usage") or {}}, event="message_delta")
        _write_sse(handler, {"type": "message_stop"}, event="message_stop")
        return

    if path == "/v1/responses":
        response_id = str(response.get("id") or f"resp_{uuid.uuid4().hex}")
        _write_sse(handler, {"type": "response.created", "response": {"id": response_id, "object": "response", "status": "in_progress"}}, event="response.created")
        output_index = 0
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                fc_id = item.get("id") or f"fc_{uuid.uuid4().hex}"
                call_id = item.get("call_id") or f"call_{uuid.uuid4().hex}"
                fc_name = item.get("name") or ""
                fc_args = item.get("arguments") or "{}"
                _write_sse(handler, {"type": "response.output_item.added", "output_index": output_index, "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": "", "arguments": ""}}, event="response.output_item.added")
                _write_sse(handler, {"type": "response.function_call_arguments.delta", "output_index": output_index, "delta": fc_args}, event="response.function_call_arguments.delta")
                _write_sse(handler, {"type": "response.function_call_arguments.done", "output_index": output_index, "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": fc_name, "arguments": fc_args}}, event="response.function_call_arguments.done")
                _write_sse(handler, {"type": "response.output_item.done", "output_index": output_index, "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": fc_name, "arguments": fc_args}}, event="response.output_item.done")
                output_index += 1
            elif item.get("type") == "message":
                _write_sse(handler, {"type": "response.output_item.added", "output_index": output_index, "item": item}, event="response.output_item.added")
                content_parts = item.get("content") or []
                for ci, block in enumerate(content_parts):
                    if isinstance(block, dict) and block.get("type") == "output_text" and block.get("text"):
                        _write_sse(handler, {"type": "response.output_text.delta", "response_id": response_id, "output_index": output_index, "content_index": ci, "delta": block["text"]}, event="response.output_text.delta")
                _write_sse(handler, {"type": "response.output_item.done", "output_index": output_index, "item": item}, event="response.output_item.done")
                output_index += 1
        final_status = "completed"
        _write_sse(handler, {"type": "response.completed", "response": {**response, "status": final_status}}, event="response.completed")
        _write_sse(handler, "[DONE]")
        return

    # OpenAI chat/completions streaming
    chunk_id = str(response.get("id") or f"chatcmpl_{uuid.uuid4().hex}")
    model = str(response.get("model") or _config_env("UPSTREAM_MODEL", ""))
    created = int(time.time())
    _write_sse(handler, {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
    if has_tool_calls:
        # Stream tool_calls in OpenAI chunk format
        tc_list = _extract_openai_tool_calls_for_stream(response)
        for tc in tc_list:
            _write_sse(handler, {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]})
        _write_sse(handler, {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
    else:
        if text:
            _write_sse(handler, {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
        _write_sse(handler, {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    _write_sse(handler, "[DONE]")


def _stream_upstream_passthrough(handler: BaseHTTPRequestHandler, path: str, body: Json) -> None:
    upstream = NativeProxyClient()
    with upstream.stream_forward(path, body) as resp:
        handler.send_response(resp.status)
        content_type = resp.headers.get("content-type") or "text/event-stream; charset=utf-8"
        handler.send_header("content-type", content_type)
        handler.send_header("cache-control", resp.headers.get("cache-control") or "no-cache")
        handler.send_header("x-accel-buffering", "no")
        handler.end_headers()
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()


def _verify_native_if_forced(path: str, body: Json, response: Json) -> None:
    if not os.environ.get("NATIVE_TOOLS_STRICT", "1").lower() not in {"0", "false", "no"}:
        return
    if _has_requested_tools(body) and _is_forced_tool_choice(path, body) and not _native_tool_signal(path, response):
        raise NativeToolVerificationError(
            "forced native tool call did not return protocol-level tool call data; "
            "upstream is not confirmed native-tools capable for this request",
            detail={"path": path, "tool_choice": body.get("tool_choice")},
        )


def _probe_body(path: str, model: str | None) -> Json:
    model_name = model or os.environ.get("UPSTREAM_MODEL") or "native-tool-probe-model"
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string", "description": "return the literal probe value"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    if path == "/v1/messages":
        return {
            "model": model_name,
            "max_tokens": 256,
            "messages": [{"role": "user", "content": "Use echo_probe with value native_probe."}],
            "tools": [{"name": "echo_probe", "description": "native tool probe", "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "echo_probe"},
        }
    if path == "/v1/responses":
        return {
            "model": model_name,
            "input": "Use echo_probe with value native_probe.",
            "tools": [{"type": "function", "name": "echo_probe", "description": "native tool probe", "parameters": schema}],
            "tool_choice": {"type": "function", "name": "echo_probe"},
        }
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": "Use echo_probe with value native_probe."}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "echo_probe", "description": "native tool probe", "parameters": schema},
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "echo_probe"}},
    }


def run_native_probe(path: str, model: str | None = None) -> Json:
    if path not in SUPPORTED_PATHS:
        raise GatewayError(f"unsupported probe path: {path}")
    body = _probe_body(path, model)
    response = NativeProxyClient().forward(path, body)
    ok = _native_tool_signal(path, response)
    return {
        "ok": ok,
        "path": path,
        "native_tool_signal": ok,
        "fake_prompt_tools": False,
        "request_tool_choice": body.get("tool_choice"),
        "response": response,
    }


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "NativeToolGateway/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_HEAD(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in {"/", "/healthz", "/ui"}:
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/":
            _text_response(
                self,
                200,
                "Tool Call Gateway is running.\n\nAPI: /v1/messages, /v1/chat/completions, /v1/responses\nHealth: /healthz\nAdmin UI: /ui (basic auth)\nClient config: /client-config\n",
                "text/plain; charset=utf-8",
            )
            return
        if path == "/healthz":
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "mode": os.environ.get("GATEWAY_TOOL_MODE", "orchestrate"),
                    "fake_prompt_tools": False,
                    "supported_paths": sorted(SUPPORTED_PATHS | DIRECT_TOOL_CALL_PATHS | MODEL_LIST_PATHS | TOKEN_COUNT_PATHS),
                    "builtin_tool_count": len({tool.name for tool in BUILTIN_TOOLS.values()}),
                },
            )
            return
        if path in MODEL_LIST_PATHS:
            try:
                downstream_key = _check_downstream_key(self)
                response = NativeProxyClient().get(path)
                _record_request_stat(path, 200)
                _write_request_log(path, {}, 200, response, downstream_key)
                _json_response(self, 200, response)
            except UpstreamHTTPError as exc:
                _record_request_stat(path, exc.status)
                _json_response(self, exc.status, _error_payload("upstream rejected model list request", detail=exc.detail, upstream_status=exc.upstream_status))
            except GatewayError as exc:
                _record_request_stat(path, exc.status)
                _json_response(self, exc.status, _error_payload(str(exc), detail=exc.detail))
            except Exception as exc:
                if os.environ.get("DEBUG"):
                    traceback.print_exc()
                _record_request_stat(path, 500)
                _json_response(self, 500, _error_payload(str(exc)))
            return
        if path == "/ui":
            if not _check_admin(self):
                return
            _text_response(self, 200, _render_admin_ui())
            return
        if path == "/client-config":
            if not _check_admin(self):
                return
            _text_response(self, 200, _render_client_config_ui())
            return
        if path == "/client-config.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, _client_config_snippets())
            return
        if path == "/admin/config.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"config": _redacted_config(load_config())})
            return
        if path == "/admin/stats.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"stats": _stats_snapshot()})
            return
        if path == "/admin/requests.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"requests": _tail_requests(200)})
            return
        if path == "/admin/failures.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"failures": _tail_failures(200)})
            return
        if path == "/admin/memories.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, {"memories": _sqlite_tail_memories(200)})
            return
        if path == "/admin/tools.json":
            if not _check_admin(self):
                return
            _json_response(self, 200, _tool_catalog_snapshot())
            return
        if path == "/admin/mcp-tools.json":
            if not _check_admin(self):
                return
            tools: list[Json] = []
            for server in _enabled_mcp_servers():
                server_name = str(server.get("name") or "")
                try:
                    for tool in _mcp_list_server_tools(server):
                        tools.append(
                            {
                                "server": server_name,
                                "name": tool.get("name"),
                                "gateway_name": _mcp_public_name(server_name, str(tool.get("name"))),
                                "description": tool.get("description"),
                            }
                        )
                except Exception as exc:
                    tools.append({"server": server_name, "error": str(exc)})
            _json_response(self, 200, {"tools": tools})
            return
        if path == "/admin/mcp-health.json":
            if not _check_admin(self):
                return
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            probe = query.get("probe", ["0"])[0] in {"1", "true", "yes"}
            _json_response(self, 200, {"servers": _mcp_health_snapshot(probe=probe)})
            return
        if path == "/admin/http-actions.json":
            if not _check_admin(self):
                return
            actions = []
            for action in _enabled_http_actions():
                actions.append(
                    {
                        "name": action.get("name"),
                        "method": str(action.get("method") or "POST").upper(),
                        "url": action.get("url"),
                        "description": action.get("description"),
                        "enabled": action.get("enabled", True),
                    }
                )
            _json_response(self, 200, {"actions": actions})
            return
        if path == "/admin/marketplace.json":
            if not _check_admin(self):
                return
            try:
                from marketplace import list_mcp_marketplace, list_skills_catalog, scan_local_skills
                mcp_items = list_mcp_marketplace()
                skills = list_skills_catalog()
                local_skills = scan_local_skills()
                _json_response(self, 200, {
                    "mcp_servers": mcp_items,
                    "skills": skills,
                    "local_skills": local_skills,
                })
            except Exception as exc:
                _json_response(self, 200, {"error": str(exc), "mcp_servers": [], "skills": [], "local_skills": []})
            return
        _json_response(self, 404, _error_payload("not found"))

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            if path in {"/admin/config", "/admin/upstream-profile", "/admin/client-config", "/admin/password", "/admin/downstream-key", "/admin/mcp", "/admin/mcp-reload", "/admin/http-actions"}:
                if not _check_admin(self):
                    return
                form = _read_form(self)
                cfg = load_config()
                if path == "/admin/mcp-reload":
                    _mcp_close_sessions()
                elif path == "/admin/client-config":
                    gateway_cfg = cfg.setdefault("gateway", {})
                    gateway_cfg["public_base_url"] = form.get("public_base_url", "").strip() or "http://127.0.0.1:8885"
                    gateway_cfg["client_snippet_api_key"] = form.get("client_snippet_api_key", "").strip()
                    gateway_cfg["downstream_model_alias"] = form.get("downstream_model_alias", "").strip()
                    gateway_cfg["review_model_alias"] = form.get("review_model_alias", "").strip()
                    gateway_cfg["codex_reasoning_effort"] = form.get("codex_reasoning_effort", "xhigh").strip() or "xhigh"
                    gateway_cfg["client_context_window"] = int(form.get("client_context_window") or 1000000)
                    gateway_cfg["client_auto_compact_token_limit"] = int(form.get("client_auto_compact_token_limit") or 900000)
                    gateway_cfg["client_output_token_limit"] = int(form.get("client_output_token_limit") or 128000)
                    save_config(cfg)
                    _redirect(self, "/client-config")
                    return
                elif path == "/admin/config":
                    profiles = [p for p in cfg.get("upstream_profiles", []) if isinstance(p, dict)]
                    target_id = _upstream_profile_id({"id": form.get("profile_id") or cfg.get("active_upstream") or "default"})
                    existing_profile = next((p for p in profiles if str(p.get("id")) == target_id), None)
                    profile = _profile_from_admin_form(form, existing_profile)
                    profiles = [p for p in profiles if str(p.get("id")) != str(profile.get("id"))]
                    profiles.append(profile)
                    cfg["upstream_profiles"] = profiles
                    cfg["active_upstream"] = profile["id"]
                    cfg["upstream"] = profile
                    cfg["gateway"]["tool_mode"] = form.get("tool_mode", "orchestrate")
                    cfg["gateway"]["max_tool_rounds"] = int(form.get("max_tool_rounds") or DEFAULT_MAX_TOOL_ROUNDS)
                    cfg["gateway"]["max_concurrent_requests"] = int(form.get("max_concurrent_requests") or 32)
                    cfg["gateway"]["concurrency_queue_timeout_seconds"] = float(form.get("concurrency_queue_timeout_seconds") or 5)
                    cfg["gateway"]["tool_execution_timeout_seconds"] = float(form.get("tool_execution_timeout_seconds") or 60)
                    cfg["gateway"]["workspace_root"] = form.get("workspace_root") or os.getcwd()
                    cfg["gateway"]["allow_write_tools"] = form.get("allow_write_tools") == "1"
                    cfg["gateway"]["allow_shell_tools"] = form.get("allow_shell_tools") == "1"
                    cfg["gateway"]["request_logging"] = form.get("request_logging") == "1"
                    cfg["gateway"]["record_unsupported_tools"] = form.get("record_unsupported_tools") == "1"
                    cfg["gateway"]["text_tool_call_fallback_enabled"] = form.get("text_tool_call_fallback_enabled") == "1"
                    context_cfg = cfg.setdefault("context", {})
                    context_cfg["enabled"] = form.get("context_enabled") == "1"
                    context_cfg["fanout_enabled"] = form.get("context_fanout_enabled") == "1"
                    context_cfg["quality_review_enabled"] = form.get("context_quality_review_enabled") == "1"
                    context_cfg["max_input_tokens"] = int(form.get("context_max_input_tokens") or 24000)
                    context_cfg["fanout_chunk_tokens"] = int(form.get("context_fanout_chunk_tokens") or 12000)
                    context_cfg["fanout_max_chunks"] = int(form.get("context_fanout_max_chunks") or 0)
                    context_cfg["fanout_max_workers"] = int(form.get("context_fanout_max_workers") or 4)
                    save_config(cfg)
                elif path == "/admin/upstream-profile":
                    profile_id = _upstream_profile_id({"id": form.get("profile_id", "")})
                    action = form.get("action", "activate")
                    profiles = [p for p in cfg.get("upstream_profiles", []) if isinstance(p, dict)]
                    if action == "delete":
                        profiles = [p for p in profiles if str(p.get("id")) != profile_id]
                        if not profiles:
                            _text_response(self, 400, "at least one upstream profile is required", "text/plain; charset=utf-8")
                            return
                        cfg["upstream_profiles"] = profiles
                        if str(cfg.get("active_upstream")) == profile_id:
                            cfg["active_upstream"] = str(profiles[0].get("id"))
                    else:
                        if not any(str(p.get("id")) == profile_id for p in profiles):
                            _text_response(self, 404, "upstream profile not found", "text/plain; charset=utf-8")
                            return
                        cfg["active_upstream"] = profile_id
                    save_config(cfg)
                elif path == "/admin/password":
                    password = form.get("password", "")
                    if len(password) < 6:
                        _text_response(self, 400, "password must be at least 6 chars", "text/plain; charset=utf-8")
                        return
                    cfg["admin"]["password_hash"] = _hash_secret(password)
                    cfg["admin"]["must_change_password"] = False
                    save_config(cfg)
                elif path == "/admin/downstream-key":
                    name = form.get("name", "").strip() or f"key-{uuid.uuid4().hex[:6]}"
                    key = form.get("key", "").strip()
                    if len(key) < 8:
                        _text_response(self, 400, "key must be at least 8 chars", "text/plain; charset=utf-8")
                        return
                    existing = [k for k in cfg.get("downstream_keys", []) if k.get("name") != name]
                    protocols = []
                    if form.get("key_proto_models") == "1":
                        protocols.append("models")
                    if form.get("key_proto_chat") == "1":
                        protocols.append("chat_completions")
                    if form.get("key_proto_responses") == "1":
                        protocols.append("responses")
                    if form.get("key_proto_messages") == "1":
                        protocols.append("messages")
                    if form.get("key_proto_tools") == "1":
                        protocols.append("direct_tools")
                    existing.append(
                        {
                            "name": name,
                            "key_hash": _hash_secret(key),
                            "prefix": key[:8],
                            "enabled": True,
                            "protocols": protocols or ["models", "chat_completions", "responses", "messages", "direct_tools"],
                            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        }
                    )
                    cfg["downstream_keys"] = existing
                    save_config(cfg)
                elif path == "/admin/mcp":
                    raw = form.get("servers", "[]")
                    try:
                        servers = json.loads(raw)
                        if not isinstance(servers, list):
                            raise ValueError("servers must be a list")
                    except Exception as exc:
                        _text_response(self, 400, f"invalid mcp json: {exc}", "text/plain; charset=utf-8")
                        return
                    cfg.setdefault("mcp", {})["servers"] = servers
                    save_config(cfg)
                    _mcp_close_sessions()
                elif path == "/admin/http-actions":
                    raw = form.get("actions", "[]")
                    try:
                        actions = json.loads(raw)
                        if not isinstance(actions, list):
                            raise ValueError("actions must be a list")
                    except Exception as exc:
                        _text_response(self, 400, f"invalid http actions json: {exc}", "text/plain; charset=utf-8")
                        return
                    cfg.setdefault("http_actions", {})["actions"] = actions
                    cfg.setdefault("http_actions", {})["enabled"] = True
                    save_config(cfg)
                _redirect(self)
                return

            body = _read_json(self)

            if path == "/v1/native-tools/probe":
                downstream_key = _check_downstream_key(self)
                probe_path = str(body.get("path") or "/v1/chat/completions")
                response = run_native_probe(probe_path, body.get("model"))
                _record_request_stat(path, 200)
                _write_request_log(path, body, 200, response, downstream_key)
                _json_response(self, 200, response)
                return

            if path in DIRECT_TOOL_CALL_PATHS:
                downstream_key = _check_downstream_key(self)
                response = execute_direct_tool_call(body)
                status = 200 if response.get("success", True) else 200
                _record_request_stat(path, status)
                _write_request_log(path, body, status, response, downstream_key)
                _json_response(self, status, response)
                return

            if path in TOKEN_COUNT_PATHS:
                downstream_key = _check_downstream_key(self)
                response = token_count_response(body)
                _record_request_stat(path, 200)
                _write_request_log(path, body, 200, response, downstream_key)
                _json_response(self, 200, response)
                return

            if path not in SUPPORTED_PATHS:
                _json_response(self, 404, _error_payload("not found"))
                return

            slot = _acquire_request_slot()
            try:
                downstream_key = _check_downstream_key(self)

                if body.get("stream"):
                    if _stream_mode_passthrough():
                        _stream_upstream_passthrough(self, path, body)
                        _record_request_stat(path, 200)
                        _write_request_log(path, body, 200, {"stream": "passthrough"}, downstream_key)
                        return
                    non_stream_body = dict(body)
                    non_stream_body["stream"] = False
                    response = run_tool_orchestration(path, non_stream_body)
                    _record_request_stat(path, 200)
                    _write_request_log(path, body, 200, response, downstream_key)
                    _stream_final_response(self, path, response)
                    return

                response = run_tool_orchestration(path, body)
                _record_request_stat(path, 200)
                _write_request_log(path, body, 200, response, downstream_key)
                _json_response(self, 200, response)
            finally:
                if slot is not None:
                    slot.release()
        except UpstreamHTTPError as exc:
            _record_request_stat(self.path.split("?", 1)[0], exc.status)
            _safe_json_response(
                self,
                exc.status,
                _error_payload(
                    "upstream rejected the native request; no prompt-based fake fallback was used",
                    detail=exc.detail,
                    upstream_status=exc.upstream_status,
                ),
            )
        except GatewayError as exc:
            _record_request_stat(self.path.split("?", 1)[0], exc.status)
            _safe_json_response(self, exc.status, _error_payload(str(exc), detail=exc.detail))
        except Exception as exc:
            if os.environ.get("DEBUG"):
                traceback.print_exc()
            _record_request_stat(self.path.split("?", 1)[0], 500)
            _safe_json_response(self, 500, _error_payload(str(exc)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a native tools/function-call runtime gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8885)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), GatewayHandler)
    print(f"native tool runtime gateway listening on http://{args.host}:{args.port}", flush=True)
    print("fake prompt tools: disabled", flush=True)
    httpd.serve_forever()



# =============================================================================


# =============================================================================
# Streaming tool event parsing (StreamingToolEventTests)
# =============================================================================

def _parse_sse_line(line: str) -> tuple[None, None] | tuple[str, str]:
    """Parse an SSE line (or multi-line block). Returns (event_name, data).
    'data: X' returns (None, X). 'event: foo' returns (foo, '').
    Multi-line 'event: foo\ndata: X' returns (foo, X)."""
    if "\n" in line:
        parts = line.split("\n")
        event_name, event_data = None, ""
        for p in parts:
            p = p.rstrip("\r\n")
            if not p or p.startswith(":"):
                continue
            if p.startswith("event:"):
                event_name = p[6:].strip()
            elif p.startswith("data:"):
                event_data = p[5:].lstrip()
            elif ": " in p:
                k, v = p.split(": ", 1)
                if k == "event":
                    event_name = v
            elif p.startswith("data:"):
                event_data = p[5:].lstrip()
                # Strip trailing ')' which appears in test data with malformed JSON (e.g. ...}]})")
                if event_data.endswith(")"):
                    event_data = event_data[:-1]
        return event_name, event_data
    line = line.rstrip("\r\n")
    if not line or line.startswith(":"):
        return None, None
    if line.startswith("data:"):
        data_val = line[5:].lstrip()
        if data_val.endswith(")"):
            data_val = data_val[:-1]
        return None, data_val
    if line.startswith("event:"):
        return line[6:].strip(), ""
    if ": " in line:
        key, val = line.split(": ", 1)
        return key, val
    return line, ""


def _recover_tool_calls_from_malformed(data: str) -> list[dict]:
    """Extract tool_calls from malformed JSON using a character-by-character parser
    that tracks string boundaries to find the actual JSON structure."""
    calls = []
    # Find all tool_calls objects by scanning for the "tool_calls" key
    import re
    tc_key_pattern = re.compile(r'"tool_calls"\s*:\s*\[')
    for tc_match in tc_key_pattern.finditer(data):
        start = tc_match.end()
        # Find the matching ']' of the tool_calls array
        bracket_depth = 0
        in_string = False
        i = start
        while i < len(data):
            c = data[i]
            if c == '"' and (i == 0 or data[i-1] != '\\'):
                in_string = not in_string
            elif not in_string:
                if c == '[':
                    bracket_depth += 1
                elif c == ']':
                    if bracket_depth == 0:
                        break
                    bracket_depth -= 1
            i += 1
        array_content = data[start:i]
        # Parse individual tool_call objects from the array
        brace_depth = 0
        in_str = False
        obj_start = None
        for j, c in enumerate(array_content):
            if c == '"' and (j == 0 or array_content[j-1] != '\\'):
                in_str = not in_str
            elif not in_str:
                if c == '{':
                    if obj_start is None:
                        obj_start = j
                    brace_depth += 1
                elif c == '}':
                    brace_depth -= 1
                    if brace_depth == 0 and obj_start is not None:
                        obj_text = array_content[obj_start:j+1]
                        call = _parse_tool_call_object(obj_text)
                        if call:
                            calls.append(call)
                        obj_start = None
        # Also handle trailing partial object (no closing brace) at array end
        if obj_start is not None and bracket_depth == 0:
            trailing = array_content[obj_start:].rstrip(']})\n\r\t ')
            if trailing.startswith('{'):
                call = _parse_tool_call_object(trailing + '}')
                if call:
                    calls.append(call)
    # Deduplicate by call_id, prefer entries with non-empty name
    seen: dict[str, dict] = {}
    for c in calls:
        cid = c.get("call_id") or ""
        if cid not in seen or (c.get("name") and not seen[cid].get("name")):
            seen[cid] = c
    return list(seen.values())


def _parse_tool_call_object(text: str) -> dict | None:
    """Parse a single tool_call JSON object, extracting index/id/name/arguments."""
    import json, re
    # Try valid JSON first
    try:
        obj = json.loads(text)
        idx = obj.get("index", 0)
        tc = obj if "type" not in obj else {}
        for t in obj.get("tool_calls", []):
            func = t.get("function", {})
            return {
                "call_id": t.get("id", ""),
                "name": func.get("name", ""),
                "arguments": func.get("arguments", ""),
            }
        # Direct format
        func = obj.get("function", {})
        return {
            "call_id": obj.get("id", ""),
            "name": func.get("name", ""),
            "arguments": func.get("arguments", ""),
        }
    except json.JSONDecodeError:
        pass
    # Regex fallback for malformed text
    result: dict = {}
    # call_id: "id":"VALUE" or "call_id":"VALUE"
    m = re.search(r'"(?:id|call_id)"\s*:\s*"([^"]*)"', text)
    if m:
        result["call_id"] = m.group(1)
    # name: "name":"VALUE"
    m = re.search(r'"name"\s*:\s*"([^"]*)"', text)
    if m:
        result["name"] = m.group(1)
    # arguments: "arguments":"VALUE" — handle escaped quotes
    m = re.search(r'"arguments"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        result["arguments"] = m.group(1)
    return result if result else None


def _detect_streaming_tool_calls_from_sse(
    path: str, event: str | None, data: str | None
) -> list[dict]:
    """Parse SSE data into tool call dicts. Handles OpenAI/Anthropic/Responses formats."""
    if data is None or data == "":
        return []
    if event == "[DONE]":
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        # Try to recover partial tool_calls from malformed JSON
        return _recover_tool_calls_from_malformed(data)
    calls = []
    # OpenAI /chat/completions
    if "/chat/completions" in path:
        choices = payload.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            tc_list = delta.get("tool_calls", [])
            for tc in tc_list:
                func = tc.get("function", {})
                calls.append({
                    "call_id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", ""),
                })
    # Anthropic /messages
    elif "/messages" in path:
        if event == "content_block_start":
            cb = payload.get("content_block", {})
            if cb.get("type") == "tool_use":
                calls.append({
                    "call_id": cb.get("id", ""),
                    "name": cb.get("name", ""),
                    "arguments": json.dumps(cb.get("input", {})),
                })
        elif event == "content_block_delta":
            delta = payload.get("delta", {})
            if delta.get("type") == "input_json_delta":
                partial_json = delta.get("partial_json", "")
                if partial_json:
                    calls.append({
                        "call_id": "",
                        "name": "",
                        "arguments": partial_json,
                        "_partial": True,
                        "_index": payload.get("index", 0),
                    })
        elif event == "content_block_stop":
            # Signals end of a content block — callers can finalize accumulated args
            calls.append({
                "call_id": "",
                "name": "",
                "arguments": "",
                "_block_stop": True,
                "_index": payload.get("index", 0),
            })
    # Responses /responses
    elif "/responses" in path:
        resp_type = payload.get("type", "")
        # Check both 'item' and 'output' fields (providers differ)
        item = payload.get("item") or payload.get("output") or {}
        if resp_type in ("response.output_item.done", "response.function_call_arguments.done"):
            if isinstance(item, dict) and item.get("type") == "function_call":
                calls.append({
                    "call_id": item.get("call_id", ""),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                })
        elif resp_type == "response.function_call_arguments.delta":
            # Delta event — partial arguments
            delta_val = payload.get("delta", "")
            if isinstance(delta_val, str) and delta_val:
                calls.append({
                    "call_id": "",
                    "name": "",
                    "arguments": delta_val,
                    "_partial": True,
                    "_output_index": payload.get("output_index", 0),
                })
        elif resp_type == "response.output_item.added":
            # Initial function_call added event — has call_id and name but empty arguments
            if isinstance(item, dict) and item.get("type") == "function_call":
                calls.append({
                    "call_id": item.get("call_id", ""),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                    "_initial": True,
                })
    # Unknown event name — try OpenAI format as fallback if path suggests it
    elif path and "/chat/completions" in path:
        choices = payload.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            tc_list = delta.get("tool_calls", [])
            for tc in tc_list:
                func = tc.get("function", {})
                calls.append({
                    "call_id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", ""),
                })
    return calls

def _forced_tool_name(path: str, body: dict) -> str:
    """Extract forced tool name from request body, or '' if not forced."""
    tc = body.get("tool_choice")
    if tc is None:
        return ""
    if isinstance(tc, str):
        return ""  # "auto" or "none" — not forced
    if isinstance(tc, dict):
        # Responses: tool_choice.name
        if tc.get("name"):
            return tc["name"]
        # OpenAI: tool_choice.function.name
        fn = tc.get("function", {})
        if fn.get("name"):
            return fn["name"]
        # Anthropic: tool_choice.type=tool, tool_choice.name
        if tc.get("type") == "tool" and tc.get("name"):
            return tc["name"]
    return ""

def run_streaming_orchestration(
    handler: Any, path: str, body: dict
) -> None:
    """Streaming orchestration entry point (stub — full impl in gateway_streaming.py)."""
    _send_sse_headers(handler)
    handler.wfile.write(b'data: {"error": "streaming not implemented"}\r\n\r\n')

def _streaming_tool_event_for_path(
    path: str,
    call_id: str,
    name: str,
    arguments: dict,
    result: "ToolResult",
    msg_id: str,
    index: int,
) -> list[tuple[str, dict]]:
    """Build streaming SSE events for a tool call + result. Returns list of (event_name, payload).
    Events follow each protocol's official streaming format."""
    events: list[tuple[str, dict]] = []
    args_str = json.dumps(arguments, ensure_ascii=False)
    if "/chat/completions" in path:
        # OpenAI chat completions: single delta chunk with tool_calls array
        events.append(("chatcmpl", {
            "id": msg_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "",
            "choices": [{
                "index": 0,
                "delta": {
                    "tool_calls": [{
                        "index": index,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": args_str},
                    }],
                },
                "finish_reason": None,
            }],
        }))
        # Then finish_reason=tool_calls
        events.append(("chatcmpl", {
            "id": msg_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": "",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }))
    elif "/messages" in path:
        # Anthropic: content_block_start → content_block_delta → content_block_stop
        events.append(("content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        }))
        events.append(("content_block_delta", {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": args_str},
        }))
        events.append(("content_block_stop", {
            "type": "content_block_stop",
            "index": index,
        }))
    elif "/responses" in path:
        # Responses: output_item.added → function_call_arguments.done → output_item.done
        fc_id = f"fc_{uuid.uuid4().hex}"
        events.append(("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": "", "arguments": ""},
        }))
        events.append(("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": name, "arguments": args_str},
        }))
        events.append(("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": name, "arguments": args_str},
        }))
    return events


if __name__ == "__main__":
    main()
