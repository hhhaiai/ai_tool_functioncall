"""Transactional mutation for the main Gateway Admin configuration form."""
from __future__ import annotations

import copy
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .gateway_http_security import normalize_origin

Json = dict[str, Any]
_PATH = "/admin/config"
_UPSTREAM_PROTOCOLS = {"openai_chat", "openai_responses", "anthropic_messages"}
_TOOL_MODES = {"orchestrate", "passthrough", "native_passthrough", "proxy"}


@dataclass(frozen=True)
class AdminConfigMutationResult:
    matched: bool
    success: bool = False
    status: int = 0
    error: str = ""


def _failure(error: str) -> AdminConfigMutationResult:
    return AdminConfigMutationResult(matched=True, success=False, status=400, error=error)


def _mapping(config: Json, key: str) -> Json:
    value = config.get(key)
    if isinstance(value, dict):
        return value
    replacement: Json = {}
    config[key] = replacement
    return replacement


def _positive_int(
    form: dict[str, str],
    key: str,
    existing: Any,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    from .gateway_config import _admin_form_int

    value = _admin_form_int(form, (key,), existing, default)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"invalid numeric field: {key}")
    return value


def _bounded_float(
    form: dict[str, str],
    key: str,
    existing: Any,
    default: float,
    *,
    minimum: float,
) -> float:
    from .gateway_config import _admin_form_float

    value = _admin_form_float(form, (key,), existing, default)
    if value < minimum:
        raise ValueError(f"invalid numeric field: {key}")
    return value


