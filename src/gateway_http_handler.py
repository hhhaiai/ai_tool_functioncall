#!/usr/bin/env python3
"""HTTP request handler for the gateway.

Handles HTTP routing, request/response processing, and API endpoints.
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import os
import sys
import traceback
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from typing import Any

Json = dict[str, Any]

from .gateway_config import SUPPORTED_PATHS, MODEL_LIST_PATHS, TOKEN_COUNT_PATHS, DIRECT_TOOL_CALL_PATHS, _normalize_request_path, _supported_public_paths
from .gateway_errors import error_payload as _error_payload


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Json) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _safe_json_response(handler: BaseHTTPRequestHandler, status: int, payload: Json) -> None:
    try:
        _json_response(handler, status, payload)
    except Exception:
        try:
            handler.send_response(500)
            handler.end_headers()
        except Exception:
            pass


def _text_response(handler: BaseHTTPRequestHandler, status: int, payload: str, content_type: str = "text/html; charset=utf-8") -> None:
    body = payload.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(body)


def _request_body_limit() -> int:
    from .gateway_config import _gateway_config
    try:
        value = int(_gateway_config().get("max_request_body_bytes") or 64 * 1024 * 1024)
    except (TypeError, ValueError):
        value = 64 * 1024 * 1024
    return max(1, value)


def _request_content_length(handler: BaseHTTPRequestHandler) -> int:
    raw = handler.headers.get("Content-Length", "0")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError) as exc:
        from .gateway_errors import GatewayError
        raise GatewayError("invalid Content-Length header") from exc


def _read_limited_body(handler: BaseHTTPRequestHandler) -> bytes:
    content_length = _request_content_length(handler)
    if content_length == 0:
        return b""
    limit = _request_body_limit()
    if content_length > limit:
        from .gateway_errors import RequestBodyTooLargeError
        raise RequestBodyTooLargeError(f"request body too large: {content_length} bytes exceeds limit {limit}")
    return handler.rfile.read(content_length)


def _read_json(handler: BaseHTTPRequestHandler) -> Json:
    raw = _read_limited_body(handler)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        from .gateway_errors import GatewayError
        raise GatewayError(f"invalid JSON: {e}") from e


def _constant_time_equal(left: object, right: object) -> bool:
    """Compare auth material without leaking timing or failing on unicode input."""
    return hmac.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        if ":" in decoded:
            username, password = decoded.split(":", 1)
            return username, password
    except Exception:
        pass
    return None


def _check_admin(handler: BaseHTTPRequestHandler) -> bool:
    from .gateway_config import load_config, _hash_secret
    try:
        cfg = load_config()
    except Exception as exc:
        _handle_error(handler, handler.path.split("?", 1)[0], exc)
        return False
    admin = cfg.get("admin", {})
    auth = handler.headers.get("Authorization")
    creds = _parse_basic_auth(auth)
    if creds:
        username, password = creds
        if _constant_time_equal(username, admin.get("username", "admin")):
            if _constant_time_equal(_hash_secret(password), admin.get("password_hash") or ""):
                return True
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="Gateway Admin"')
    handler.end_headers()
    return False


def _check_downstream_key(handler: BaseHTTPRequestHandler) -> str | None:
    from .gateway_config import load_config, _hash_secret
    cfg = load_config()
    downstream_keys = cfg.get("downstream_keys") or []
    if not downstream_keys:
        return None
    auth = handler.headers.get("Authorization") or handler.headers.get("authorization")
    api_key = ""
    if auth:
        if auth.startswith("Bearer "):
            api_key = auth[7:]
        elif auth.startswith("Basic "):
            creds = _parse_basic_auth(auth)
            if creds:
                api_key = creds[1]
    if not api_key:
        api_key = handler.headers.get("x-api-key") or handler.headers.get("X-API-Key") or ""
    if not api_key:
        from .gateway_errors import DownstreamAuthError
        raise DownstreamAuthError("missing Authorization or x-api-key header")
    key_hash = _hash_secret(api_key)
    for dk in downstream_keys:
        if isinstance(dk, dict) and dk.get("enabled", True):
            if _constant_time_equal(dk.get("key_hash") or "", key_hash):
                protocols = set(dk.get("protocols") or [])
                if protocols:
                    route = "models"
                    if "/chat/completions" in handler.path:
                        route = "chat_completions"
                    elif "/responses" in handler.path:
                        route = "responses"
                    elif "/messages" in handler.path:
                        route = "messages"
                    elif "/tools/call" in handler.path or "/functions/call" in handler.path:
                        route = "direct_tools"
                    models_compatible = route == "models" and bool(protocols & {"models", "chat_completions", "responses", "messages"})
                    if route not in protocols and not models_compatible:
                        from .gateway_errors import DownstreamAuthError
                        raise DownstreamAuthError(f"API key is not allowed for {route}")
                return dk.get("name", "unknown")
    from .gateway_errors import DownstreamAuthError
    raise DownstreamAuthError("invalid API key")


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    body = _read_limited_body(handler)
    if not body:
        return {}
    raw = body.decode("utf-8")
    content_type = handler.headers.get("Content-Type", "")
    if "application/json" in content_type:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items()}
        except json.JSONDecodeError:
            pass
    import urllib.parse
    parsed = urllib.parse.parse_qs(raw)
    return {k: v[0] if v else "" for k, v in parsed.items()}


def _url_origin(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urllib.parse.urlparse(value.strip())
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc or not parsed.hostname:
        return None
    try:
        scheme = parsed.scheme.lower()
        host = parsed.hostname.lower()
        port = parsed.port
    except ValueError:
        return None
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    if port and port != default_port:
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def _request_origin(handler: BaseHTTPRequestHandler) -> str | None:
    host = (handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "").split(",", 1)[0].strip()
    if not host:
        return None
    proto = (handler.headers.get("X-Forwarded-Proto") or "http").split(",", 1)[0].strip().lower()
    return _url_origin(f"{proto}://{host}")


def _check_admin_origin(handler: BaseHTTPRequestHandler, cfg: Json) -> bool:
    """Reject browser cross-origin admin writes while keeping CLI requests working."""
    source = handler.headers.get("Origin") or handler.headers.get("Referer")
    if not source:
        return True
    source_origin = _url_origin(source)
    if not source_origin:
        _json_response(handler, 403, _error_payload("cross-origin admin request rejected"))
        return False
    allowed = {_request_origin(handler)}
    gateway_cfg = cfg.get("gateway", {}) if isinstance(cfg.get("gateway"), dict) else {}
    allowed.add(_url_origin(str(gateway_cfg.get("public_base_url") or "")))
    if source_origin in {origin for origin in allowed if origin}:
        return True
    _json_response(handler, 403, _error_payload("cross-origin admin request rejected"))
    return False


def _redirect(handler: BaseHTTPRequestHandler, location: str = "/ui") -> None:
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.end_headers()


def _model_ids_from_payload(payload: Any) -> list[str]:
    """Extract model ids from common OpenAI-compatible model-list shapes."""
    models: list[str] = []
    if isinstance(payload, dict):
        candidate_lists: list[Any] = [payload.get("data"), payload.get("models"), payload.get("items")]
    elif isinstance(payload, list):
        candidate_lists = [payload]
    else:
        candidate_lists = []
    for items in candidate_lists:
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
                if model_id:
                    models.append(str(model_id))
            elif isinstance(item, str):
                models.append(item)
    return sorted(dict.fromkeys(models))


def _fetch_upstream_models_for_admin(handler: BaseHTTPRequestHandler, overrides: dict[str, str] | None = None) -> Json:
    """Fetch models from active or form-provided upstream settings for the Admin UI."""
    from .gateway_config import _upstream_config
    from .gateway_errors import GatewayError, UpstreamHTTPError

    upstream_cfg = _upstream_config()
    # GET intentionally ignores query overrides and uses only the saved active
    # profile. Otherwise a cross-site GET in an authenticated browser could make
    # the gateway send the saved upstream Authorization header to an attacker
    # controlled base_url. Temporary discovery overrides are POST-only and pass
    # the Admin Origin/Referer guard before form parsing.
    use_form_overrides = overrides is not None
    overrides = overrides or {}

    def first(name: str) -> str:
        if use_form_overrides and name in overrides and str(overrides.get(name) or "").strip():
            return str(overrides.get(name) or "").strip()
        return ""

    base_url = (first("base_url") or str(upstream_cfg.get("base_url") or "")).rstrip("/")
    # Do not accept temporary API keys from GET query strings: URLs are commonly
    # captured by browser/server logs. The Admin UI submits form overrides via
    # POST body; GET uses the already-saved active profile key.
    api_key = (str(overrides.get("api_key") or "").strip() if use_form_overrides else "") or str(upstream_cfg.get("api_key") or "")
    protocol = first("protocol") or str(upstream_cfg.get("protocol") or "openai_chat")
    paths = upstream_cfg.get("paths") if isinstance(upstream_cfg.get("paths"), dict) else {}
    models_path = first("path_models") or str(paths.get("models") or "/v1/models")
    if not models_path.startswith("/"):
        models_path = "/" + models_path
    if not base_url:
        raise GatewayError("missing upstream base_url")

    headers = {"Accept": "application/json"}
    if api_key:
        if protocol == "anthropic_messages":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {api_key}"
    url = f"{base_url}{models_path}"
    try:
        timeout = float(upstream_cfg.get("timeout_seconds") or 60.0)
    except (TypeError, ValueError):
        timeout = 60.0
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            payload = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UpstreamHTTPError(exc.code, detail) from exc
    models = _model_ids_from_payload(payload)
    return {
        "ok": True,
        "active_model": upstream_cfg.get("model", ""),
        "base_url": base_url,
        "path": models_path,
        "models": models,
        "raw": payload,
    }


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "NativeToolGateway/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_HEAD(self) -> None:
        path = _normalize_request_path(self.path.split("?", 1)[0])
        if path in {"/", "/healthz", "/ui"}:
            self.send_response(200)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:
        path = _normalize_request_path(self.path.split("?", 1)[0])
        if path == "/":
            _text_response(
                self,
                200,
                "Tool Call Gateway is running.\n\nAPI: /v1/messages, /v1/chat/completions, /v1/responses\nHealth: /healthz\nAdmin UI: /ui (basic auth)\nClient config: /client-config\n",
                "text/plain; charset=utf-8",
            )
            return
        if path == "/healthz":
            from .gateway_builtin_tools import BUILTIN_TOOLS
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "mode": os.environ.get("GATEWAY_TOOL_MODE", "orchestrate"),
                    "fake_prompt_tools": False,
                    "supported_paths": sorted(_supported_public_paths()),
                    "builtin_tool_count": len({tool.name for tool in BUILTIN_TOOLS.values()}),
                },
            )
            return
        if path in MODEL_LIST_PATHS:
            try:
                downstream_key = _check_downstream_key(self)
                from .gateway_tool_runtime import _request_slot_scope
                with _request_slot_scope():
                    from .gateway_proxy import NativeProxyClient
                    response = NativeProxyClient().get(path)
                    from .gateway_logging import _record_request_stat, _write_request_log
                    _record_request_stat(path, 200)
                    _write_request_log(path, {}, 200, response, downstream_key)
                    _json_response(self, 200, response)
            except Exception as exc:
                _handle_error(self, path, exc)
            return
        if path == "/ui":
            if not _check_admin(self):
                return
            from .gateway_admin import _render_admin_ui
            _text_response(self, 200, _render_admin_ui())
            return
        if path == "/client-config":
            if not _check_admin(self):
                return
            from .gateway_admin import _render_client_config_ui
            _text_response(self, 200, _render_client_config_ui())
            return
        if path == "/client-config.json":
            if not _check_admin(self):
                return
            from .gateway_admin import _client_config_snippets
            _json_response(self, 200, _client_config_snippets())
            return
        if path == "/admin/config.json":
            if not _check_admin(self):
                return
            from .gateway_config import load_config, _redacted_config
            _json_response(self, 200, {"config": _redacted_config(load_config())})
            return
        if path == "/admin/stats.json":
            if not _check_admin(self):
                return
            from .gateway_logging import _stats_snapshot
            _json_response(self, 200, {"stats": _stats_snapshot()})
            return
        if path == "/admin/requests.json":
            if not _check_admin(self):
                return
            from .gateway_logging import _tail_requests
            _json_response(self, 200, {"requests": _tail_requests(200)})
            return
        if path == "/admin/failures.json":
            if not _check_admin(self):
                return
            from .gateway_logging import _tail_failures
            _json_response(self, 200, {"failures": _tail_failures(200)})
            return
        if path == "/admin/memories.json":
            if not _check_admin(self):
                return
            from .gateway_context import _sqlite_tail_memories
            _json_response(self, 200, {"memories": _sqlite_tail_memories(200)})
            return
        if path == "/admin/tools.json":
            if not _check_admin(self):
                return
            from .gateway_logging import _tool_catalog_snapshot
            _json_response(self, 200, _tool_catalog_snapshot())
            return
        if path == "/admin/mcp-tools.json":
            if not _check_admin(self):
                return
            from .gateway_mcp import _enabled_mcp_servers, _mcp_list_server_tools, _mcp_public_name
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
            import urllib.parse
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            probe = query.get("probe", ["0"])[0] in {"1", "true", "yes"}
            from .gateway_mcp import _mcp_health_snapshot
            _json_response(self, 200, {"servers": _mcp_health_snapshot(probe=probe)})
            return
        if path == "/admin/upstream-models.json":
            if not _check_admin(self):
                return
            try:
                _json_response(self, 200, _fetch_upstream_models_for_admin(self))
            except Exception as exc:
                _handle_error(self, path, exc)
            return
        if path == "/admin/http-actions.json":
            if not _check_admin(self):
                return
            from .gateway_http_actions import _enabled_http_actions
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

    def do_POST(self) -> None:
        try:
            path = _normalize_request_path(self.path.split("?", 1)[0])
            if path == "/admin/upstream-models.json":
                if not _check_admin(self):
                    return
                from .gateway_config import load_config
                cfg = load_config()
                if not _check_admin_origin(self, cfg):
                    return
                form = _read_form(self)
                try:
                    _json_response(self, 200, _fetch_upstream_models_for_admin(self, form))
                except Exception as exc:
                    _handle_error(self, path, exc)
                return
            if path in {"/admin/config", "/admin/upstream-profile", "/admin/client-config", "/admin/password", "/admin/downstream-key", "/admin/mcp", "/admin/mcp-reload", "/admin/http-actions"}:
                if not _check_admin(self):
                    return
                from .gateway_config import load_config, save_config
                cfg = load_config()
                if not _check_admin_origin(self, cfg):
                    return
                form = _read_form(self)
                if path == "/admin/mcp-reload":
                    from .gateway_mcp import _mcp_close_sessions
                    _mcp_close_sessions()
                elif path == "/admin/config":
                    from .gateway_config import _admin_form_float, _admin_form_int, _profile_from_admin_form
                    try:
                        profile = _profile_from_admin_form(form, cfg.get("upstream") if isinstance(cfg.get("upstream"), dict) else None)
                    except ValueError as exc:
                        _json_response(self, 400, _error_payload(str(exc)))
                        return
                    profiles = cfg.get("upstream_profiles") if isinstance(cfg.get("upstream_profiles"), list) else []
                    replaced = False
                    for index, existing_profile in enumerate(profiles):
                        if isinstance(existing_profile, dict) and existing_profile.get("id") == profile["id"]:
                            profiles[index] = profile
                            replaced = True
                            break
                    if not replaced:
                        profiles.append(profile)
                    cfg["active_upstream_id"] = profile["id"]
                    cfg["active_upstream"] = profile["id"]
                    cfg["upstream"] = profile
                    cfg["upstream_profiles"] = profiles
                    gateway_cfg = cfg.setdefault("gateway", {})
                    gateway_cfg["tool_mode"] = form.get("tool_mode", gateway_cfg.get("tool_mode", "orchestrate"))
                    invalid_field = None
                    for field, default, parser in [
                        ("max_tool_rounds", 5, _admin_form_int),
                        ("max_concurrent_requests", 32, _admin_form_int),
                        ("text_tool_adapter_compact_token_limit", 48000, _admin_form_int),
                        ("concurrency_queue_timeout_seconds", 5.0, _admin_form_float),
                        ("tool_execution_timeout_seconds", 60.0, _admin_form_float),
                    ]:
                        if invalid_field is not None:
                            break
                        try:
                            gateway_cfg[field] = parser(form, (field,), gateway_cfg.get(field), default)
                        except ValueError:
                            invalid_field = field
                    if invalid_field is not None:
                        _json_response(self, 400, _error_payload(f"invalid numeric field: {invalid_field}"))
                        return
                    # Note: workspace_root is NOT saved - it's a runtime field determined per-request from client metadata
                    gateway_cfg["allow_write_tools"] = form.get("allow_write_tools", "") != ""
                    gateway_cfg["allow_shell_tools"] = form.get("allow_shell_tools", "") != ""
                    gateway_cfg["request_logging"] = form.get("request_logging", "") != ""
                    gateway_cfg["record_unsupported_tools"] = form.get("record_unsupported_tools", "") != ""
                    gateway_cfg["text_tool_call_fallback_enabled"] = form.get("text_tool_call_fallback_enabled", "") != ""
                    context_cfg = cfg.setdefault("context", {})
                    context_cfg["enabled"] = form.get("context_enabled", "") != ""
                    context_cfg["fanout_enabled"] = form.get("context_fanout_enabled", "") != ""
                    context_cfg["quality_review_enabled"] = form.get("context_quality_review_enabled", "") != ""
                    invalid_field = None
                    for json_key, form_key, default in [
                        ("max_input_tokens", "context_max_input_tokens", 1048576),
                        ("fanout_chunk_tokens", "context_fanout_chunk_tokens", 120000),
                        ("fanout_max_chunks", "context_fanout_max_chunks", 0),
                        ("fanout_max_workers", "context_fanout_max_workers", 4),
                    ]:
                        if invalid_field is not None:
                            break
                        try:
                            context_cfg[json_key] = _admin_form_int(form, (form_key,), context_cfg.get(json_key), default)
                        except ValueError:
                            invalid_field = form_key
                    if invalid_field is not None:
                        _json_response(self, 400, _error_payload(f"invalid numeric field: {invalid_field}"))
                        return
                    save_config(cfg)
                elif path == "/admin/client-config":
                    from .gateway_config import _admin_form_int
                    gateway_cfg = cfg.setdefault("gateway", {})
                    gateway_cfg["public_base_url"] = form.get("public_base_url", "").strip() or "http://127.0.0.1:8885"
                    gateway_cfg["client_snippet_api_key"] = form.get("client_snippet_api_key", "").strip()
                    gateway_cfg["downstream_model_alias"] = form.get("downstream_model_alias", "").strip()
                    gateway_cfg["review_model_alias"] = form.get("review_model_alias", "").strip()
                    gateway_cfg["codex_reasoning_effort"] = form.get("codex_reasoning_effort", "xhigh").strip() or "xhigh"
                    invalid_field = None
                    for field, default in [
                        ("client_context_window", 1048576),
                        ("client_auto_compact_token_limit", 943718),
                        ("client_output_token_limit", 131072),
                    ]:
                        if invalid_field is not None:
                            break
                        try:
                            gateway_cfg[field] = _admin_form_int(form, (field,), gateway_cfg.get(field), default)
                        except ValueError:
                            invalid_field = field
                    if invalid_field is not None:
                        _json_response(self, 400, _error_payload(f"invalid numeric field: {invalid_field}"))
                        return
                    save_config(cfg)
                elif path == "/admin/password":
                    from .gateway_config import _hash_secret
                    old_password = form.get("old_password", "")
                    new_password = form.get("new_password", "")
                    if not old_password or not new_password:
                        _json_response(self, 400, _error_payload("missing old_password or new_password"))
                        return
                    admin = cfg.get("admin", {})
                    if not _constant_time_equal(_hash_secret(old_password), admin.get("password_hash") or ""):
                        _json_response(self, 403, _error_payload("invalid old password"))
                        return
                    admin["password_hash"] = _hash_secret(new_password)
                    admin["must_change_password"] = False
                    cfg["admin"] = admin
                    save_config(cfg)
                elif path == "/admin/downstream-key":
                    action = form.get("action", "add")
                    if action == "add":
                        key_name = form.get("name", "").strip()
                        key_value = form.get("key", "").strip()
                        if not key_name or not key_value:
                            _json_response(self, 400, _error_payload("missing name or key"))
                            return
                        from .gateway_config import _hash_secret
                        import datetime as _dt
                        downstream_keys = cfg.setdefault("downstream_keys", [])
                        downstream_keys.append({
                            "name": key_name,
                            "key_hash": _hash_secret(key_value),
                            "prefix": key_value[:8],
                            "enabled": True,
                            "protocols": ["models", "chat_completions", "responses", "messages", "direct_tools"],
                            "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                        })
                        save_config(cfg)
                    elif action == "delete":
                        key_name = form.get("name", "").strip()
                        downstream_keys = cfg.get("downstream_keys") or []
                        cfg["downstream_keys"] = [k for k in downstream_keys if k.get("name") != key_name]
                        save_config(cfg)
                elif path == "/admin/mcp":
                    action = form.get("action", "add")
                    if action == "add":
                        server_name = form.get("name", "").strip()
                        command = form.get("command", "").strip()
                        if not server_name or not command:
                            _json_response(self, 400, _error_payload("missing name or command"))
                            return
                        import shlex
                        mcp_cfg = cfg.setdefault("mcp", {})
                        servers = mcp_cfg.setdefault("servers", [])
                        servers.append({
                            "name": server_name,
                            "command": shlex.split(command),
                            "enabled": True,
                        })
                        save_config(cfg)
                    elif action == "delete":
                        server_name = form.get("name", "").strip()
                        mcp_cfg = cfg.get("mcp", {})
                        servers = mcp_cfg.get("servers") or []
                        mcp_cfg["servers"] = [s for s in servers if s.get("name") != server_name]
                        save_config(cfg)
                elif path == "/admin/http-actions":
                    action = form.get("action", "add")
                    if action == "add":
                        action_name = form.get("name", "").strip()
                        url = form.get("url", "").strip()
                        if not action_name or not url:
                            _json_response(self, 400, _error_payload("missing name or url"))
                            return
                        actions_cfg = cfg.setdefault("http_actions", {})
                        actions = actions_cfg.setdefault("actions", [])
                        actions.append({
                            "name": action_name,
                            "url": url,
                            "method": form.get("method", "POST").upper(),
                            "description": form.get("description", ""),
                            "enabled": True,
                        })
                        save_config(cfg)
                    elif action == "delete":
                        action_name = form.get("name", "").strip()
                        actions_cfg = cfg.get("http_actions", {})
                        actions = actions_cfg.get("actions") or []
                        actions_cfg["actions"] = [a for a in actions if a.get("name") != action_name]
                        save_config(cfg)
                elif path == "/admin/upstream-profile":
                    action = form.get("action", "save")
                    if action == "save":
                        from .gateway_config import _profile_from_admin_form
                        try:
                            profile = _profile_from_admin_form(form)
                        except ValueError as exc:
                            _json_response(self, 400, _error_payload(str(exc)))
                            return
                        profiles = cfg.setdefault("upstream_profiles", [])
                        existing_idx = None
                        for i, p in enumerate(profiles):
                            if p.get("id") == profile.get("id"):
                                existing_idx = i
                                break
                        if existing_idx is not None:
                            profiles[existing_idx] = profile
                        else:
                            profiles.append(profile)
                        save_config(cfg)
                    elif action == "delete":
                        profile_id = form.get("id", "").strip()
                        profiles = cfg.get("upstream_profiles") or []
                        cfg["upstream_profiles"] = [p for p in profiles if p.get("id") != profile_id]
                        save_config(cfg)
                    elif action == "activate":
                        profile_id = form.get("id", "").strip()
                        cfg["active_upstream_id"] = profile_id
                        save_config(cfg)
                _redirect(self, "/ui")
                return
            if path in SUPPORTED_PATHS or path in TOKEN_COUNT_PATHS or path in DIRECT_TOOL_CALL_PATHS:
                downstream_key = _check_downstream_key(self)
                from .gateway_tool_runtime import _request_slot_scope
                with _request_slot_scope():
                    body = _read_json(self)

                    if path in TOKEN_COUNT_PATHS:
                        from .gateway_tool_runtime import token_count_response
                        response = token_count_response(body)
                        from .gateway_logging import _record_request_stat, _write_request_log
                        _record_request_stat(path, 200)
                        _write_request_log(path, body, 200, response, downstream_key)
                        _json_response(self, 200, response)
                        return
                    if path in DIRECT_TOOL_CALL_PATHS:
                        from .gateway_tool_runtime import execute_direct_tool_call
                        response = execute_direct_tool_call(body)
                        from .gateway_logging import _record_request_stat, _write_request_log
                        _record_request_stat(path, 200)
                        _write_request_log(path, body, 200, response, downstream_key)
                        _json_response(self, 200, response)
                        return
                    stream = body.get("stream", False)
                    if stream:
                        from .gateway_streaming import run_streaming_orchestration
                        run_streaming_orchestration(self, path, body)
                    else:
                        # Check semantic cache for non-streaming requests
                        cache_hit = None
                        query_text = ""
                        try:
                            from .gateway_cache import get_semantic_cache
                            from .gateway_protocol import _last_user_text
                            cache = get_semantic_cache()
                            query_text = _last_user_text(path, body)
                            if query_text:
                                cache_hit = cache.get(query_text)
                        except Exception:
                            cache_hit = None

                        if cache_hit is not None:
                            from .gateway_logging import _record_request_stat, _write_request_log
                            _record_request_stat(path, 200)
                            _write_request_log(path, body, 200, cache_hit, downstream_key)
                            _json_response(self, 200, cache_hit)
                        else:
                            from .gateway_tool_runtime import run_tool_orchestration
                            # Pass client_id for permission checking (use downstream_key as client identifier)
                            response = run_tool_orchestration(path, body, client_id=downstream_key)
                            # Store in semantic cache if eligible
                            try:
                                if query_text and cache_hit is None:
                                    cache.put(query_text, response)
                            except Exception:
                                pass
                            from .gateway_logging import _record_request_stat, _write_request_log
                            _record_request_stat(path, 200)
                            _write_request_log(path, body, 200, response, downstream_key)
                            _json_response(self, 200, response)
                return
            # --- Skill Create ---
            if path == "/admin/skill-create":
                if not _check_admin(self):
                    return
                import pathlib
                form = _read_form(self)
                skill_name = form.get("skill_name", "").strip()
                skill_content = form.get("skill_content", "").strip()
                if not skill_name or not skill_content:
                    _json_response(self, 400, {"error": "skill_name and skill_content required"})
                    return
                safe_name = __import__("re").sub(r"[^A-Za-z0-9_.-]+", "-", skill_name).strip("-")
                skills_dir = pathlib.Path.cwd() / "skills" / safe_name
                skills_dir.mkdir(parents=True, exist_ok=True)
                (skills_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")
                _redirect(self, "/ui#skills")
                return
            # --- Skill Install from Marketplace ---
            if path == "/admin/skill-install.json":
                if not _check_admin(self):
                    return
                body = _read_json(self)
                skill_id = body.get("id", "").strip()
                if not skill_id:
                    _json_response(self, 400, {"error": "id required"})
                    return
                try:
                    from marketplace import get_skill_by_id
                    skill = get_skill_by_id(skill_id)
                    if not skill:
                        _json_response(self, 404, {"error": f"skill not found: {skill_id}"})
                        return
                    import pathlib
                    skills_dir = pathlib.Path.cwd() / "skills" / skill_id
                    skills_dir.mkdir(parents=True, exist_ok=True)
                    content = "# " + skill["name"] + "\n\n" + skill.get("description", "") + "\n"
                    (skills_dir / "SKILL.md").write_text(content, encoding="utf-8")
                    _json_response(self, 200, {"ok": True, "name": skill_id})
                except Exception as exc:
                    _json_response(self, 500, {"error": str(exc)})
                return
            # --- Skill Delete ---
            if path == "/admin/skill-delete.json":
                if not _check_admin(self):
                    return
                body = _read_json(self)
                skill_name = body.get("name", "").strip()
                if not skill_name:
                    _json_response(self, 400, {"error": "name required"})
                    return
                import pathlib, shutil
                skills_dir = pathlib.Path.cwd() / "skills" / skill_name
                if skills_dir.is_dir():
                    shutil.rmtree(skills_dir)
                    _json_response(self, 200, {"ok": True})
                else:
                    _json_response(self, 404, {"error": "skill not found"})
                return
            # --- MCP Install from Marketplace ---
            if path == "/admin/mcp-install.json":
                if not _check_admin(self):
                    return
                body = _read_json(self)
                mcp_id = body.get("id", "").strip()
                if not mcp_id:
                    _json_response(self, 400, {"error": "id required"})
                    return
                try:
                    from marketplace import get_mcp_server_by_id
                    server = get_mcp_server_by_id(mcp_id)
                    if not server:
                        _json_response(self, 404, {"error": f"MCP server not found: {mcp_id}"})
                        return
                    from .gateway_config import load_config, save_config
                    cfg = load_config()
                    mcp_cfg = cfg.setdefault("mcp", {})
                    servers = mcp_cfg.setdefault("servers", [])
                    if any(s.get("name") == mcp_id for s in servers):
                        _json_response(self, 200, {"ok": True, "message": "already installed"})
                        return
                    cmd_parts = server.get("install_command", "").split()
                    servers.append({"name": mcp_id, "command": cmd_parts[0] if cmd_parts else "npx", "args": cmd_parts[1:] if len(cmd_parts) > 1 else [], "enabled": True})
                    save_config(cfg)
                    _json_response(self, 200, {"ok": True, "name": mcp_id})
                except Exception as exc:
                    _json_response(self, 500, {"error": str(exc)})
                return
            _json_response(self, 404, _error_payload("not found"))
        except Exception as exc:
            _handle_error(self, _normalize_request_path(self.path.split("?", 1)[0]), exc)

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


def _handle_error(handler: BaseHTTPRequestHandler, path: str, exc: Exception) -> None:
    from .gateway_errors import GatewayError, UpstreamHTTPError, DownstreamAuthError, GatewayBusyError
    from .gateway_logging import _record_request_stat
    def record_status(status: int) -> None:
        try:
            _record_request_stat(path, status)
        except Exception:
            pass

    if isinstance(exc, UpstreamHTTPError):
        record_status(502)
        _safe_json_response(handler, 502, _error_payload(str(exc), detail=exc.detail, upstream_status=exc.upstream_status))
    elif isinstance(exc, DownstreamAuthError):
        record_status(401)
        _safe_json_response(handler, 401, _error_payload(str(exc)))
    elif isinstance(exc, GatewayBusyError):
        record_status(429)
        _safe_json_response(handler, 429, _error_payload(str(exc)))
    elif isinstance(exc, GatewayError):
        record_status(exc.status)
        _safe_json_response(handler, exc.status, _error_payload(str(exc), detail=exc.detail))
    else:
        if os.environ.get("DEBUG"):
            traceback.print_exc()
        record_status(500)
        _safe_json_response(handler, 500, _error_payload(str(exc)))
