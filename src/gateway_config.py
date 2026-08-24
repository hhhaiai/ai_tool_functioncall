#!/usr/bin/env python3
"""Configuration management for the gateway.

Handles loading, saving, and merging configuration from files and environment variables.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import tempfile
import threading
import uuid
from functools import lru_cache
from typing import Any

from .gateway_errors import ConfigConflictError, ConfigError

Json = dict[str, Any]

CONFIG_PATH = pathlib.Path(os.environ.get("GATEWAY_CONFIG_PATH") or ".gateway_service.json")
CONFIG_LOCK = threading.RLock()

# Canonical set of API path constants (shared by gateway_tool_runtime and gateway_http_handler)
SUPPORTED_PATHS = {
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/assistants",
    "/v1/threads",
}
MODEL_LIST_PATHS = {"/v1/models"}
TOKEN_COUNT_PATHS = {"/v1/messages/count_tokens", "/v1/chat/completions/count_tokens"}
DIRECT_TOOL_CALL_PATHS = {"/v1/tools/call", "/v1/functions/call", "/tools/call"}
WEB2API_PATHS = {"/v1/web2api", "/api/web2api"}
ANTHROPIC_COMPAT_PREFIX = "/anthropic"

# Canonical model capability registry.
#
# Each upstream model declares which capabilities it actually supports.  The
# Gateway routes tool traffic (tool call, function call, web search, image /
# music / video recognition, ...) to a model that declares the matching
# capability instead of assuming the whole profile is uniform.
#
# `kind` groups capabilities for the Config Center matrix UI:
#   tool      - tool-call / function-call / web-search style
#   recognition - upstream-side content understanding (image / music / video / audio / speech)
#   protocol  - transport-level protocol features (streaming / json schema / network / vision)
MODEL_CAPABILITY_SPEC: list[tuple[str, str, str]] = [
    ("supports_tools",              "Tool Call",        "tool"),
    ("supports_function_calls",     "Function Call",    "tool"),
    ("supports_parallel_tool_calls", "Parallel Tools",   "tool"),
    ("supports_web_search",         "Web Search",       "tool"),
    ("supports_image_recognition",  "Recognize Image",  "recognition"),
    ("supports_music_recognition",  "Recognize Music",  "recognition"),
    ("supports_video_recognition",  "Recognize Video",  "recognition"),
    ("supports_audio_recognition",  "Recognize Audio",  "recognition"),
    ("supports_speech",             "Speech to Text",   "recognition"),
    ("supports_streaming",          "Streaming",        "protocol"),
    ("supports_json_schema",        "JSON Schema",      "protocol"),
    ("supports_network",            "Network",          "protocol"),
    ("supports_vision",             "Vision (protocol)", "protocol"),
]

MODEL_CAPABILITY_KEYS: tuple[str, ...] = tuple(spec[0] for spec in MODEL_CAPABILITY_SPEC)
MODEL_CAPABILITY_LABELS: dict[str, str] = {spec[0]: spec[1] for spec in MODEL_CAPABILITY_SPEC}
MODEL_CAPABILITY_KINDS: dict[str, str] = {spec[0]: spec[2] for spec in MODEL_CAPABILITY_SPEC}

TOOLS_ENABLED_DISABLED_ALIASES: frozenset[str] = frozenset(
    {"off", "disabled", "false", "0", "none"}
)


def canonical_tools_enabled(value: Any, *, default: str = "adapter") -> str:
    """Return the canonical, case-insensitive upstream tools mode.

    Historical config and environment inputs used five equivalent disabled
    spellings.  Persist and expose one value (``off``) while accepting every
    legacy alias at runtime and at Admin/config boundaries.
    """
    raw = default if value is None else str(value)
    normalized = raw.strip().lower()
    if not normalized:
        normalized = str(default).strip().lower() or "adapter"
    if normalized in TOOLS_ENABLED_DISABLED_ALIASES:
        return "off"
    return normalized


def tools_enabled_is_disabled(value: Any) -> bool:
    """Return whether an upstream tools mode disables all tool handling."""
    return canonical_tools_enabled(value) == "off"


def model_capability_defaults() -> dict[str, bool]:
    """Return the canonical default capability set (everything off by default)."""
    return {key: False for key in MODEL_CAPABILITY_KEYS}


def _merge_model_capabilities(base: dict[str, bool], override: Any) -> dict[str, bool]:
    """Merge a model-level capability override over a profile-level base.

    Only keys present in the canonical registry are honored; unknown keys are
    dropped so typos cannot silently create new capability columns.
    """
    merged = dict(base)
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if key in MODEL_CAPABILITY_KEYS:
            if not isinstance(value, bool):
                raise ConfigError(
                    f"capability {key!r} must be a JSON boolean"
                )
            merged[key] = value
    return merged


def _normalize_model_entry(model: Any, *, base_capabilities: dict[str, bool]) -> dict[str, Any]:
    """Normalize a single model entry inside a profile's ``models`` list."""
    if not isinstance(model, dict):
        return {}
    name = str(model.get("name") or model.get("model") or "").strip()
    if not name:
        return {}
    # ``capability_overrides`` is the persisted, sparse source of truth.  The
    # materialized ``capabilities`` map is retained for compatibility and for
    # the Config Center matrix, but must not turn inherited profile values into
    # sticky model overrides on the next normalization pass.
    if "capability_overrides" in model:
        raw_overrides = model.get("capability_overrides")
        if not isinstance(raw_overrides, dict):
            raise ConfigError("model capability_overrides must be an object")
    else:
        # Backward compatibility: old model entries only had ``capabilities``;
        # those values were user-authored and are therefore explicit.
        raw_overrides = model.get("capabilities")
    overrides = _merge_model_capabilities({}, raw_overrides)
    caps = _merge_model_capabilities(base_capabilities, overrides)
    entry: dict[str, Any] = {
        "name": name,
        "capability_overrides": overrides,
        "capabilities": caps,
    }
    for extra in (
        "description",
        "max_input_tokens",
        "max_output_tokens",
        "enabled",
        "load_balance_enabled",
        "healthy",
        "health_status",
    ):
        if model.get(extra) is not None:
            entry[extra] = model[extra]
    return entry


def _normalize_request_path(path: str) -> str:
    """Map compatibility URL prefixes to the gateway's canonical API paths."""
    if path == ANTHROPIC_COMPAT_PREFIX:
        return "/"
    if path.startswith(f"{ANTHROPIC_COMPAT_PREFIX}/"):
        suffix = path[len(ANTHROPIC_COMPAT_PREFIX):]
        return suffix or "/"
    return path


def _supported_public_paths() -> set[str]:
    canonical = SUPPORTED_PATHS | DIRECT_TOOL_CALL_PATHS | MODEL_LIST_PATHS | TOKEN_COUNT_PATHS | WEB2API_PATHS
    anthropic_aliases = {f"{ANTHROPIC_COMPAT_PREFIX}{path}" for path in canonical if path.startswith("/v1/")}
    return canonical | anthropic_aliases


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _secret_fingerprint(secret: str) -> str:
    """Return a non-reversible display identifier for a credential."""
    return _hash_secret(secret)[:8]


_PASSWORD_SCHEME = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 600_000
_PASSWORD_MAX_VERIFY_ITERATIONS = 2_000_000


def _hash_password(password: str, *, iterations: int = _PASSWORD_ITERATIONS, salt: bytes | None = None) -> str:
    if iterations < 1 or iterations > _PASSWORD_MAX_VERIFY_ITERATIONS:
        raise ValueError("password hash iteration count is outside the supported range")
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join((
        _PASSWORD_SCHEME,
        str(iterations),
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    ))


def _verify_password(password: str, encoded: Any) -> bool:
    value = str(encoded or "")
    if value.startswith(f"{_PASSWORD_SCHEME}$"):
        try:
            _scheme, raw_iterations, raw_salt, raw_digest = value.split("$", 3)
            iterations = int(raw_iterations)
            if iterations < 1 or iterations > _PASSWORD_MAX_VERIFY_ITERATIONS:
                return False
            salt = base64.urlsafe_b64decode(raw_salt.encode("ascii"))
            expected = base64.urlsafe_b64decode(raw_digest.encode("ascii"))
            if not salt or len(expected) != hashlib.sha256().digest_size:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError, UnicodeError):
            return False
    return hmac.compare_digest(_hash_secret(password), value)


def _password_hash_needs_upgrade(encoded: Any) -> bool:
    value = str(encoded or "")
    if not value.startswith(f"{_PASSWORD_SCHEME}$"):
        return True
    try:
        return int(value.split("$", 3)[1]) < _PASSWORD_ITERATIONS
    except (IndexError, ValueError):
        return True


