"""Transactional Admin mutations for MCP, HTTP Actions, and upstream profiles."""
from __future__ import annotations

import copy
import shlex
from dataclasses import dataclass
from typing import Any, Callable

Json = dict[str, Any]
_PATHS = {"/admin/mcp", "/admin/mcp-reload", "/admin/http-actions", "/admin/upstream-profile"}
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_UPSTREAM_PROTOCOLS = {"openai_chat", "openai_responses", "anthropic_messages"}


@dataclass(frozen=True)
class AdminConnectorMutationResult:
    matched: bool
    success: bool = False
    status: int = 0
    error: str = ""


def _failure(status: int, error: str) -> AdminConnectorMutationResult:
    return AdminConnectorMutationResult(matched=True, success=False, status=status, error=error)


def _mapping(config: Json, key: str) -> Json:
    value = config.get(key)
    if isinstance(value, dict):
        return value
    replacement: Json = {}
    config[key] = replacement
    return replacement


def _dict_list(container: Json, key: str) -> list[Json] | None:
    value = container.get(key)
    if value is None:
        result: list[Json] = []
        container[key] = result
        return result
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def _apply_mcp(config: Json, form: dict[str, str]) -> AdminConnectorMutationResult | None:
    action = str(form.get("action") or "add").strip().lower()
    name = str(form.get("name") or "").strip()
    mcp = _mapping(config, "mcp")
    servers = _dict_list(mcp, "servers")
    if servers is None:
        return _failure(400, "invalid MCP server configuration")

    if action == "add":
        command_text = str(form.get("command") or "").strip()
        if not name or not command_text:
            return _failure(400, "missing name or command")
        if any(str(item.get("name") or "") == name for item in servers):
            return _failure(409, "MCP server name already exists")
        try:
            command = shlex.split(command_text)
        except ValueError as exc:
            return _failure(400, f"invalid MCP command: {exc}")
        if not command or any(not part or "\x00" in part for part in command):
            return _failure(400, "invalid MCP command")
        servers.append({"name": name, "command": command, "enabled": True})
        return None

    if action == "delete":
        if not name:
            return _failure(400, "missing name")
        mcp["servers"] = [item for item in servers if str(item.get("name") or "") != name]
        return None

    return _failure(400, "invalid MCP action")


def _form_bool(form: dict[str, str], key: str) -> bool:
    return str(form.get(key) or "").strip().lower() in {"1", "true", "yes", "on"}


def _apply_http_action(config: Json, form: dict[str, str]) -> AdminConnectorMutationResult | None:
    from .gateway_errors import ToolExecutionError
    from .gateway_http_actions import _validate_action_url

    action = str(form.get("action") or "add").strip().lower()
    name = str(form.get("name") or "").strip()
    http_actions = _mapping(config, "http_actions")
    actions = _dict_list(http_actions, "actions")
    if actions is None:
        return _failure(400, "invalid HTTP Action configuration")

    if action == "add":
        url = str(form.get("url") or "").strip()
        if not name or not url:
            return _failure(400, "missing name or url")
        if any(str(item.get("name") or "") == name for item in actions):
            return _failure(409, "HTTP Action name already exists")
        method = str(form.get("method") or "POST").strip().upper()
        if method not in _HTTP_METHODS:
            return _failure(400, "invalid HTTP Action method")
        configured: Json = {
            "name": name,
            "url": url,
            "method": method,
            "description": str(form.get("description") or ""),
            "enabled": True,
            "allow_private_network": _form_bool(form, "allow_private_network"),
        }
        try:
            _validate_action_url(url, configured, resolve_dns=False)
        except ToolExecutionError as exc:
            return _failure(400, str(exc))
        actions.append(configured)
        return None

    if action == "delete":
        if not name:
            return _failure(400, "missing name")
        http_actions["actions"] = [item for item in actions if str(item.get("name") or "") != name]
        return None

    return _failure(400, "invalid HTTP Action action")


