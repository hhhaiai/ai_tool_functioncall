#!/usr/bin/env python3
"""Configuration management for the gateway.

Handles loading, saving, and merging configuration from files and environment variables.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import uuid
from typing import Any

from .gateway_errors import ConfigError

Json = dict[str, Any]

CONFIG_PATH = pathlib.Path(os.environ.get("GATEWAY_CONFIG_PATH") or ".gateway_service.json")

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
ANTHROPIC_COMPAT_PREFIX = "/anthropic"


def _normalize_request_path(path: str) -> str:
    """Map compatibility URL prefixes to the gateway's canonical API paths."""
    if path == ANTHROPIC_COMPAT_PREFIX:
        return "/"
    if path.startswith(f"{ANTHROPIC_COMPAT_PREFIX}/"):
        suffix = path[len(ANTHROPIC_COMPAT_PREFIX):]
        return suffix or "/"
    return path


def _supported_public_paths() -> set[str]:
    canonical = SUPPORTED_PATHS | DIRECT_TOOL_CALL_PATHS | MODEL_LIST_PATHS | TOKEN_COUNT_PATHS
    anthropic_aliases = {f"{ANTHROPIC_COMPAT_PREFIX}{path}" for path in canonical if path.startswith("/v1/")}
    return canonical | anthropic_aliases


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


