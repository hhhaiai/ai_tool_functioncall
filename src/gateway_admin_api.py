"""Canonical authenticated Admin configuration and runtime-status API."""
from __future__ import annotations

import copy
import dataclasses
import math
import re
import urllib.parse
from typing import Any, Callable, Mapping, Protocol

from .gateway_errors import BadRequestError
from .gateway_http_security import normalize_origin

Json = dict[str, Any]

_GET_PATHS = {
    "/ui/config",
    "/api/config",
    "/api/config/schema",
    "/api/stats/dashboard",
    "/api/cache/stats",
}
_POST_PATHS = {"/api/config", "/api/config/update", "/api/cache/clear"}
_SECRET_PLACEHOLDERS = {"", "***"}
_MAX_UPDATE_FIELDS = 200
_MAX_UPDATE_DEPTH = 8


class AdminAPIHandler(Protocol):
    path: str
    headers: Mapping[str, str]


AdminCheck = Callable[[AdminAPIHandler], bool]
JsonResponse = Callable[[AdminAPIHandler, int, Json], None]
TextResponse = Callable[[AdminAPIHandler, int, str, str], None]
ReadJson = Callable[[AdminAPIHandler], Json]


def _schema_fields() -> dict[str, Json]:
    from .gateway_web_config import get_config_schema

    result: dict[str, Json] = {}
    for tab in get_config_schema():
        for field in tab.get("fields") or []:
            if isinstance(field, dict) and field.get("name"):
                result[str(field["name"])] = field
    return result


def _flatten_update(
    value: Json,
    *,
    prefix: str = "",
    depth: int = 0,
) -> list[tuple[str, Any]]:
    if depth > _MAX_UPDATE_DEPTH:
        raise BadRequestError("configuration update is nested too deeply")
    fields: list[tuple[str, Any]] = []
    for raw_key, item in value.items():
        key = str(raw_key).strip()
        if not key or "." in key:
            raise BadRequestError("configuration keys must be non-empty nested object keys")
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            fields.extend(_flatten_update(item, prefix=path, depth=depth + 1))
        else:
            fields.append((path, item))
        if len(fields) > _MAX_UPDATE_FIELDS:
            raise BadRequestError("configuration update contains too many fields")
    return fields