def _validate_upstream_profile(profile: Json) -> str | None:
    protocol = str(profile.get("protocol") or "")
    if protocol not in _UPSTREAM_PROTOCOLS:
        return "invalid upstream protocol"
    base_url = str(profile.get("base_url") or "").strip()
    try:
        parsed = urllib.parse.urlparse(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        return "invalid upstream base_url"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        return "invalid upstream base_url"
    del port
    for field in ("timeout_seconds", "max_input_tokens", "max_output_tokens", "max_concurrency"):
        try:
            if float(profile.get(field) or 0) <= 0:
                return f"invalid numeric field: {field}"
        except (TypeError, ValueError):
            return f"invalid numeric field: {field}"
    if int(profile["max_output_tokens"]) > int(profile["max_input_tokens"]):
        return "upstream_max_output_tokens must not exceed upstream_max_input_tokens"
    paths = profile.get("paths")
    if not isinstance(paths, dict):
        return "invalid upstream paths"
    for name in ("models", "chat_completions", "responses", "messages"):
        value = str(paths.get(name) or "")
        parsed_path = urllib.parse.urlparse(value)
        segments = [segment for segment in parsed_path.path.split("/") if segment]
        if (
            not value.startswith("/")
            or value.startswith("//")
            or bool(parsed_path.scheme)
            or bool(parsed_path.netloc)
            or bool(parsed_path.query)
            or bool(parsed_path.fragment)
            or ".." in segments
        ):
            return f"invalid upstream path: {name}"
    return None


def _profiles(config: Json) -> list[Json] | None:
    value = config.get("upstream_profiles")
    if value is None:
        result: list[Json] = []
        config["upstream_profiles"] = result
        return result
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        return None
    return value


def apply_admin_config_mutation(
    path: str,
    config: Json,
    revision: str,
    form: dict[str, str],
) -> AdminConfigMutationResult:
    if path != _PATH:
        return AdminConfigMutationResult(matched=False)

    from .gateway_config import _profile_from_admin_form, save_config

    candidate = copy.deepcopy(config)
    existing_upstream = candidate.get("upstream") if isinstance(candidate.get("upstream"), dict) else None
    try:
        profile = _profile_from_admin_form(form, existing_upstream)
    except ValueError as exc:
        return _failure(str(exc))
    profile_error = _validate_upstream_profile(profile)
    if profile_error:
        return _failure(profile_error)

    profiles = _profiles(candidate)
    if profiles is None:
        return _failure("invalid upstream profile configuration")
    existing_ids = [str(item.get("id") or "") for item in profiles]
    if len(existing_ids) != len(set(existing_ids)):
        return _failure("duplicate upstream profile id")
    profile_id = str(profile.get("id") or "")
    indexes = [index for index, item in enumerate(profiles) if str(item.get("id") or "") == profile_id]
    if indexes:
        profiles[indexes[0]] = profile
    else:
        profiles.append(profile)
    candidate["active_upstream_id"] = profile_id
    candidate["active_upstream"] = profile_id
    candidate["upstream"] = profile
    candidate["upstream_profiles"] = profiles

    gateway = _mapping(candidate, "gateway")
    tool_mode = str(form.get("tool_mode") or gateway.get("tool_mode") or "orchestrate").strip().lower()
    if tool_mode not in _TOOL_MODES:
        return _failure("invalid gateway tool_mode")
    gateway["tool_mode"] = tool_mode
    try:
        gateway["max_tool_rounds"] = _positive_int(
            form, "max_tool_rounds", gateway.get("max_tool_rounds"), 10, maximum=100
        )
        gateway["max_concurrent_requests"] = _positive_int(
            form, "max_concurrent_requests", gateway.get("max_concurrent_requests"), 32, maximum=10000
        )
        gateway["text_tool_adapter_compact_token_limit"] = _positive_int(
            form,
            "text_tool_adapter_compact_token_limit",
            gateway.get("text_tool_adapter_compact_token_limit"),
            48000,
        )
        gateway["concurrency_queue_timeout_seconds"] = _bounded_float(
            form,
            "concurrency_queue_timeout_seconds",
            gateway.get("concurrency_queue_timeout_seconds"),
            5.0,
            minimum=0.0,
        )
        gateway["tool_execution_timeout_seconds"] = _bounded_float(
            form,
            "tool_execution_timeout_seconds",
            gateway.get("tool_execution_timeout_seconds"),
            60.0,
            minimum=0.001,
        )
    except ValueError as exc:
        return _failure(str(exc))

    gateway["allow_write_tools"] = "allow_write_tools" in form and bool(form.get("allow_write_tools"))
    gateway["allow_shell_tools"] = "allow_shell_tools" in form and bool(form.get("allow_shell_tools"))
    gateway["request_logging"] = "request_logging" in form and bool(form.get("request_logging"))
    gateway["record_unsupported_tools"] = "record_unsupported_tools" in form and bool(form.get("record_unsupported_tools"))
    gateway["text_tool_call_fallback_enabled"] = (
        "text_tool_call_fallback_enabled" in form and bool(form.get("text_tool_call_fallback_enabled"))
    )
    gateway["cors_enabled"] = "cors_enabled" in form and bool(form.get("cors_enabled"))
    raw_origins = [item.strip() for item in str(form.get("cors_allowed_origins") or "").split(",") if item.strip()]
    normalized_origins = [normalize_origin(item) for item in raw_origins]
    if any(origin is None for origin in normalized_origins):
        return _failure("invalid CORS origin; use exact http(s) origins")
    gateway["cors_allowed_origins"] = list(dict.fromkeys(normalized_origins))

    context = _mapping(candidate, "context")
    context["enabled"] = "context_enabled" in form and bool(form.get("context_enabled"))
    context["fanout_enabled"] = "context_fanout_enabled" in form and bool(form.get("context_fanout_enabled"))
    context["quality_review_enabled"] = (
        "context_quality_review_enabled" in form and bool(form.get("context_quality_review_enabled"))
    )
    try:
        context["max_input_tokens"] = _positive_int(
            form, "context_max_input_tokens", context.get("max_input_tokens"), 1048576
        )
        context["fanout_chunk_tokens"] = _positive_int(
            form, "context_fanout_chunk_tokens", context.get("fanout_chunk_tokens"), 120000
        )
        context["fanout_max_chunks"] = _positive_int(
            form,
            "context_fanout_max_chunks",
            context.get("fanout_max_chunks"),
            0,
            minimum=0,
            maximum=10000,
        )
        context["fanout_max_workers"] = _positive_int(
            form,
            "context_fanout_max_workers",
            context.get("fanout_max_workers"),
            4,
            maximum=256,
        )
    except ValueError as exc:
        return _failure(str(exc))
    if int(context["fanout_chunk_tokens"]) > int(context["max_input_tokens"]):
        return _failure("context_fanout_chunk_tokens must not exceed context_max_input_tokens")

    save_config(candidate, expected_revision=revision)
    return AdminConfigMutationResult(matched=True, success=True)


__all__ = ["AdminConfigMutationResult", "apply_admin_config_mutation"]