def _env_upstream_protocol(default: str = "openai_chat") -> str:
    return str(os.environ.get("GATEWAY_UPSTREAM_PROTOCOL") or os.environ.get("UPSTREAM_PROTOCOL") or default)


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
    admin_password_hash = os.environ.get("GATEWAY_ADMIN_PASSWORD_HASH", "")
    if not admin_password_hash:
        admin_password = os.environ.get("GATEWAY_ADMIN_PASSWORD", "")
        if admin_password:
            admin_password_hash = _hash_secret(admin_password)
        else:
            admin_password_hash = _hash_secret("admin")
    admin_must_change = not os.environ.get("GATEWAY_ADMIN_PASSWORD") and not os.environ.get("GATEWAY_ADMIN_PASSWORD_HASH")

    # Downstream keys
    downstream_keys: list = []
    downstream_key_env = os.environ.get("GATEWAY_DOWNSTREAM_KEY") or os.environ.get("DOWNSTREAM_API_KEY", "")
    if downstream_key_env:
        downstream_keys.append({
            "name": "default",
            "key_hash": _hash_secret(downstream_key_env),
            "prefix": downstream_key_env[:8],
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
                "supports_web_search": _env_bool("UPSTREAM_SUPPORTS_WEB_SEARCH", False),
                "supports_json_schema": _env_bool("UPSTREAM_SUPPORTS_JSON_SCHEMA", True),
            },
        },
        "gateway": {
            "tool_mode": os.environ.get("GATEWAY_TOOL_MODE", "orchestrate"),
            "max_tool_rounds": int(os.environ.get("GATEWAY_MAX_TOOL_ROUNDS") or 5),
            "workspace_root": os.environ.get("GATEWAY_WORKSPACE_ROOT") or os.getcwd(),
            "allow_write_tools": os.environ.get("GATEWAY_ALLOW_WRITE_TOOLS", "0") in {"1", "true", "yes"},
            "allow_shell_tools": os.environ.get("GATEWAY_ALLOW_SHELL_TOOLS", "0") in {"1", "true", "yes"},
            "request_logging": True,
            "logging_backend": os.environ.get("GATEWAY_LOGGING_BACKEND", "sqlite"),
            "max_log_payload_chars": _env_int("GATEWAY_MAX_LOG_PAYLOAD_CHARS", 200000),
            "sqlite_log_path": str(_sqlite_path()),
            "max_concurrent_requests": _env_int("GATEWAY_MAX_CONCURRENT_REQUESTS", 32),
            "max_request_body_bytes": _env_int("GATEWAY_MAX_REQUEST_BODY_BYTES", 64 * 1024 * 1024),
            "concurrency_queue_timeout_seconds": _env_float("GATEWAY_CONCURRENCY_QUEUE_TIMEOUT", 5.0),
            "tool_execution_timeout_seconds": _env_float("GATEWAY_TOOL_EXECUTION_TIMEOUT", 60.0),
            "record_unsupported_tools": _env_bool("GATEWAY_RECORD_UNSUPPORTED_TOOLS", True),
            "text_tool_call_fallback_enabled": _env_bool("GATEWAY_TEXT_TOOL_CALL_FALLBACK", True),
            "text_tool_adapter_compact_token_limit": _env_int("GATEWAY_TEXT_TOOL_ADAPTER_COMPACT_TOKEN_LIMIT", 12000),
            "local_planner_enabled": _env_bool("GATEWAY_LOCAL_PLANNER_ENABLED", True),
            "local_planner_max_files": _env_int("GATEWAY_LOCAL_PLANNER_MAX_FILES", 24),
            "local_planner_max_bytes_per_file": _env_int("GATEWAY_LOCAL_PLANNER_MAX_BYTES_PER_FILE", 24000),
            "public_base_url": os.environ.get("GATEWAY_PUBLIC_BASE_URL", "http://127.0.0.1:8885"),
            "client_snippet_api_key": os.environ.get("DOWNSTREAM_API_KEY") or os.environ.get("GATEWAY_DOWNSTREAM_KEY", ""),
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
    _ensure_client_snippet_downstream_key(cfg)
    return cfg


def load_config() -> Json:
    if not CONFIG_PATH.exists():
        cfg = _default_config()
        _ensure_client_snippet_downstream_key(cfg)
        cfg = _sync_active_upstream(cfg)
        save_config(cfg)
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
    cfg = _default_config()
    _normalize_admin_credentials(loaded)
    _deep_update(cfg, loaded)
    _ensure_client_snippet_downstream_key(cfg)
    return _sync_active_upstream(cfg)


def save_config(config: Json) -> None:
    normalized = copy.deepcopy(config)
    _normalize_admin_credentials(normalized)
    _ensure_client_snippet_downstream_key(normalized)
    CONFIG_PATH.write_text(json.dumps(_sync_active_upstream(normalized), ensure_ascii=False, indent=2), encoding="utf-8")


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
        admin["password_hash"] = _hash_secret(password)
        admin.setdefault("must_change_password", password == "admin")
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
            if _sensitive_key_name(key):
                redacted[key] = "***"
            else:
                redacted[key] = _redact_sensitive_values(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]
    return value


def _ensure_client_snippet_downstream_key(config: Json) -> Json:
    """Make copied client snippets authenticate without extra manual key setup."""
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
            item["prefix"] = item.get("prefix") or snippet_key[:8]
            item["enabled"] = True
            item["protocols"] = protocols
            return config
        if item.get("name") == "client-snippet":
            item.update({
                "key_hash": key_hash,
                "prefix": snippet_key[:8],
                "enabled": True,
                "protocols": protocols,
            })
            item["updated_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
            return config
    downstream_keys.append({
        "name": "client-snippet",
        "key_hash": key_hash,
        "prefix": snippet_key[:8],
        "enabled": True,
        "protocols": protocols,
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
    })
    return config


def _upstream_profile_id(profile: Json) -> str:
    raw = str(profile.get("id") or profile.get("name") or profile.get("base_url") or uuid.uuid4().hex[:8])
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", raw).strip("-._")
    return cleaned or f"upstream-{uuid.uuid4().hex[:8]}"


def _normalize_upstream_profile(profile: Json, *, fallback_name: str = "default") -> Json:
    default_upstream = _default_config()["upstream"]
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
        prof = _normalize_upstream_profile(item, fallback_name=f"profile-{index}")
        pid = prof["id"]
        if pid in seen:
            pid = f"{pid}-{index}"
            prof["id"] = pid
        seen.add(pid)
        normalized.append(prof)
    config["upstream_profiles"] = normalized
    active_id = str(config.get("active_upstream_id") or "")
    if not active_id or active_id not in seen:
        active_id = normalized[0]["id"]
    config["active_upstream_id"] = active_id
    config["active_upstream"] = active_id
    active = next((p for p in normalized if p["id"] == active_id), normalized[0])
    config["upstream"] = copy.deepcopy(active)
    return config


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
    profile["tools_enabled"] = form.get("tools_enabled", form.get("tool_mode", "auto")).strip()
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
    }
    existing_caps = profile.get("capabilities") if isinstance(profile.get("capabilities"), dict) else {}
    explicit_capability_form = form.get("capabilities_form", "") != "" or any(form_key in form for form_key in cap_form_keys.values())
    if explicit_capability_form or not existing_caps:
        profile["capabilities"] = {
            cap_key: form.get(form_key, "") != ""
            for cap_key, form_key in cap_form_keys.items()
        }
    else:
        profile["capabilities"] = {
            "supports_streaming": bool(existing_caps.get("supports_streaming", True)),
            "supports_tools": bool(existing_caps.get("supports_tools", True)),
            "supports_function_calls": bool(existing_caps.get("supports_function_calls", True)),
            "supports_parallel_tool_calls": bool(existing_caps.get("supports_parallel_tool_calls", True)),
            "supports_vision": bool(existing_caps.get("supports_vision", False)),
            "supports_network": bool(existing_caps.get("supports_network", False)),
            "supports_web_search": bool(existing_caps.get("supports_web_search", False)),
            "supports_json_schema": bool(existing_caps.get("supports_json_schema", True)),
        }
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
        return int(os.environ.get("GATEWAY_MAX_TOOL_ROUNDS") or gateway_cfg.get("max_tool_rounds") or 5)
    except (TypeError, ValueError):
        return 5


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