@lru_cache(maxsize=8)
def _configured_admin_password_hash(configured_hash: str, configured_password: str) -> str:
    """Derive the environment/default Admin verifier once per process."""
    if configured_hash:
        return configured_hash
    return _hash_password(configured_password or "admin")


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


def _env_upstream_protocol(default: str = "openai_chat") -> str:
    return str(os.environ.get("GATEWAY_UPSTREAM_PROTOCOL") or os.environ.get("UPSTREAM_PROTOCOL") or default)


def _env_model_name(default: str = "") -> str:
    """Return the configured upstream model name (used to seed the default models list)."""
    return str(os.environ.get("UPSTREAM_MODEL") or default).strip()


def _admin_form_numeric_raw(
    form: dict[str, str],
    keys: tuple[str, ...],
    existing_value: Any,
    default: int | float,
) -> str:
    if not keys:
        raise ValueError("admin numeric field requires at least one key")
    for key in keys:
        value = form.get(key)
        if value is not None:
            stripped = str(value).strip()
            if stripped:
                return stripped
    if existing_value not in (None, ""):
        return str(existing_value).strip()
    return str(default)


def _admin_form_int(
    form: dict[str, str],
    keys: tuple[str, ...],
    existing_value: Any,
    default: int,
) -> int:
    raw = _admin_form_numeric_raw(form, keys, existing_value, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid numeric field: {keys[0]}") from None


def _admin_form_float(
    form: dict[str, str],
    keys: tuple[str, ...],
    existing_value: Any,
    default: float,
) -> float:
    raw = _admin_form_numeric_raw(form, keys, existing_value, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"invalid numeric field: {keys[0]}") from None


def _default_config() -> Json:
    from .gateway_logging import _sqlite_path

    # Admin password: use env var if set, otherwise use known default for dev/testing
    configured_admin_hash = os.environ.get("GATEWAY_ADMIN_PASSWORD_HASH", "")
    configured_admin_password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "")
    admin_password_hash = _configured_admin_password_hash(
        configured_admin_hash,
        configured_admin_password,
    )
    admin_must_change = not os.environ.get("GATEWAY_ADMIN_PASSWORD") and not os.environ.get("GATEWAY_ADMIN_PASSWORD_HASH")

    # Downstream keys
    downstream_keys: list = []
    downstream_key_env = os.environ.get("GATEWAY_DOWNSTREAM_KEY") or os.environ.get("DOWNSTREAM_API_KEY", "")
    if downstream_key_env:
        downstream_keys.append({
            "name": "default",
            "key_hash": _hash_secret(downstream_key_env),
            "prefix": _secret_fingerprint(downstream_key_env),
            "enabled": True,
            "protocols": ["models", "chat_completions", "responses", "messages", "direct_tools"],
            "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        })

    cfg = {
        "admin": {
            "username": "admin",
            "password_hash": admin_password_hash,
            "must_change_password": admin_must_change,
        },
        "upstream": {
            "base_url": os.environ.get("UPSTREAM_BASE_URL", ""),
            "api_key": os.environ.get("UPSTREAM_API_KEY", ""),
            "model": os.environ.get("UPSTREAM_MODEL", ""),
            "protocol": _env_upstream_protocol(),
            "tools_enabled": canonical_tools_enabled(
                os.environ.get("GATEWAY_TOOLS_ENABLED", "adapter")
            ),
            "native_tools_verified": False,
            "use_for_coding": True,
            "timeout_seconds": _env_float("UPSTREAM_TIMEOUT", 60.0),
            "retry_max_attempts": _env_int("UPSTREAM_RETRY_MAX_ATTEMPTS", 3),
            "retry_initial_delay_seconds": _env_float("UPSTREAM_RETRY_INITIAL_DELAY", 0.5),
            "retry_max_delay_seconds": _env_float("UPSTREAM_RETRY_MAX_DELAY", 4.0),
            "retry_max_elapsed_seconds": _env_float("UPSTREAM_RETRY_MAX_ELAPSED", 90.0),
            "max_input_tokens": _env_int("UPSTREAM_MAX_INPUT_TOKENS", 1048576),
            "max_output_tokens": _env_int("UPSTREAM_MAX_OUTPUT_TOKENS", 131072),
            "max_response_bytes": _env_int("UPSTREAM_MAX_RESPONSE_BYTES", 32 * 1024 * 1024),
            "max_stderr_bytes": _env_int("UPSTREAM_MAX_STDERR_BYTES", 256 * 1024),
            "max_stream_event_bytes": _env_int("UPSTREAM_MAX_STREAM_EVENT_BYTES", 1024 * 1024),
            "max_stream_events": _env_int("UPSTREAM_MAX_STREAM_EVENTS", 100000),
            "max_concurrency": _env_int("UPSTREAM_MAX_CONCURRENCY", 32),
            "paths": {
                "models": os.environ.get("UPSTREAM_MODELS_PATH", "/v1/models"),
                "chat_completions": os.environ.get("UPSTREAM_CHAT_COMPLETIONS_PATH", "/v1/chat/completions"),
                "responses": os.environ.get("UPSTREAM_RESPONSES_PATH", "/v1/responses"),
                "messages": os.environ.get("UPSTREAM_MESSAGES_PATH", "/v1/messages"),
            },
            "capabilities": {
                "supports_streaming": _env_bool("UPSTREAM_SUPPORTS_STREAMING", True),
                "supports_tools": _env_bool("UPSTREAM_SUPPORTS_TOOLS", False),
                "supports_function_calls": _env_bool("UPSTREAM_SUPPORTS_FUNCTION_CALLS", False),
                "supports_parallel_tool_calls": _env_bool("UPSTREAM_SUPPORTS_PARALLEL_TOOL_CALLS", False),
                "supports_vision": _env_bool("UPSTREAM_SUPPORTS_VISION", False),
                "supports_network": _env_bool("UPSTREAM_SUPPORTS_NETWORK", False),
                "supports_web_search": _env_bool("UPSTREAM_SUPPORTS_WEB_SEARCH", False),
                "supports_json_schema": _env_bool("UPSTREAM_SUPPORTS_JSON_SCHEMA", False),
                "supports_image_recognition": _env_bool(
                    "UPSTREAM_SUPPORTS_IMAGE_RECOGNITION", False
                ),
                "supports_music_recognition": _env_bool(
                    "UPSTREAM_SUPPORTS_MUSIC_RECOGNITION", False
                ),
                "supports_video_recognition": _env_bool(
                    "UPSTREAM_SUPPORTS_VIDEO_RECOGNITION", False
                ),
                "supports_audio_recognition": _env_bool(
                    "UPSTREAM_SUPPORTS_AUDIO_RECOGNITION", False
                ),
                "supports_speech": _env_bool("UPSTREAM_SUPPORTS_SPEECH", False),
            },
            "models": [
                {
                    "name": _env_model_name(),
                    "capabilities": {
                        "supports_streaming": _env_bool("UPSTREAM_SUPPORTS_STREAMING", True),
                        "supports_tools": _env_bool("UPSTREAM_SUPPORTS_TOOLS", False),
                        "supports_function_calls": _env_bool("UPSTREAM_SUPPORTS_FUNCTION_CALLS", False),
                        "supports_parallel_tool_calls": _env_bool("UPSTREAM_SUPPORTS_PARALLEL_TOOL_CALLS", False),
                        "supports_vision": _env_bool("UPSTREAM_SUPPORTS_VISION", False),
                        "supports_network": _env_bool("UPSTREAM_SUPPORTS_NETWORK", False),
                        "supports_web_search": _env_bool("UPSTREAM_SUPPORTS_WEB_SEARCH", False),
                        "supports_json_schema": _env_bool("UPSTREAM_SUPPORTS_JSON_SCHEMA", False),
                        "supports_image_recognition": _env_bool(
                            "UPSTREAM_SUPPORTS_IMAGE_RECOGNITION", False
                        ),
                        "supports_music_recognition": _env_bool(
                            "UPSTREAM_SUPPORTS_MUSIC_RECOGNITION", False
                        ),
                        "supports_video_recognition": _env_bool(
                            "UPSTREAM_SUPPORTS_VIDEO_RECOGNITION", False
                        ),
                        "supports_audio_recognition": _env_bool(
                            "UPSTREAM_SUPPORTS_AUDIO_RECOGNITION", False
                        ),
                        "supports_speech": _env_bool(
                            "UPSTREAM_SUPPORTS_SPEECH", False
                        ),
                    },
                }
            ],
        },
        "gateway": {
            "tool_mode": os.environ.get("GATEWAY_TOOL_MODE", "orchestrate"),
            "max_tool_rounds": int(os.environ.get("GATEWAY_MAX_TOOL_ROUNDS") or 10),
            # Runtime workspace is request-scoped.  Keep the default empty so
            # missing client workspace metadata never falls back to the Gateway
            # service checkout.  An explicit env/config root is still accepted
            # as a testing/admin fallback by the tool runtime.
            "workspace_root": os.environ.get("GATEWAY_WORKSPACE_ROOT", ""),
            "allow_write_tools": os.environ.get("GATEWAY_ALLOW_WRITE_TOOLS", "0") in {"1", "true", "yes"},
            "allow_shell_tools": os.environ.get("GATEWAY_ALLOW_SHELL_TOOLS", "0") in {"1", "true", "yes"},
            "tool_cache_persist_local_results": _env_bool("GATEWAY_TOOL_CACHE_PERSIST_LOCAL_RESULTS", False),
            "request_logging": True,
            "logging_backend": os.environ.get("GATEWAY_LOGGING_BACKEND", "sqlite"),
            "max_log_payload_chars": _env_int("GATEWAY_MAX_LOG_PAYLOAD_CHARS", 200000),
            "sqlite_log_path": str(_sqlite_path()),
            "max_concurrent_requests": _env_int("GATEWAY_MAX_CONCURRENT_REQUESTS", 32),
            "concurrency_backend": os.environ.get("GATEWAY_CONCURRENCY_BACKEND", "sqlite"),
            "concurrency_db_path": os.environ.get(
                "GATEWAY_CONCURRENCY_DB_PATH",
                str(pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR", ".gateway_runtime")) / "admission.sqlite3"),
            ),
            "concurrency_fallback_backend": os.environ.get("GATEWAY_CONCURRENCY_FALLBACK_BACKEND", "none"),
            "concurrency_busy_timeout_ms": _env_int("GATEWAY_CONCURRENCY_BUSY_TIMEOUT_MS", 1000),
            "concurrency_lease_ttl_seconds": _env_float("GATEWAY_CONCURRENCY_LEASE_TTL_SECONDS", 120.0),
            "concurrency_heartbeat_seconds": _env_float("GATEWAY_CONCURRENCY_HEARTBEAT_SECONDS", 30.0),
            "max_request_body_bytes": _env_int("GATEWAY_MAX_REQUEST_BODY_BYTES", 64 * 1024 * 1024),
            "cors_enabled": _env_bool("GATEWAY_CORS_ENABLED", False),
            "cors_allowed_origins": [
                item.strip()
                for item in os.environ.get("GATEWAY_CORS_ALLOWED_ORIGINS", "").split(",")
                if item.strip()
            ],
            "concurrency_queue_timeout_seconds": _env_float("GATEWAY_CONCURRENCY_QUEUE_TIMEOUT", 5.0),
            "rate_limit_enabled": _env_bool("GATEWAY_RATE_LIMIT_ENABLED", True),
            "rate_limit_rpm": _env_int("GATEWAY_RATE_LIMIT_RPM", 120),
            "rate_limit_backend": os.environ.get("GATEWAY_RATE_LIMIT_BACKEND", "sqlite"),
            "rate_limit_db_path": os.environ.get(
                "GATEWAY_RATE_LIMIT_DB_PATH",
                str(pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR", ".gateway_runtime")) / "rate_limits.sqlite3"),
            ),
            "rate_limit_fallback_backend": os.environ.get("GATEWAY_RATE_LIMIT_FALLBACK_BACKEND", "memory"),
            "rate_limit_busy_timeout_ms": _env_int("GATEWAY_RATE_LIMIT_BUSY_TIMEOUT_MS", 1000),
            "rate_limit_state_ttl_seconds": _env_int("GATEWAY_RATE_LIMIT_STATE_TTL_SECONDS", 3600),
            "tool_execution_timeout_seconds": _env_float("GATEWAY_TOOL_EXECUTION_TIMEOUT", 60.0),
            "record_unsupported_tools": _env_bool("GATEWAY_RECORD_UNSUPPORTED_TOOLS", True),
            "text_tool_call_fallback_enabled": _env_bool("GATEWAY_TEXT_TOOL_CALL_FALLBACK", True),
            "text_tool_adapter_compact_token_limit": _env_int("GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT", 48000),
            "execute_user_side_tools_in_gateway": _env_bool("GATEWAY_EXECUTE_USER_SIDE_TOOLS", False),
            "intent_detection_enabled": _env_bool("GATEWAY_INTENT_DETECTION_ENABLED", True),
            "local_planner_enabled": _env_bool("GATEWAY_LOCAL_PLANNER_ENABLED", True),
            "local_planner_max_files": _env_int("GATEWAY_LOCAL_PLANNER_MAX_FILES", 24),
            "local_planner_max_bytes_per_file": _env_int("GATEWAY_LOCAL_PLANNER_MAX_BYTES_PER_FILE", 24000),
            "public_base_url": os.environ.get("GATEWAY_PUBLIC_BASE_URL", "http://127.0.0.1:8885"),
            "client_snippet_api_key": os.environ.get("DOWNSTREAM_API_KEY") or os.environ.get("GATEWAY_DOWNSTREAM_KEY", ""),
            "downstream_model_alias": os.environ.get("GATEWAY_DOWNSTREAM_MODEL_ALIAS", os.environ.get("UPSTREAM_MODEL", "")),
            "review_model_alias": os.environ.get("GATEWAY_REVIEW_MODEL_ALIAS", os.environ.get("GATEWAY_DOWNSTREAM_MODEL_ALIAS", os.environ.get("UPSTREAM_MODEL", ""))),
            "codex_reasoning_effort": os.environ.get("GATEWAY_CODEX_REASONING_EFFORT", "xhigh"),
            "client_context_window": _env_int("GATEWAY_CLIENT_CONTEXT_WINDOW", 1048576),
            "client_auto_compact_token_limit": _env_int("GATEWAY_CLIENT_AUTO_COMPACT_TOKEN_LIMIT", 943718),
            "client_output_token_limit": _env_int("GATEWAY_CLIENT_OUTPUT_TOKEN_LIMIT", 131072),
            "agent_planner_strict_every_turn": _env_bool("GATEWAY_AGENT_PLANNER_STRICT_EVERY_TURN", True),
        },
        "context": {
            "enabled": os.environ.get("GATEWAY_CONTEXT_ENABLED", "1").lower() in {"1", "true", "yes"},
            "max_input_tokens": int(os.environ.get("GATEWAY_CONTEXT_MAX_INPUT_TOKENS") or "1048576"),
            "keep_recent_messages": int(os.environ.get("GATEWAY_CONTEXT_KEEP_RECENT_MESSAGES") or "12"),
            "summary_max_chars": int(os.environ.get("GATEWAY_CONTEXT_SUMMARY_MAX_CHARS") or "6000"),
            "fanout_enabled": os.environ.get("GATEWAY_CONTEXT_FANOUT_ENABLED", "1").lower() in {"1", "true", "yes"},
            "fanout_chunk_tokens": int(os.environ.get("GATEWAY_CONTEXT_FANOUT_CHUNK_TOKENS") or "120000"),
            "fanout_max_chunks": int(os.environ.get("GATEWAY_CONTEXT_FANOUT_MAX_CHUNKS") or "0"),
            "fanout_max_workers": int(os.environ.get("GATEWAY_CONTEXT_FANOUT_MAX_WORKERS") or "4"),
            "quality_review_enabled": _env_bool("GATEWAY_CONTEXT_QUALITY_REVIEW", True),
            "memory_enabled": _env_bool("GATEWAY_MEMORY_ENABLED", True),
            "memory_max_items": _env_int("GATEWAY_MEMORY_MAX_ITEMS", 200),
            "memory_recall_limit": _env_int("GATEWAY_MEMORY_RECALL_LIMIT", 8),
            "memory_inject_max_chars": _env_int("GATEWAY_MEMORY_INJECT_MAX_CHARS", 4000),
            "memory_summary_max_chars": _env_int("GATEWAY_MEMORY_SUMMARY_MAX_CHARS", 900),
            "memory_rollup_every_turns": _env_int("GATEWAY_MEMORY_ROLLUP_EVERY_TURNS", 8),
            "memory_rollup_max_chars": _env_int("GATEWAY_MEMORY_ROLLUP_MAX_CHARS", 4000),
            "route_to_long_context": os.environ.get("GATEWAY_CONTEXT_ROUTE_LONG", "1").lower() in {"1", "true", "yes"},
            "long_context_upstream": {
                "base_url": os.environ.get("GATEWAY_LONG_CONTEXT_BASE_URL", ""),
                "api_key": os.environ.get("GATEWAY_LONG_CONTEXT_API_KEY", ""),
                "model": os.environ.get("GATEWAY_LONG_CONTEXT_MODEL", ""),
                "protocol": os.environ.get("GATEWAY_LONG_CONTEXT_PROTOCOL", ""),
            },
        },
        "persistence": {
            "enabled": _env_bool("GATEWAY_PERSISTENCE_ENABLED", True),
            "db_path": os.environ.get(
                "GATEWAY_PERSISTENCE_DB_PATH",
                str(pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR", ".gateway_runtime")) / "gateway.db"),
            ),
        },
        "assistants": {
            "db_path": os.environ.get(
                "GATEWAY_ASSISTANTS_DB_PATH",
                str(pathlib.Path(os.environ.get("GATEWAY_RUNTIME_DIR", ".gateway_runtime")) / "assistants.sqlite3"),
            ),
            "retention_days": _env_int("GATEWAY_ASSISTANTS_RETENTION_DAYS", 30),
            "max_rows": _env_int("GATEWAY_ASSISTANTS_MAX_ROWS", 50000),
        },
        "downstream_keys": downstream_keys,
        "mcp": {
            "servers": [],
            "marketplace_enabled": True,
            "max_header_bytes": _env_int("GATEWAY_MCP_MAX_HEADER_BYTES", 64 * 1024),
            "max_message_bytes": _env_int("GATEWAY_MCP_MAX_MESSAGE_BYTES", 16 * 1024 * 1024),
            "max_stderr_bytes": _env_int("GATEWAY_MCP_MAX_STDERR_BYTES", 256 * 1024),
        },
        "http_actions": {
            "enabled": True,
            "actions": [],
        },
        "cache": {
            "enabled": _env_bool("GATEWAY_CACHE_ENABLED", True),
            "max_entries": _env_int("GATEWAY_CACHE_MAX_ENTRIES", 1000),
            "similarity_threshold": _env_float("GATEWAY_CACHE_SIMILARITY_THRESHOLD", 0.92),
            "ttl_seconds": _env_int("GATEWAY_CACHE_TTL_SECONDS", 3600),
            "embedding_url": os.environ.get("GATEWAY_EMBEDDING_URL", ""),
            "embedding_model": os.environ.get("GATEWAY_EMBEDDING_MODEL", "default"),
            "embedding_api_key": os.environ.get("GATEWAY_EMBEDDING_API_KEY", ""),
        },
        "intelligence": {
            "enabled": _env_bool("GATEWAY_INTELLIGENCE_ENABLED", True),
            "reflection_enabled": _env_bool("GATEWAY_INTELLIGENCE_REFLECTION", True),
            "decomposition_enabled": _env_bool("GATEWAY_INTELLIGENCE_DECOMPOSITION", True),
            "quality_assessment_enabled": _env_bool("GATEWAY_INTELLIGENCE_QUALITY", True),
            "max_reflection_tokens": _env_int("GATEWAY_INTELLIGENCE_MAX_REFLECTION_TOKENS", 500),
            "max_decomposition_parts": _env_int("GATEWAY_INTELLIGENCE_MAX_DECOMPOSITION_PARTS", 5),
            "quality_threshold": _env_float("GATEWAY_INTELLIGENCE_QUALITY_THRESHOLD", 0.6),
            "use_llm": _env_bool("GATEWAY_INTELLIGENCE_USE_LLM", False),
            "provider": os.environ.get("GATEWAY_INTELLIGENCE_PROVIDER", "gateway_upstream"),
            "model": os.environ.get("GATEWAY_INTELLIGENCE_MODEL", ""),
            "llm_timeout": _env_float("GATEWAY_INTELLIGENCE_LLM_TIMEOUT", 15.0),
            "max_input_chars": _env_int("GATEWAY_INTELLIGENCE_MAX_INPUT_CHARS", 20000),
            "temperature": _env_float("GATEWAY_INTELLIGENCE_TEMPERATURE", 0.0),
            "strict_mode": _env_bool("GATEWAY_INTELLIGENCE_STRICT_MODE", False),
        },
        "concurrency": {
            "enabled": _env_bool("GATEWAY_CONCURRENCY_ENABLED", True),
            "max_connections": _env_int("GATEWAY_CONCURRENCY_MAX_CONNECTIONS", 100),
            "max_connections_per_host": _env_int("GATEWAY_CONCURRENCY_MAX_CONNECTIONS_PER_HOST", 10),
            "retry_count": _env_int("GATEWAY_CONCURRENCY_RETRY_COUNT", 2),
            "retry_delay": _env_float("GATEWAY_CONCURRENCY_RETRY_DELAY", 1.0),
            "load_balance_strategy": os.environ.get("GATEWAY_CONCURRENCY_STRATEGY", "round_robin"),
            "multi_upstream_enabled": _env_bool("GATEWAY_MULTI_UPSTREAM_ENABLED", False),
            "multi_upstream_max_attempts": _env_int("GATEWAY_MULTI_UPSTREAM_MAX_ATTEMPTS", 0),
            "multi_upstream_failure_threshold": _env_int("GATEWAY_MULTI_UPSTREAM_FAILURE_THRESHOLD", 3),
            "multi_upstream_recovery_seconds": _env_float("GATEWAY_MULTI_UPSTREAM_RECOVERY_SECONDS", 30.0),
        },
        "stats": {
            "enabled": _env_bool("GATEWAY_STATS_ENABLED", True),
            "track_requests": True,
            "track_tools": True,
            "track_cache": True,
            "track_quality": True,
            "retention_days": _env_int("GATEWAY_STATS_RETENTION_DAYS", 30),
        },
        "maintenance": {
            "enabled": _env_bool("GATEWAY_MAINTENANCE_ENABLED", True),
            "interval_seconds": _env_int("GATEWAY_MAINTENANCE_INTERVAL_SECONDS", 300),
            "dry_run": _env_bool("GATEWAY_MAINTENANCE_DRY_RUN", False),
            "batch_size": _env_int("GATEWAY_MAINTENANCE_BATCH_SIZE", 1000),
            "max_batches_per_run": _env_int("GATEWAY_MAINTENANCE_MAX_BATCHES", 4),
            "incremental_vacuum_pages": _env_int("GATEWAY_MAINTENANCE_VACUUM_PAGES", 256),
            "request_log_retention_days": _env_int("GATEWAY_REQUEST_LOG_RETENTION_DAYS", 30),
            "request_log_max_rows": _env_int("GATEWAY_REQUEST_LOG_MAX_ROWS", 100000),
            "tool_failure_retention_days": _env_int("GATEWAY_TOOL_FAILURE_RETENTION_DAYS", 90),
            "tool_failure_max_rows": _env_int("GATEWAY_TOOL_FAILURE_MAX_ROWS", 50000),
            "memory_retention_days": _env_int("GATEWAY_MEMORY_RETENTION_DAYS", 90),
            "memory_max_rows": _env_int("GATEWAY_MEMORY_MAX_ROWS", 50000),
            "planner_session_retention_days": _env_int("GATEWAY_PLANNER_RETENTION_DAYS", 30),
            "planner_session_max_rows": _env_int("GATEWAY_PLANNER_MAX_ROWS", 20000),
            "planner_event_max_rows": _env_int("GATEWAY_PLANNER_EVENT_MAX_ROWS", 100000),
            "runtime_cleanup_enabled": _env_bool("GATEWAY_RUNTIME_CLEANUP_ENABLED", False),
            "runtime_cleanup_dry_run": _env_bool("GATEWAY_RUNTIME_CLEANUP_DRY_RUN", True),
            "runtime_retention_days": _env_int("GATEWAY_RUNTIME_RETENTION_DAYS", 7),
            "runtime_max_entries_per_run": _env_int("GATEWAY_RUNTIME_MAX_ENTRIES_PER_RUN", 20),
            "runtime_max_entry_bytes": _env_int("GATEWAY_RUNTIME_MAX_ENTRY_BYTES", 268435456),
        },
        "web2api": {
            "enabled": _env_bool("GATEWAY_WEB2API_ENABLED", True),
            "max_concurrent": _env_int("GATEWAY_WEB2API_MAX_CONCURRENT", 5),
            "cache_ttl_seconds": _env_int("GATEWAY_WEB2API_CACHE_TTL", 300),
            "request_timeout": _env_int("GATEWAY_WEB2API_TIMEOUT", 30),
            "max_content_bytes": _env_int("GATEWAY_WEB2API_MAX_CONTENT_BYTES", 5 * 1024 * 1024),
            "max_cache_entries": _env_int("GATEWAY_WEB2API_MAX_CACHE_ENTRIES", 256),
            "user_agent": os.environ.get("GATEWAY_WEB2API_USER_AGENT", "Gateway-Web2API/1.0"),
            "allow_private_network": _env_bool("GATEWAY_WEB2API_ALLOW_PRIVATE_NETWORK", False),
            "allow_regex": _env_bool("GATEWAY_WEB2API_ALLOW_REGEX", False),
            "allow_raw_html": _env_bool("GATEWAY_WEB2API_ALLOW_RAW_HTML", False),
        },
    }
    _ensure_client_snippet_downstream_key(cfg)
    return cfg


def _config_file_revision_unlocked() -> str:
    if not CONFIG_PATH.exists():
        return "missing"
    try:
        return hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
    except OSError as exc:
        raise ConfigError(f"unable to read gateway config revision: {CONFIG_PATH}", detail=str(exc)) from exc


def config_file_revision() -> str:
    with CONFIG_LOCK:
        return _config_file_revision_unlocked()


def _load_config_unlocked() -> Json:
    if not CONFIG_PATH.exists():
        cfg = _default_config()
        _ensure_client_snippet_downstream_key(cfg)
        cfg = _sync_active_upstream(cfg)
        _save_config_unlocked(cfg, expected_revision="missing")
        return cfg
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("config root must be object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ConfigError(
            f"invalid gateway config: {CONFIG_PATH}",
            detail=f"{exc.__class__.__name__}: {exc}",
        ) from exc

    try:
        from . import gateway_encryption as ge
        loaded = ge.decrypt_config(loaded, in_place=True)
    except Exception as exc:
        raise ConfigError(
            f"unable to decrypt gateway config: {CONFIG_PATH}",
            detail=f"{exc.__class__.__name__}: {exc}",
        ) from exc

    cfg = _default_config()
    loaded_upstream = loaded.get("upstream")
    _normalize_admin_credentials(loaded)
    _deep_update(cfg, loaded)
    # Preserve migration provenance.  The env-seeded default contains a
    # ``models`` list, but a legacy persisted profile may only declare its own
    # singular ``model``.  Do not let the environment list masquerade as an
    # explicitly persisted per-model list for the active profile.
    if isinstance(loaded_upstream, dict) and "models" not in loaded_upstream:
        active = cfg.get("upstream")
        if isinstance(active, dict):
            active.pop("models", None)
    _apply_security_environment_overrides(cfg)
    _ensure_client_snippet_downstream_key(cfg)
    return _sync_active_upstream(cfg)


def load_config() -> Json:
    with CONFIG_LOCK:
        return _load_config_unlocked()


def load_config_with_revision() -> tuple[Json, str]:
    with CONFIG_LOCK:
        cfg = _load_config_unlocked()
        return cfg, _config_file_revision_unlocked()


def _atomic_write_config(payload: str) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{CONFIG_PATH.name}.", suffix=".tmp", dir=str(CONFIG_PATH.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, CONFIG_PATH)
        try:
            directory_fd = os.open(str(CONFIG_PATH.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _save_config_unlocked(config: Json, *, expected_revision: str | None = None) -> str:
    current_revision = _config_file_revision_unlocked()
    if expected_revision is not None and current_revision != expected_revision:
        raise ConfigConflictError(
            "gateway config changed while it was being edited",
            detail={"expected_revision": expected_revision, "current_revision": current_revision},
        )

    normalized = copy.deepcopy(config)
    _normalize_admin_credentials(normalized)
    _ensure_client_snippet_downstream_key(normalized)
    if "gateway" in normalized and isinstance(normalized["gateway"], dict):
        root = str(normalized["gateway"].get("workspace_root") or "").strip()
        if not root:
            normalized["gateway"].pop("workspace_root", None)
        elif not os.environ.get("GATEWAY_WORKSPACE_ROOT"):
            try:
                if pathlib.Path(root).expanduser().resolve(strict=False) == pathlib.Path.cwd().resolve(strict=False):
                    normalized["gateway"].pop("workspace_root", None)
            except Exception:
                pass

    try:
        from . import gateway_encryption as ge
        normalized = ge.encrypt_config(normalized, in_place=True)
    except Exception as exc:
        if os.environ.get("GATEWAY_ALLOW_PLAINTEXT_CONFIG", "").lower() not in {"1", "true", "yes", "on"}:
            raise ConfigError(
                "refusing to save gateway config because encryption failed",
                detail=f"{exc.__class__.__name__}: {exc}",
            ) from exc

    payload = json.dumps(_sync_active_upstream(normalized), ensure_ascii=False, indent=2)
    _atomic_write_config(payload)
    return _config_file_revision_unlocked()


def save_config(config: Json, *, expected_revision: str | None = None) -> str:
    with CONFIG_LOCK:
        return _save_config_unlocked(config, expected_revision=expected_revision)


def _deep_update(base: Json, updates: Json) -> Json:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def _normalize_admin_credentials(config: Json) -> Json:
    """Convert legacy/template ``admin.password`` to ``password_hash``.

    Public templates historically used a plain ``admin.password`` field for
    readability, while runtime authentication only checks ``password_hash``.
    Normalizing at load/save keeps old templates usable without persisting the
    plain password back to disk.
    """
    admin = config.get("admin")
    if not isinstance(admin, dict):
        return config
    plain_password = admin.pop("password", None)
    if plain_password and not admin.get("password_hash"):
        password = str(plain_password)
        admin["password_hash"] = _hash_password(password)
        admin.setdefault("must_change_password", password == "admin")
    return config


def _downstream_key_id(item: Json) -> str:
    existing = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(item.get("id") or "")).strip("-._")
    if existing:
        return existing
    seed = str(item.get("key_hash") or item.get("name") or item.get("prefix") or uuid.uuid4().hex)
    return f"client_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


def _normalize_downstream_key_ids(config: Json) -> Json:
    downstream_keys = config.get("downstream_keys")
    if not isinstance(downstream_keys, list):
        return config
    seen: set[str] = set()
    for index, item in enumerate(downstream_keys):
        if not isinstance(item, dict):
            continue
        client_id = _downstream_key_id(item)
        if client_id in seen:
            client_id = f"{client_id}_{index}"
        item["id"] = client_id
        seen.add(client_id)
    return config


def _apply_security_environment_overrides(config: Json) -> Json:
    """Make explicitly supplied runtime credentials authoritative over disk state.

    Production deployments commonly retain an older encrypted configuration
    volume while rotating credentials through the process environment. An
    empty/default persisted credential must not silently override a required
    runtime secret or make an otherwise secure external deployment fail to
    authenticate.
    """
    configured_hash = os.environ.get("GATEWAY_ADMIN_PASSWORD_HASH", "")
    configured_password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "")
    if configured_hash or configured_password:
        admin = config.setdefault("admin", {})
        admin["password_hash"] = _configured_admin_password_hash(configured_hash, configured_password)
        admin["must_change_password"] = False

    downstream_key = os.environ.get("GATEWAY_DOWNSTREAM_KEY") or os.environ.get("DOWNSTREAM_API_KEY", "")
    if downstream_key:
        gateway = config.setdefault("gateway", {})
        gateway["client_snippet_api_key"] = downstream_key
        downstream_keys = config.get("downstream_keys")
        if not isinstance(downstream_keys, list):
            downstream_keys = []
            config["downstream_keys"] = downstream_keys
        key_hash = _hash_secret(downstream_key)
        for item in downstream_keys:
            if isinstance(item, dict) and item.get("key_hash") == key_hash:
                item["enabled"] = True
                item.setdefault("protocols", ["models", "chat_completions", "responses", "messages", "direct_tools"])
                break
        else:
            downstream_keys.append({
                "name": "environment",
                "key_hash": key_hash,
                "prefix": _secret_fingerprint(downstream_key),
                "enabled": True,
                "protocols": ["models", "chat_completions", "responses", "messages", "direct_tools"],
                "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            })
        _normalize_downstream_key_ids(config)
    return config


_SENSITIVE_KEY_NAMES = {
    "apikey",
    "authorization",
    "auth",
    "authtoken",
    "bearer",
    "cookie",
    "setcookie",
    "password",
    "passwordhash",
    "keyhash",
    "clientsecret",
    "privatekey",
}


_NON_SENSITIVE_KEY_NAMES = {
    "mustchangepassword",
}


def _sensitive_key_name(key: object) -> bool:
    """Return True for payload/config field names that should never be logged."""
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if normalized in _NON_SENSITIVE_KEY_NAMES:
        return False
    if normalized in _SENSITIVE_KEY_NAMES:
        return True
    return normalized.endswith(("apikey", "token", "secret", "password", "keyhash", "cookie"))


def _redact_sensitive_values(value: Any) -> Any:
    """Recursively redact common credential-bearing fields while preserving shape."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if _sensitive_key_name(key):
                redacted[key] = "***"
            elif normalized_key == "base64" or (
                normalized_key == "data"
                and (
                    str(value.get("type") or "").lower() == "base64"
                    or "media_type" in value
                    or "format" in value
                )
            ):
                redacted[key] = "[REDACTED_MEDIA]"
            else:
                redacted[key] = _redact_sensitive_values(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]
    if isinstance(value, str):
        match = re.match(r"^(data:[^;,]+;base64,)", value, flags=re.IGNORECASE)
        if match:
            return f"{match.group(1)}[REDACTED_MEDIA]"
    return value


def _ensure_client_snippet_downstream_key(config: Json) -> Json:
    """Make copied client snippets authenticate without extra manual key setup."""
    _normalize_downstream_key_ids(config)
    gateway_cfg = config.get("gateway")
    if not isinstance(gateway_cfg, dict):
        return config
    snippet_key = str(gateway_cfg.get("client_snippet_api_key") or "").strip()
    if not snippet_key:
        return config
    downstream_keys = config.get("downstream_keys")
    if not isinstance(downstream_keys, list):
        downstream_keys = []
        config["downstream_keys"] = downstream_keys
    key_hash = _hash_secret(snippet_key)
    protocols = ["models", "chat_completions", "responses", "messages", "direct_tools"]
    for item in downstream_keys:
        if not isinstance(item, dict):
            continue
        if item.get("key_hash") == key_hash:
            item["id"] = _downstream_key_id(item)
            item["prefix"] = _secret_fingerprint(snippet_key)
            item["enabled"] = True
            item["protocols"] = protocols
            return config
        if item.get("name") == "client-snippet":
            item.update({
                "key_hash": key_hash,
                "prefix": _secret_fingerprint(snippet_key),
                "enabled": True,
                "protocols": protocols,
            })
            item["updated_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            item["id"] = _downstream_key_id(item)
            return config
    new_item = {
        "name": "client-snippet",
        "key_hash": key_hash,
        "prefix": _secret_fingerprint(snippet_key),
        "enabled": True,
        "protocols": protocols,
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    }
    new_item["id"] = _downstream_key_id(new_item)
    downstream_keys.append(new_item)
    return config


def _upstream_profile_id(profile: Json) -> str:
    raw = str(profile.get("id") or profile.get("name") or profile.get("base_url") or uuid.uuid4().hex[:8])
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-._")
    return cleaned or f"upstream-{uuid.uuid4().hex[:8]}"


def _normalize_upstream_profile(profile: Json, *, fallback_name: str = "default") -> Json:
    raw_profile = profile if isinstance(profile, dict) else {}
    raw_models = raw_profile.get("models") if "models" in raw_profile else None
    default_upstream = _default_config()["upstream"]
    merged = copy.deepcopy(default_upstream)
    _deep_update(merged, raw_profile)
    merged["name"] = str(merged.get("name") or fallback_name or "default")
    merged["id"] = _upstream_profile_id(merged)
    merged["tools_enabled"] = canonical_tools_enabled(merged.get("tools_enabled"))
    merged.setdefault("paths", {})
    merged.setdefault("capabilities", {})
    merged["capabilities"] = _merge_model_capabilities(
        model_capability_defaults(), merged.get("capabilities")
    )
    merged["models"] = _normalize_profile_models(
        raw_models,
        base_capabilities=merged["capabilities"],
        default_model=merged.get("model"),
    )
    model_names = [str(item.get("name") or "") for item in merged["models"]]
    if model_names and str(merged.get("model") or "").strip() not in model_names:
        merged["model"] = model_names[0]
    return merged


def _normalize_profile_models(
    models: Any, *, base_capabilities: dict[str, bool], default_model: str | None
) -> list[dict[str, Any]]:
    """Normalize a profile's per-model capability list.

    Backward compatible: an empty/absent ``models`` list falls back to the
    profile's single ``model`` field, inheriting the profile-level capabilities.
    """
    normalized: list[dict[str, Any]] = []
    if isinstance(models, list):
        for item in models:
            entry = _normalize_model_entry(item, base_capabilities=base_capabilities)
            if entry:
                normalized.append(entry)
    if not normalized and default_model:
        normalized.append(
            {
                "name": str(default_model).strip(),
                "capability_overrides": {},
                "capabilities": dict(base_capabilities),
            }
        )
    if not normalized:
        normalized.append(
            {
                "name": str(merged_default_model()),
                "capability_overrides": {},
                "capabilities": dict(base_capabilities),
            }
        )
    return normalized


def merged_default_model() -> str:
    """Return the default model name from the default upstream config."""
    return str(_default_config()["upstream"].get("model") or "")


def _normalized_profiles(config: Json) -> list[Json]:
    """Return all upstream profiles with normalized per-model capability lists."""
    raw = config.get("upstream_profiles")
    if not isinstance(raw, list) or not raw:
        active = config.get("upstream")
        raw = [active] if isinstance(active, dict) else []
    return [_normalize_upstream_profile(item) for item in raw if isinstance(item, dict)]


def flatten_profile_models(config: Json) -> list[dict[str, Any]]:
    """Flatten every profile's models into a single list of capability rows.

    Each row: ``{profile_id, profile_name, base_url, model, capabilities, is_default}``.
    ``is_default`` marks the model that backs the active profile's top-level
    ``model`` field (used by the legacy single-model code path).
    """
    active_id = str(config.get("active_upstream_id") or "")
    rows: list[dict[str, Any]] = []
    for profile in _normalized_profiles(config):
        pid = str(profile.get("id") or "")
        models = profile.get("models") or []
        default_model = str(profile.get("model") or "").strip()
        for index, model in enumerate(models):
            if not isinstance(model, dict):
                continue
            rows.append(
                {
                    "profile_id": pid,
                    "profile_name": str(profile.get("name") or pid),
                    "base_url": str(profile.get("base_url") or ""),
                    "protocol": str(profile.get("protocol") or ""),
                    "model": str(model.get("name") or ""),
                    "capabilities": dict(model.get("capabilities") or {}),
                    "capability_overrides": dict(model.get("capability_overrides") or {}),
                    "is_default": bool(default_model and model.get("name") == default_model),
                    "is_active_profile": pid == active_id,
                }
            )
    return rows


def find_model_row(config: Json, profile_id: str, model_name: str) -> dict[str, Any] | None:
    """Locate a single model capability row, or None if it does not exist."""
    for row in flatten_profile_models(config):
        if row["profile_id"] == profile_id and row["model"] == model_name:
            return row
    return None


def model_capability_snapshot(config: Json) -> dict[str, Any]:
    """Return a web-UI-friendly snapshot of the full model capability matrix."""
    rows = flatten_profile_models(config)
    columns = [
        {"key": key, "label": label, "kind": kind}
        for key, label, kind in MODEL_CAPABILITY_SPEC
    ]
    return {
        "profiles": sorted({row["profile_id"] for row in rows}),
        "columns": columns,
        "rows": rows,
        "capability_counts": {
            key: sum(1 for row in rows if row["capabilities"].get(key))
            for key in MODEL_CAPABILITY_KEYS
        },
    }


def _normalized_config_for_mutation(config: Json) -> Json:
    """Return a copy of ``config`` with ``upstream_profiles`` populated.

    The Admin UI edits the single ``upstream`` object, so a freshly loaded
    config may only have ``upstream`` and no ``upstream_profiles`` list.  The
    per-model mutation helpers operate on the normalized profile list, so they
    need this bridge.  The returned copy is safe to mutate in place.
    """
    normalized = copy.deepcopy(config)
    profiles = normalized.get("upstream_profiles")
    if not isinstance(profiles, list) or not profiles:
        active = normalized.get("upstream")
        if isinstance(active, dict) and active:
            profiles = [copy.deepcopy(active)]
            normalized["upstream_profiles"] = profiles
    normalized["upstream_profiles"] = [
        _normalize_upstream_profile(item) for item in profiles if isinstance(item, dict)
    ]
    return normalized


def set_model_capability(
    config: Json, profile_id: str, model_name: str, capability: str, value: bool
) -> tuple[Json, str]:
    """Set a single capability flag on a specific model.

    Returns ``(updated_config, changed_field_path)``.  Raises ConfigError if the
    model does not exist or the capability is not in the canonical registry.
    """
    if capability not in MODEL_CAPABILITY_KEYS:
        raise ConfigError(f"unknown capability: {capability!r}")
    if not isinstance(value, bool):
        raise ConfigError("model capability value must be a JSON boolean")
    config = _normalized_config_for_mutation(config)
    profiles = config.get("upstream_profiles")
    if not isinstance(profiles, list):
        profiles = []
        config["upstream_profiles"] = profiles
    found = False
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if str(profile.get("id") or "") != profile_id:
            continue
        models = profile.setdefault("models", [])
        if not isinstance(models, list):
            models = []
            profile["models"] = models
        for model in models:
            if not isinstance(model, dict):
                continue
            if str(model.get("name") or "") == model_name:
                overrides = model.setdefault("capability_overrides", {})
                if not isinstance(overrides, dict):
                    raise ConfigError("model capability_overrides must be an object")
                overrides[capability] = value
                base = _merge_model_capabilities(
                    model_capability_defaults(), profile.get("capabilities")
                )
                model["capabilities"] = _merge_model_capabilities(base, overrides)
                found = True
                break
        if found:
            break
    if not found:
        raise ConfigError(f"model not found: {profile_id}/{model_name}")
    active_id = str(config.get("active_upstream_id") or config.get("active_upstream") or "")
    if profile_id == active_id:
        active_profile = next(
            (
                item
                for item in profiles
                if isinstance(item, dict) and str(item.get("id") or "") == profile_id
            ),
            None,
        )
        if active_profile is not None:
            config["upstream"] = copy.deepcopy(active_profile)
    field = f"upstream_profiles[?id=={profile_id}].models[?name=={model_name}].capability_overrides.{capability}"
    return config, field


def add_model_to_profile(
    config: Json, profile_id: str, model_name: str, capabilities: dict[str, bool] | None = None
) -> Json:
    """Add a new model to a profile, inheriting the profile's capability defaults."""
    config = _normalized_config_for_mutation(config)
    profiles = config.get("upstream_profiles")
    if not isinstance(profiles, list):
        profiles = []
        config["upstream_profiles"] = profiles
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if str(profile.get("id") or "") != profile_id:
            continue
        models = profile.setdefault("models", [])
        if not isinstance(models, list):
            models = []
            profile["models"] = models
        for model in models:
            if isinstance(model, dict) and str(model.get("name") or "") == model_name:
                return config  # already exists
        base = _merge_model_capabilities(model_capability_defaults(), profile.get("capabilities"))
        overrides = _merge_model_capabilities({}, capabilities)
        models.append(
            {
                "name": model_name,
                "capability_overrides": overrides,
                "capabilities": _merge_model_capabilities(base, overrides),
            }
        )
        return config
    raise ConfigError(f"profile not found: {profile_id}")


def remove_model_from_profile(config: Json, profile_id: str, model_name: str) -> Json:
    """Remove a model from a profile.  Refuses to remove the last model."""
    config = _normalized_config_for_mutation(config)
    profiles = config.get("upstream_profiles")
    if not isinstance(profiles, list):
        raise ConfigError("no upstream_profiles to modify")
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if str(profile.get("id") or "") != profile_id:
            continue
        models = profile.get("models")
        if not isinstance(models, list) or not models:
            raise ConfigError(f"profile {profile_id} has no models")
        if len(models) == 1:
            raise ConfigError(f"cannot remove the last model from profile {profile_id}")
        profile["models"] = [m for m in models if not (isinstance(m, dict) and str(m.get("name") or "") == model_name)]
        if not profile["models"]:
            raise ConfigError(f"model not found: {profile_id}/{model_name}")
        return config
    raise ConfigError(f"profile not found: {profile_id}")


def _sync_active_upstream(config: Json) -> Json:
    """Reconcile ``upstream`` with ``upstream_profiles`` and write back.

    The Admin UI edits the single ``upstream`` object, but older sessions also
    keep a per-profile list (``upstream_profiles``).  When the two diverge the
    stale profile wins, silently reverting the user's last edit (e.g. switching
    the active protocol or capability flags).  To avoid that, the active
    profile is rebuilt from the user-edited top-level ``upstream`` with env
    defaults only filling in fields the user did not set.
    """
    main = config.get("upstream") if isinstance(config.get("upstream"), dict) else {}
    env_default = _default_config()["upstream"]
    profiles = config.get("upstream_profiles")
    if not isinstance(profiles, list) or not profiles:
        base = copy.deepcopy(main)
        base.setdefault("name", "default")
        base.setdefault("id", "default")
        profiles = [base]
    main_id = str(main.get("id") or "") if isinstance(main, dict) else ""

    normalized: list[Json] = []
    seen: set[str] = set()
    for index, item in enumerate(profiles):
        prof = _normalize_upstream_profile(item, fallback_name=f"profile-{index}")
        is_active = bool(main_id) and str(prof.get("id") or "") == main_id
        if is_active and isinstance(main, dict) and main:
            # Rebuild from the user-edited top-level upstream; only fall back to
            # env defaults for keys the user never set.
            rebuilt = copy.deepcopy(env_default)
            _deep_update(rebuilt, copy.deepcopy(main))
            if "models" not in main:
                rebuilt.pop("models", None)
            # Preserve the profile's id/name if main did not override them
            rebuilt.setdefault("id", prof.get("id") or "default")
            rebuilt["name"] = str(
                main.get("name") or prof.get("name") or rebuilt.get("id") or "default"
            )
            prof = _normalize_upstream_profile(
                rebuilt,
                fallback_name=str(prof.get("name") or f"profile-{index}"),
            )
        pid = prof["id"]
        if pid in seen:
            pid = f"{pid}-{index}"
            prof["id"] = pid
        seen.add(pid)
        normalized.append(prof)
    config["upstream_profiles"] = normalized
    active_id = str(config.get("active_upstream_id") or "") or main_id
    if not active_id or active_id not in seen:
        active_id = normalized[0]["id"]
    config["active_upstream_id"] = active_id
    config["active_upstream"] = active_id
    active = next((p for p in normalized if p["id"] == active_id), normalized[0])
    config["upstream"] = copy.deepcopy(active)
    return config


def _models_from_admin_form(
    form: dict[str, str], existing: Json | None, profile: Json
) -> list[dict[str, Any]]:
    """Build the per-model list for a profile from the admin form.

    When the form carries a non-empty ``models_json`` field it is parsed as a
    JSON array of ``{"name": str, "capabilities": {key: bool}, ...}`` entries
    and becomes the profile's ``models`` list.  Otherwise the legacy single
    ``model`` field is used (one model inheriting the profile-level
    capabilities), preserving backward compatibility.
    """
    raw = str(form.get("models_json") or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"models_json 不是合法 JSON: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError("models_json 必须是 JSON 数组")
        models: list[dict[str, Any]] = []
        for index, item in enumerate(parsed):
            if not isinstance(item, dict):
                raise ValueError(f"models_json[{index}] 不是对象")
            name = str(item.get("name") or item.get("model") or "").strip()
            if not name:
                raise ValueError(f"models_json[{index}] 缺少 name")
            entry: dict[str, Any] = {"name": name}
            caps = item.get("capabilities")
            if caps is not None and not isinstance(caps, dict):
                raise ValueError(f"models_json[{index}].capabilities 必须是对象")
            if isinstance(caps, dict):
                overrides: dict[str, bool] = {}
                for key, value in caps.items():
                    if key not in MODEL_CAPABILITY_KEYS:
                        continue
                    if not isinstance(value, bool):
                        raise ValueError(
                            f"models_json[{index}].capabilities.{key} 必须是 JSON 布尔值"
                        )
                    overrides[key] = value
                entry["capability_overrides"] = overrides
            for extra in ("description", "max_input_tokens", "max_output_tokens"):
                if item.get(extra) is not None:
                    entry[extra] = item[extra]
            models.append(entry)
        if models:
            return models
    # Legacy single-model fallback.
    default_model = str(profile.get("model") or "").strip()
    if not default_model and isinstance(existing, dict):
        default_model = str(existing.get("model") or "").strip()
    if not default_model:
        default_model = merged_default_model()
    return [
        {
            "name": default_model,
            "capability_overrides": {},
            "capabilities": dict(profile.get("capabilities") or {}),
        }
    ]


def _profile_from_admin_form(form: dict[str, str], existing: Json | None = None) -> Json:
    profile = copy.deepcopy(existing) if existing else {}
    profile["id"] = form.get("profile_id", form.get("id", "")).strip() or profile.get("id") or "default"
    profile["name"] = form.get("profile_name", form.get("name", "")).strip() or profile.get("name") or profile["id"]
    profile["base_url"] = form.get("base_url", "").strip() or profile.get("base_url", "")
    api_key_value = form.get("api_key")
    if api_key_value is not None and api_key_value.strip():
        profile["api_key"] = api_key_value.strip()
    else:
        profile["api_key"] = profile.get("api_key", "")
    profile["model"] = form.get("model", "").strip() or profile.get("model", "")
    profile["protocol"] = form.get("protocol", "openai_chat").strip()
    profile["tools_enabled"] = canonical_tools_enabled(
        form.get("tools_enabled", form.get("tool_mode", "adapter"))
    )
    profile["timeout_seconds"] = _admin_form_float(
        form,
        ("upstream_timeout_seconds", "timeout_seconds", "timeout"),
        profile.get("timeout_seconds"),
        60.0,
    )
    profile["max_input_tokens"] = _admin_form_int(
        form,
        ("upstream_max_input_tokens", "max_input_tokens"),
        profile.get("max_input_tokens"),
        128000,
    )
    profile["max_output_tokens"] = _admin_form_int(
        form,
        ("upstream_max_output_tokens", "max_output_tokens"),
        profile.get("max_output_tokens"),
        8192,
    )
    profile["max_concurrency"] = _admin_form_int(
        form,
        ("upstream_max_concurrency", "max_concurrency"),
        profile.get("max_concurrency"),
        32,
    )
    existing_paths = profile.get("paths") if isinstance(profile.get("paths"), dict) else {}
    profile["paths"] = {
        "models": form.get("path_models", "").strip() or existing_paths.get("models") or "/v1/models",
        "chat_completions": form.get("path_chat_completions", "").strip() or existing_paths.get("chat_completions") or "/v1/chat/completions",
        "responses": form.get("path_responses", "").strip() or existing_paths.get("responses") or "/v1/responses",
        "messages": form.get("path_messages", "").strip() or existing_paths.get("messages") or "/v1/messages",
    }
    profile["native_tools_verified"] = form.get("native_tools_verified", "") != "" if "native_tools_verified" in form else bool(profile.get("native_tools_verified", False))
    profile["use_for_coding"] = form.get("use_for_coding", "") != "" if "use_for_coding" in form else bool(profile.get("use_for_coding", True))
    cap_form_keys = {
        "supports_streaming": "cap_supports_streaming",
        "supports_tools": "cap_supports_tools",
        "supports_function_calls": "cap_supports_function_calls",
        "supports_parallel_tool_calls": "cap_supports_parallel_tool_calls",
        "supports_vision": "cap_supports_vision",
        "supports_network": "cap_supports_network",
        "supports_web_search": "cap_supports_web_search",
        "supports_json_schema": "cap_supports_json_schema",
        "supports_image_recognition": "cap_supports_image_recognition",
        "supports_music_recognition": "cap_supports_music_recognition",
        "supports_video_recognition": "cap_supports_video_recognition",
        "supports_audio_recognition": "cap_supports_audio_recognition",
        "supports_speech": "cap_supports_speech",
    }
    existing_caps = profile.get("capabilities") if isinstance(profile.get("capabilities"), dict) else {}
    explicit_capability_form = form.get("capabilities_form", "") != "" or any(form_key in form for form_key in cap_form_keys.values())
    if explicit_capability_form or not existing_caps:
        profile["capabilities"] = {
            cap_key: form.get(form_key, "") != ""
            for cap_key, form_key in cap_form_keys.items()
        }
    else:
        profile["capabilities"] = dict(existing_caps)
    # Per-model capability list.  When the admin form supplies a ``models_json``
    # field it wins; otherwise the legacy single ``model`` field is used and the
    # profile-level capabilities are inherited (backward compatible).
    profile["models"] = _models_from_admin_form(form, existing, profile)
    return _normalize_upstream_profile(profile, fallback_name=profile["name"])


def _redacted_config(config: Json) -> Json:
    redacted = _redact_sensitive_values(copy.deepcopy(config))
    if isinstance(redacted.get("admin"), dict):
        redacted["admin"].pop("password_hash", None)
    return redacted


def _config_env(name: str, fallback: str = "") -> str:
    return os.environ.get(name) or fallback


def _configured_max_tool_rounds(gateway_cfg: Json | None = None) -> int:
    """Resolve tool loop budget with env override first, then persisted gateway config."""
    if gateway_cfg is None:
        gateway_cfg = _gateway_config()
    try:
        return int(os.environ.get("GATEWAY_MAX_TOOL_ROUNDS") or gateway_cfg.get("max_tool_rounds") or 10)
    except (TypeError, ValueError):
        return 10


_TEXT_TOOL_ADAPTER_COMPACT_FLOOR = 8000
_TEXT_TOOL_ADAPTER_COMPACT_RATIO = 0.45
_TEXT_TOOL_ADAPTER_COMPACT_DEFAULT_CAP = 48000


def _resolved_text_tool_adapter_compact_token_limit(
    gateway_cfg: Json | None = None,
    upstream_cfg: Json | None = None,
) -> int:
    """Dynamic compact threshold for weak-upstream text tool adapter.

    Formula: max(floor, min(upstream.max_input_tokens * ratio, config_cap))
    - floor: 8000 — minimum usable budget for basic harness
    - ratio: 0.45 — leave room for tool instructions + response + user intent
    - config_cap: gateway.text_tool_adapter_compact_token_limit (default 48000)
    """
    if gateway_cfg is None:
        gateway_cfg = _gateway_config()
    if upstream_cfg is None:
        upstream_cfg = _upstream_config()
    try:
        raw = os.environ.get("GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT")
        if raw is None:
            raw = gateway_cfg.get("text_tool_adapter_compact_token_limit")
        if raw is None:
            raw = _TEXT_TOOL_ADAPTER_COMPACT_DEFAULT_CAP
        config_cap = int(raw)
    except (TypeError, ValueError):
        config_cap = _TEXT_TOOL_ADAPTER_COMPACT_DEFAULT_CAP
    if config_cap <= 0:
        return 0  # disabled
    try:
        upstream_limit = int(upstream_cfg.get("max_input_tokens") or 128000)
    except (TypeError, ValueError):
        upstream_limit = 128000
    dynamic = int(upstream_limit * _TEXT_TOOL_ADAPTER_COMPACT_RATIO)
    return max(_TEXT_TOOL_ADAPTER_COMPACT_FLOOR, min(dynamic, config_cap))


def _upstream_config() -> Json:
    return load_config().get("upstream", {})


def _gateway_config() -> Json:
    return load_config().get("gateway", {})


def _headroom_max_input_tokens() -> int:
    """Token budget the headroom compressor should target for the upstream body.

    Defaults to the upstream profile's ``max_input_tokens`` if set, else
    ``GATEWAY_HEADROOM_MAX_INPUT_TOKENS`` env override, else a safe 16K
    (≈ 48 KB raw, well under typical weak-relay caps around 50-100 KB).
    """
    try:
        env_value = int(os.environ.get("GATEWAY_HEADROOM_MAX_INPUT_TOKENS") or 0)
    except (TypeError, ValueError):
        env_value = 0
    if env_value > 0:
        return env_value
    cfg = _upstream_config()
    try:
        upstream_value = int(cfg.get("max_input_tokens") or 0)
    except (TypeError, ValueError):
        upstream_value = 0
    if upstream_value > 0:
        return upstream_value
    return 16000


def _configured_upstream_path(path: str) -> str:
    cfg = _upstream_config()
    paths = cfg.get("paths", {})
    if "/chat/completions" in path:
        return paths.get("chat_completions", "/v1/chat/completions")
    if "/responses" in path:
        return paths.get("responses", "/v1/responses")
    if "/messages" in path:
        return paths.get("messages", "/v1/messages")
    if "/models" in path:
        return paths.get("models", "/v1/models")
    return path


def _configured_upstream_path_by_key(key: str, default: str) -> str:
    cfg = _upstream_config()
    return cfg.get("paths", {}).get(key, default)


def _upstream_protocol() -> str:
    return _env_upstream_protocol(str(_upstream_config().get("protocol") or "openai_chat"))


def _use_openai_chat_upstream(path: str) -> bool:
    protocol = _upstream_protocol()
    if protocol == "anthropic_messages":
        return False
    return "/chat/completions" in path


def _force_upstream_stream_aggregate() -> bool:
    return os.environ.get("GATEWAY_UPSTREAM_STREAM_AGGREGATE", "").lower() in {"1", "true", "yes"}