def _parse_boolean(path: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise BadRequestError(f"invalid boolean field: {path}")


def _parse_number(path: str, value: Any, field: Json) -> int | float:
    if isinstance(value, bool):
        raise BadRequestError(f"invalid numeric field: {path}")
    default = field.get("default")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BadRequestError(f"invalid numeric field: {path}") from exc
    if not math.isfinite(parsed):
        raise BadRequestError(f"invalid numeric field: {path}")
    minimum = field.get("min")
    maximum = field.get("max")
    if minimum is not None and parsed < float(minimum):
        raise BadRequestError(f"numeric field below minimum: {path}")
    if maximum is not None and parsed > float(maximum):
        raise BadRequestError(f"numeric field above maximum: {path}")
    if isinstance(default, int) and not isinstance(default, bool):
        if not parsed.is_integer():
            raise BadRequestError(f"numeric field must be an integer: {path}")
        return int(parsed)
    return parsed


def _parse_field(path: str, value: Any, field: Json) -> Any:
    field_type = str(field.get("type") or "text")
    if field_type == "boolean":
        return _parse_boolean(path, value)
    if field_type == "number":
        return _parse_number(path, value, field)
    if path == "gateway.cors_allowed_origins" and isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if not isinstance(value, str):
        raise BadRequestError(f"invalid text field: {path}")
    parsed = value.strip()
    if len(parsed) > 16_384:
        raise BadRequestError(f"text field too long: {path}")
    if field_type == "select":
        allowed = {
            str(option.get("value"))
            for option in field.get("options") or []
            if isinstance(option, dict)
        }
        if parsed not in allowed:
            raise BadRequestError(f"invalid select field: {path}")
    return parsed


def _set_nested(config: Json, path: str, value: Any) -> None:
    parts = path.split(".")
    current = config
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _validate_http_url(value: Any, *, field: str, allow_empty: bool = False) -> None:
    text = str(value or "").strip()
    if not text and allow_empty:
        return
    try:
        parsed = urllib.parse.urlparse(text)
        del parsed.port
    except (TypeError, ValueError) as exc:
        raise BadRequestError(f"invalid URL field: {field}") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise BadRequestError(f"invalid URL field: {field}")


def _validate_candidate(candidate: Json, changed: set[str]) -> None:
    raw_upstream = candidate.get("upstream")
    upstream: Json = raw_upstream if isinstance(raw_upstream, dict) else {}
    if any(path.startswith("upstream.") for path in changed):
        _validate_http_url(upstream.get("base_url"), field="upstream.base_url")
        try:
            if int(upstream.get("max_output_tokens") or 0) > int(upstream.get("max_input_tokens") or 0):
                raise BadRequestError("upstream.max_output_tokens must not exceed upstream.max_input_tokens")
        except (TypeError, ValueError, OverflowError) as exc:
            raise BadRequestError("invalid upstream token limits") from exc

    raw_cache = candidate.get("cache")
    cache: Json = raw_cache if isinstance(raw_cache, dict) else {}
    if "cache.embedding_url" in changed:
        _validate_http_url(cache.get("embedding_url"), field="cache.embedding_url", allow_empty=True)

    raw_context = candidate.get("context")
    context: Json = raw_context if isinstance(raw_context, dict) else {}
    if any(path.startswith("context.") for path in changed):
        if int(context.get("fanout_chunk_tokens") or 0) > int(context.get("max_input_tokens") or 0):
            raise BadRequestError("context.fanout_chunk_tokens must not exceed context.max_input_tokens")

    raw_gateway = candidate.get("gateway")
    gateway: Json = raw_gateway if isinstance(raw_gateway, dict) else {}
    if "gateway.cors_allowed_origins" in changed:
        raw_origins = gateway.get("cors_allowed_origins")
        if isinstance(raw_origins, str):
            origins = [item.strip() for item in raw_origins.split(",") if item.strip()]
        elif isinstance(raw_origins, list):
            origins = [str(item).strip() for item in raw_origins if str(item).strip()]
        else:
            raise BadRequestError("invalid CORS origin list")
        normalized = [normalize_origin(item) for item in origins]
        if any(item is None for item in normalized):
            raise BadRequestError("invalid CORS origin; use exact http(s) origins")
        gateway["cors_allowed_origins"] = list(dict.fromkeys(item for item in normalized if item))
    if "gateway.public_base_url" in changed:
        public_origin = normalize_origin(str(gateway.get("public_base_url") or ""))
        if not public_origin:
            raise BadRequestError("invalid gateway.public_base_url")
        gateway["public_base_url"] = public_origin

    raw_intelligence = candidate.get("intelligence")
    intelligence: Json = raw_intelligence if isinstance(raw_intelligence, dict) else {}
    if "intelligence.provider" in changed:
        provider = str(intelligence.get("provider") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", provider):
            raise BadRequestError("invalid intelligence.provider")


def apply_config_update(payload: Json) -> tuple[Json, str, list[str]]:
    """Validate, atomically save, and reload one partial schema-bound update."""
    from .gateway_config import load_config_with_revision, save_config

    current, current_revision = load_config_with_revision()
    supplied_revision: str | None = None
    if "config" in payload:
        if set(payload) - {"config", "revision"}:
            raise BadRequestError("unknown configuration update envelope field")
        update = payload.get("config")
        if not isinstance(update, dict):
            raise BadRequestError("config must be an object")
        if payload.get("revision") is not None:
            supplied_revision = str(payload.get("revision"))
    else:
        update = {key: value for key, value in payload.items() if key != "revision"}
        if payload.get("revision") is not None:
            supplied_revision = str(payload.get("revision"))
    flattened = _flatten_update(update)
    if not flattened:
        raise BadRequestError("configuration update is empty")

    schema = _schema_fields()
    candidate = copy.deepcopy(current)
    changed: list[str] = []
    for path, raw_value in flattened:
        field = schema.get(path)
        if field is None:
            raise BadRequestError(f"configuration field is not editable: {path}")
        if str(field.get("type")) == "password" and str(raw_value or "") in _SECRET_PLACEHOLDERS:
            continue
        _set_nested(candidate, path, _parse_field(path, raw_value, field))
        changed.append(path)
    if not changed:
        raise BadRequestError("configuration update did not change any editable fields")
    _validate_candidate(candidate, set(changed))
    expected_revision = supplied_revision if supplied_revision is not None else current_revision
    new_revision = save_config(candidate, expected_revision=expected_revision)
    _reload_runtime_after_config_update(set(changed))
    from .gateway_config import _redacted_config

    return _redacted_config(candidate), new_revision, sorted(changed)


def _reload_runtime_after_config_update(changed: set[str]) -> None:
    from .gateway_assistants import reset_assistant_store
    from .gateway_cache import reset_caches
    from .gateway_upstream_pool import reset_upstream_pool
    from .gateway_web2api import reset_engine

    if any(path.startswith(("cache.", "gateway.tool_cache_", "persistence.")) for path in changed):
        reset_caches()
    if any(path.startswith(("upstream.", "concurrency.")) for path in changed):
        reset_upstream_pool()
    if any(path.startswith("web2api.") for path in changed):
        reset_engine()
    if any(path.startswith("assistants.") for path in changed):
        reset_assistant_store()


def cache_status() -> Json:
    from .gateway_cache import get_semantic_cache, get_tool_result_cache
    from .gateway_persistence import get_database_stats

    return {
        "semantic": get_semantic_cache().stats,
        "tools": get_tool_result_cache().stats,
        "persistence": get_database_stats(),
    }


def clear_caches() -> Json:
    from .gateway_cache import get_semantic_cache, get_tool_result_cache
    from .gateway_persistence import clear_persistent_caches

    semantic = get_semantic_cache()
    tools = get_tool_result_cache()
    before = {"semantic": semantic.stats, "tools": tools.stats}
    semantic.clear()
    tools.clear()
    persistent = clear_persistent_caches(strict=True)
    return {
        "cleared": {
            "semantic_memory": int(before["semantic"].get("entries") or 0),
            "tool_memory": int(before["tools"].get("entries") or 0),
            "semantic_persistent": int(persistent.get("semantic") or 0),
            "tool_persistent": int(persistent.get("tools") or 0),
        },
        "cache": cache_status(),
    }


def dashboard_status() -> Json:
    from .gateway_logging import _stats_snapshot
    from .gateway_stats import get_dashboard, get_hourly_trends, get_top_paths, get_top_tools
    from .gateway_upstream_pool import upstream_pool_snapshot

    dashboard = dataclasses.asdict(get_dashboard())
    return {
        "dashboard": dashboard,
        "http": _stats_snapshot(),
        "hourly": get_hourly_trends(24),
        "top_paths": get_top_paths(10),
        "top_tools": get_top_tools(10),
        "upstream_pool": upstream_pool_snapshot(),
    }


def handle_admin_api_get(
    handler: AdminAPIHandler,
    path: str,
    *,
    check_admin: AdminCheck,
    json_response: JsonResponse,
    text_response: TextResponse,
) -> bool:
    if path not in _GET_PATHS:
        return False
    if not check_admin(handler):
        return True
    if path == "/ui/config":
        from .gateway_config import _redacted_config, load_config_with_revision
        from .gateway_web_config import render_web_config_ui

        config, revision = load_config_with_revision()
        text_response(
            handler,
            200,
            render_web_config_ui(_redacted_config(config), revision=revision),
            "text/html; charset=utf-8",
        )
        return True
    if path == "/api/config":
        from .gateway_config import _redacted_config, load_config_with_revision

        config, revision = load_config_with_revision()
        json_response(handler, 200, {"config": _redacted_config(config), "revision": revision})
        return True
    if path == "/api/config/schema":
        from .gateway_web_config import get_config_schema

        json_response(handler, 200, {"tabs": get_config_schema()})
        return True
    if path == "/api/stats/dashboard":
        json_response(handler, 200, dashboard_status())
        return True
    if path == "/api/cache/stats":
        json_response(handler, 200, {"cache": cache_status()})
        return True
    return False


def handle_admin_api_post(
    handler: AdminAPIHandler,
    path: str,
    *,
    check_admin_write: AdminCheck,
    read_json: ReadJson,
    json_response: JsonResponse,
) -> bool:
    if path not in _POST_PATHS:
        return False
    if not check_admin_write(handler):
        return True
    if path == "/api/cache/clear":
        payload = read_json(handler)
        if payload:
            raise BadRequestError("cache clear request body must be empty or an empty object")
        json_response(handler, 200, {"ok": True, **clear_caches()})
        return True
    payload = read_json(handler)
    config, revision, changed = apply_config_update(payload)
    json_response(
        handler,
        200,
        {"ok": True, "config": config, "revision": revision, "changed_fields": changed},
    )
    return True


__all__ = [
    "_GET_PATHS",
    "_POST_PATHS",
    "apply_config_update",
    "cache_status",
    "clear_caches",
    "dashboard_status",
    "handle_admin_api_get",
    "handle_admin_api_post",
]
