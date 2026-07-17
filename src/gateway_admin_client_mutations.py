"""Transactional Admin mutations for client settings and credentials."""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

from .gateway_http_security import normalize_origin

Json = dict[str, Any]
_PATHS = {"/admin/client-config", "/admin/password", "/admin/downstream-key"}
_PROTOCOLS = ["models", "chat_completions", "responses", "messages", "direct_tools"]


@dataclass(frozen=True)
class AdminClientMutationResult:
    matched: bool
    success: bool = False
    status: int = 0
    error: str = ""


def _failure(status: int, error: str) -> AdminClientMutationResult:
    return AdminClientMutationResult(matched=True, success=False, status=status, error=error)


def _mapping(config: Json, key: str) -> Json:
    value = config.get(key)
    if isinstance(value, dict):
        return value
    replacement: Json = {}
    config[key] = replacement
    return replacement


def _downstream_keys(config: Json) -> list[Json] | None:
    value = config.get("downstream_keys")
    if value is None:
        result: list[Json] = []
        config["downstream_keys"] = result
        return result
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def _apply_client_config(config: Json, form: dict[str, str]) -> AdminClientMutationResult | None:
    from .gateway_config import _admin_form_int

    gateway = _mapping(config, "gateway")
    raw_public_base = str(form.get("public_base_url") or "").strip() or "http://127.0.0.1:8885"
    public_base = normalize_origin(raw_public_base)
    if not public_base:
        return _failure(400, "invalid public_base_url; use an exact http(s) origin")

    numeric_values: dict[str, int] = {}
    for field, default in [
        ("client_context_window", 1048576),
        ("client_auto_compact_token_limit", 943718),
        ("client_output_token_limit", 131072),
    ]:
        try:
            value = _admin_form_int(form, (field,), gateway.get(field), default)
        except ValueError:
            return _failure(400, f"invalid numeric field: {field}")
        if value < 1:
            return _failure(400, f"invalid numeric field: {field}")
        numeric_values[field] = value

    if numeric_values["client_auto_compact_token_limit"] > numeric_values["client_context_window"]:
        return _failure(
            400,
            "client_auto_compact_token_limit must not exceed client_context_window",
        )

    snippet_key = str(form.get("client_snippet_api_key") or "").strip()
    gateway.update(
        {
            "public_base_url": public_base,
            "client_snippet_api_key": snippet_key,
            "downstream_model_alias": str(form.get("downstream_model_alias") or "").strip(),
            "review_model_alias": str(form.get("review_model_alias") or "").strip(),
            "codex_reasoning_effort": str(form.get("codex_reasoning_effort") or "xhigh").strip() or "xhigh",
            **numeric_values,
        }
    )

    # Clearing the snippet credential must revoke the automatically maintained
    # key instead of leaving the previous copied credential active indefinitely.
    if not snippet_key:
        keys = _downstream_keys(config)
        if keys is None:
            return _failure(400, "invalid downstream key configuration")
        config["downstream_keys"] = [item for item in keys if item.get("name") != "client-snippet"]
    return None


def _apply_password(config: Json, form: dict[str, str]) -> AdminClientMutationResult | None:
    from .gateway_config import _hash_password, _verify_password

    old_password = str(form.get("old_password") or "")
    new_password = str(form.get("new_password") or "")
    if not old_password or not new_password:
        return _failure(400, "missing old_password or new_password")
    raw_admin = config.get("admin")
    if not isinstance(raw_admin, dict):
        return _failure(400, "invalid admin configuration")
    if not _verify_password(old_password, str(raw_admin.get("password_hash") or "")):
        return _failure(403, "invalid old password")
    raw_admin["password_hash"] = _hash_password(new_password)
    raw_admin["must_change_password"] = False
    return None


def _apply_downstream_key(
    config: Json,
    form: dict[str, str],
    *,
    now: Callable[[], dt.datetime],
) -> AdminClientMutationResult | None:
    from .gateway_config import _downstream_key_id, _hash_secret, _secret_fingerprint

    action = str(form.get("action") or "add").strip().lower()
    key_name = str(form.get("name") or "").strip()
    keys = _downstream_keys(config)
    if keys is None:
        return _failure(400, "invalid downstream key configuration")

    if action == "add":
        key_value = str(form.get("key") or "").strip()
        if not key_name or not key_value:
            return _failure(400, "missing name or key")
        key_hash = _hash_secret(key_value)
        if any(str(item.get("name") or "") == key_name for item in keys):
            return _failure(409, "downstream key name already exists")
        if any(str(item.get("key_hash") or "") == key_hash for item in keys):
            return _failure(409, "downstream key value already exists")
        item: Json = {
            "name": key_name,
            "key_hash": key_hash,
            "prefix": _secret_fingerprint(key_value),
            "enabled": True,
            "protocols": list(_PROTOCOLS),
            "created_at": now().astimezone(dt.timezone.utc).isoformat(),
        }
        item["id"] = _downstream_key_id(item)
        keys.append(item)
        return None

    if action == "delete":
        if not key_name:
            return _failure(400, "missing name")
        config["downstream_keys"] = [item for item in keys if str(item.get("name") or "") != key_name]
        return None

    return _failure(400, "invalid downstream key action")


def apply_admin_client_mutation(
    path: str,
    config: Json,
    revision: str,
    form: dict[str, str],
    *,
    now: Callable[[], dt.datetime] | None = None,
) -> AdminClientMutationResult:
    """Validate on a copy and persist exactly once when the mutation succeeds."""
    if path not in _PATHS:
        return AdminClientMutationResult(matched=False)

    candidate = copy.deepcopy(config)
    failure: AdminClientMutationResult | None
    if path == "/admin/client-config":
        failure = _apply_client_config(candidate, form)
    elif path == "/admin/password":
        failure = _apply_password(candidate, form)
    else:
        clock = now or (lambda: dt.datetime.now(dt.timezone.utc))
        failure = _apply_downstream_key(candidate, form, now=clock)
    if failure is not None:
        return failure

    from .gateway_config import save_config

    save_config(candidate, expected_revision=revision)
    return AdminClientMutationResult(matched=True, success=True)


__all__ = ["AdminClientMutationResult", "apply_admin_client_mutation"]