def _profiles(config: Json) -> list[Json] | None:
    value = config.get("upstream_profiles")
    if value is None:
        result: list[Json] = []
        config["upstream_profiles"] = result
        return result
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def _apply_upstream_profile(config: Json, form: dict[str, str]) -> AdminConnectorMutationResult | None:
    from .gateway_config import _profile_from_admin_form

    action = str(form.get("action") or "save").strip().lower()
    profiles = _profiles(config)
    if profiles is None:
        return _failure(400, "invalid upstream profile configuration")

    requested_id = str(form.get("profile_id") or form.get("id") or "").strip()
    if action == "save":
        existing = next(
            (item for item in profiles if str(item.get("id") or "") == (requested_id or "default")),
            None,
        )
        effective_form = dict(form)
        if existing is not None:
            effective_form.setdefault("protocol", str(existing.get("protocol") or "openai_chat"))
            effective_form.setdefault("tools_enabled", str(existing.get("tools_enabled") or "adapter"))
        try:
            profile = _profile_from_admin_form(effective_form, existing)
        except ValueError as exc:
            return _failure(400, str(exc))
        if str(profile.get("protocol") or "") not in _UPSTREAM_PROTOCOLS:
            return _failure(400, "invalid upstream protocol")
        for field in ("timeout_seconds", "max_input_tokens", "max_output_tokens", "max_concurrency"):
            try:
                if float(profile.get(field) or 0) <= 0:
                    return _failure(400, f"invalid numeric field: {field}")
            except (TypeError, ValueError):
                return _failure(400, f"invalid numeric field: {field}")
        profile_id = str(profile.get("id") or "")
        indexes = [index for index, item in enumerate(profiles) if str(item.get("id") or "") == profile_id]
        if len(indexes) > 1:
            return _failure(409, "duplicate upstream profile id")
        if indexes:
            profiles[indexes[0]] = profile
        else:
            profiles.append(profile)
        active_id = str(config.get("active_upstream_id") or config.get("active_upstream") or "")
        if active_id == profile_id or (not active_id and profile_id == "default"):
            config["active_upstream_id"] = profile_id
            config["active_upstream"] = profile_id
            config["upstream"] = copy.deepcopy(profile)
        return None

    if action == "activate":
        if not requested_id:
            return _failure(400, "missing upstream profile id")
        profile = next((item for item in profiles if str(item.get("id") or "") == requested_id), None)
        if profile is None:
            return _failure(404, "upstream profile not found")
        config["active_upstream_id"] = requested_id
        config["active_upstream"] = requested_id
        config["upstream"] = copy.deepcopy(profile)
        return None

    if action == "delete":
        if not requested_id:
            return _failure(400, "missing upstream profile id")
        active_id = str(config.get("active_upstream_id") or config.get("active_upstream") or "")
        if requested_id == active_id:
            return _failure(409, "cannot delete active upstream profile")
        config["upstream_profiles"] = [
            item for item in profiles if str(item.get("id") or "") != requested_id
        ]
        return None

    return _failure(400, "invalid upstream profile action")


def _close_mcp_sessions() -> None:
    from .gateway_mcp import _mcp_close_sessions

    _mcp_close_sessions()


def apply_admin_connector_mutation(
    path: str,
    config: Json,
    revision: str,
    form: dict[str, str],
    *,
    reload_mcp: Callable[[], None] | None = None,
) -> AdminConnectorMutationResult:
    """Validate on a copy, save once, then apply required runtime side effects."""
    if path not in _PATHS:
        return AdminConnectorMutationResult(matched=False)
    reloader = reload_mcp or _close_mcp_sessions
    if path == "/admin/mcp-reload":
        reloader()
        return AdminConnectorMutationResult(matched=True, success=True)

    candidate = copy.deepcopy(config)
    if path == "/admin/mcp":
        failure = _apply_mcp(candidate, form)
    elif path == "/admin/http-actions":
        failure = _apply_http_action(candidate, form)
    else:
        failure = _apply_upstream_profile(candidate, form)
    if failure is not None:
        return failure

    from .gateway_config import save_config

    save_config(candidate, expected_revision=revision)
    if path == "/admin/mcp":
        reloader()
    return AdminConnectorMutationResult(matched=True, success=True)


__all__ = ["AdminConnectorMutationResult", "apply_admin_connector_mutation"]
