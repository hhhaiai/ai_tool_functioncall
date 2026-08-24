#!/usr/bin/env python3
"""Capability-aware, health-aware upstream model routing."""
from __future__ import annotations

import copy
import random
import threading
import time
from typing import Any, Callable, Iterable

from .gateway_config import (
    MODEL_CAPABILITY_KEYS,
    canonical_tools_enabled,
    flatten_profile_models,
    model_capability_snapshot,
)
from .gateway_errors import ConfigError, UpstreamHTTPError, UpstreamTimeoutError

Json = dict[str, Any]
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
_MODEL_ROUTING_STRATEGIES = {
    "failover",
    "first",
    "least_connections",
    "random",
    "round_robin",
}
_TEXT_TOOL_ADAPTER_CAPABILITIES = {
    "supports_tools",
    "supports_function_calls",
    "supports_parallel_tool_calls",
}
_TEXT_TOOL_ADAPTER_MODES = {"adapter", "prompt", "text_only"}


class NoModelCapabilityError(ConfigError):
    """No configured model declares the requested capability."""


class NoHealthyModelRouteError(ConfigError):
    """Models declare the capability, but none currently has a usable route."""


class ModelRoute:
    """One immutable model/profile selection from one config snapshot."""

    __slots__ = (
        "profile_id",
        "profile_name",
        "model",
        "base_url",
        "api_key",
        "protocol",
        "paths",
        "timeout_seconds",
        "max_input_tokens",
        "max_output_tokens",
        "capabilities",
        "profile",
        "key",
    )

    def __init__(self, row: Json, profile: Json, model_entry: Json) -> None:
        selected_profile = copy.deepcopy(profile)
        model_name = str(row.get("model") or "")
        selected_profile["model"] = model_name
        selected_profile["capabilities"] = dict(row.get("capabilities") or {})
        for field in ("max_input_tokens", "max_output_tokens"):
            if model_entry.get(field) is not None:
                try:
                    value = int(model_entry[field])
                except (TypeError, ValueError) as exc:
                    raise ConfigError(
                        f"invalid {field} for model {model_name!r}"
                    ) from exc
                if value < 1:
                    raise ConfigError(f"invalid {field} for model {model_name!r}")
                selected_profile[field] = value

        self.profile_id = str(row.get("profile_id") or "")
        self.profile_name = str(row.get("profile_name") or self.profile_id)
        self.model = model_name
        self.base_url = str(selected_profile.get("base_url") or "").rstrip("/")
        self.api_key = str(selected_profile.get("api_key") or "")
        self.protocol = str(selected_profile.get("protocol") or "openai_chat")
        self.paths = (
            dict(selected_profile.get("paths"))
            if isinstance(selected_profile.get("paths"), dict)
            else {}
        )
        self.timeout_seconds = float(
            selected_profile.get("timeout_seconds", 60.0) or 60.0
        )
        self.max_input_tokens = int(selected_profile.get("max_input_tokens", 0) or 0)
        self.max_output_tokens = int(selected_profile.get("max_output_tokens", 0) or 0)
        self.capabilities = dict(row.get("capabilities") or {})
        self.profile = selected_profile
        self.key = f"{self.profile_id}/{self.model}"

    def __repr__(self) -> str:  # type: ignore[override]
        return (
            f"ModelRoute(profile={self.profile_id!r}, model={self.model!r}, "
            f"base_url={self.base_url!r})"
        )

    def to_dict(self) -> Json:
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "protocol": self.protocol,
            "paths": dict(self.paths),
            "timeout_seconds": self.timeout_seconds,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "capabilities": dict(self.capabilities),
        }


class ModelRouter:
    """Select upstream models by capability across one live config snapshot."""

    def __init__(self, config_provider: Callable[[], Json]) -> None:
        self._config_provider = config_provider
        self._lock = threading.RLock()
        self._round_robin_indices: dict[str, int] = {}
        self._route_state: dict[str, Json] = {}

    def snapshot(self) -> Json:
        return model_capability_snapshot(self._config_snapshot())

    def all_models(self) -> list[Json]:
        return flatten_profile_models(self._config_snapshot())

    def models_for_capability(self, capability: str) -> list[Json]:
        self._validate_capability(capability)
        return [
            row
            for row in flatten_profile_models(self._config_snapshot())
            if row["capabilities"].get(capability)
        ]

    def select_for_capability(
        self,
        capability: str,
        *,
        exclude: set[str] | None = None,
        strategy: str = "failover",
    ) -> ModelRoute | None:
        self._validate_capability(capability)
        route, _declared = self._select_from_snapshot(
            self._config_snapshot(), capability, exclude or set(), strategy
        )
        return route

    def select_or_raise(
        self,
        capability: str,
        *,
        exclude: set[str] | None = None,
        strategy: str = "failover",
    ) -> ModelRoute:
        self._validate_capability(capability)
        route, declared = self._select_from_snapshot(
            self._config_snapshot(), capability, exclude or set(), strategy
        )
        if route is not None:
            return route
        if declared:
            raise NoHealthyModelRouteError(
                f"no healthy upstream route declares capability {capability!r}"
            )
        raise NoModelCapabilityError(
            f"no upstream model declares capability {capability!r}"
        )

    def select_for_capabilities(
        self,
        capabilities: Iterable[str],
        *,
        exclude: set[str] | None = None,
        strategy: str = "failover",
    ) -> ModelRoute | None:
        required = self._normalize_required_capabilities(capabilities)
        route, _declared = self._select_capabilities_from_snapshot(
            self._config_snapshot(), required, exclude or set(), strategy
        )
        return route

    def select_or_raise_for_capabilities(
        self,
        capabilities: Iterable[str],
        *,
        exclude: set[str] | None = None,
        strategy: str = "failover",
    ) -> ModelRoute:
        required = self._normalize_required_capabilities(capabilities)
        route, declared = self._select_capabilities_from_snapshot(
            self._config_snapshot(), required, exclude or set(), strategy
        )
        if route is not None:
            return route
        label = ", ".join(required)
        if declared:
            raise NoHealthyModelRouteError(
                f"no healthy upstream route declares all capabilities: {label}"
            )
        raise NoModelCapabilityError(
            f"no upstream model declares all capabilities: {label}"
        )

    def select_adapter_for_capabilities(
        self,
        capabilities: Iterable[str],
        *,
        exclude: set[str] | None = None,
        strategy: str = "failover",
    ) -> ModelRoute | None:
        """Select a route whose profile can safely emulate tool capabilities.

        Text-tool adaptation only substitutes the tool protocol flags. Other
        requirements, such as streaming, vision or JSON Schema, must still be
        declared by the selected model.
        """
        required = self._normalize_required_capabilities(capabilities)
        route, _declared = self._select_adapter_from_snapshot(
            self._config_snapshot(), required, exclude or set(), strategy
        )
        return route

    def select_request_route(
        self,
        capabilities: Iterable[str],
        *,
        exclude: set[str] | None = None,
        strategy: str | None = None,
    ) -> tuple[ModelRoute, str, bool]:
        """Select one request route from one config snapshot.

        The configured concurrency strategy is used when ``strategy`` is not
        explicitly supplied. Native capability matching is always preferred;
        a text-tool adapter is considered only when no healthy native route
        satisfies the complete capability set.
        """
        required = self._normalize_required_capabilities(capabilities)
        config = self._config_snapshot()
        resolved_strategy = self._resolve_request_strategy(config, strategy)
        excluded = exclude or set()
        route, native_declared = self._select_capabilities_from_snapshot(
            config,
            required,
            excluded,
            resolved_strategy,
        )
        if route is not None:
            return route, resolved_strategy, False

        adapter_route, adapter_declared = self._select_adapter_from_snapshot(
            config,
            required,
            excluded,
            resolved_strategy,
        )
        if adapter_route is not None:
            return adapter_route, resolved_strategy, True

        label = ", ".join(required)
        if native_declared or adapter_declared:
            raise NoHealthyModelRouteError(
                f"no healthy upstream route can satisfy all capabilities: {label}"
            )
        raise NoModelCapabilityError(
            f"no upstream model or text-tool adapter can satisfy all capabilities: {label}"
        )

    def request_start(self, route: ModelRoute) -> float:
        started = time.monotonic()
        with self._lock:
            state = self._route_state.setdefault(route.key, {})
            state["active_requests"] = int(state.get("active_requests") or 0) + 1
        return started

    def request_success(self, route: ModelRoute, started: float) -> None:
        latency = max(0.0, time.monotonic() - started)
        with self._lock:
            state = self._route_state.setdefault(route.key, {})
            previous = float(state.get("latency_seconds") or 0.0)
            state["latency_seconds"] = latency if previous <= 0 else previous * 0.8 + latency * 0.2
            state["unhealthy_until"] = 0.0

    def request_failure(self, route: ModelRoute, exc: BaseException) -> None:
        if not self.is_retryable_failure(exc):
            return
        try:
            cooldown = float(route.profile.get("model_unhealthy_cooldown_seconds", 30.0) or 30.0)
        except (TypeError, ValueError):
            cooldown = 30.0
        with self._lock:
            state = self._route_state.setdefault(route.key, {})
            state["unhealthy_until"] = time.monotonic() + max(0.0, cooldown)
            state["last_failure"] = exc.__class__.__name__

    def request_end(self, route: ModelRoute) -> None:
        with self._lock:
            state = self._route_state.setdefault(route.key, {})
            state["active_requests"] = max(
                0, int(state.get("active_requests") or 0) - 1
            )

    @staticmethod
    def is_retryable_failure(exc: BaseException) -> bool:
        if isinstance(exc, UpstreamTimeoutError):
            return True
        return (
            isinstance(exc, UpstreamHTTPError)
            and exc.upstream_status in _RETRYABLE_STATUS_CODES
        )

    def _config_snapshot(self) -> Json:
        config = self._config_provider()
        return copy.deepcopy(config) if isinstance(config, dict) else {}

    @staticmethod
    def _validate_capability(capability: str) -> None:
        if capability not in MODEL_CAPABILITY_KEYS:
            raise ConfigError(f"unknown capability: {capability!r}")

    def _normalize_required_capabilities(
        self, capabilities: Iterable[str]
    ) -> tuple[str, ...]:
        requested = {str(capability) for capability in capabilities}
        if not requested:
            raise ConfigError("at least one model capability is required")
        for capability in requested:
            self._validate_capability(capability)
        return tuple(
            capability
            for capability in MODEL_CAPABILITY_KEYS
            if capability in requested
        )

    @staticmethod
    def _resolve_request_strategy(config: Json, strategy: str | None) -> str:
        if strategy is None:
            concurrency = (
                config.get("concurrency")
                if isinstance(config.get("concurrency"), dict)
                else {}
            )
            strategy = str(
                concurrency.get("load_balance_strategy") or "round_robin"
            )
        normalized = str(strategy).strip().lower()
        if normalized not in _MODEL_ROUTING_STRATEGIES:
            raise ConfigError(f"unknown model routing strategy: {strategy!r}")
        return normalized

    def _select_from_snapshot(
        self,
        config: Json,
        capability: str,
        exclude: set[str],
        strategy: str,
    ) -> tuple[ModelRoute | None, bool]:
        return self._select_capabilities_from_snapshot(
            config,
            (capability,),
            exclude,
            strategy,
        )

    def _select_capabilities_from_snapshot(
        self,
        config: Json,
        capabilities: tuple[str, ...],
        exclude: set[str],
        strategy: str,
    ) -> tuple[ModelRoute | None, bool]:
        from .gateway_config import _normalized_profiles

        active_id = str(config.get("active_upstream_id") or "")
        declared = False
        candidates: list[tuple[Json, Json, Json]] = []
        for profile in _normalized_profiles(config):
            profile_id = str(profile.get("id") or "")
            for model_entry in profile.get("models") or []:
                if not isinstance(model_entry, dict):
                    continue
                model = str(model_entry.get("name") or "").strip()
                caps = dict(model_entry.get("capabilities") or {})
                if not all(caps.get(capability) for capability in capabilities):
                    continue
                declared = True
                row = {
                    "profile_id": profile_id,
                    "profile_name": str(profile.get("name") or profile_id),
                    "model": model,
                    "capabilities": caps,
                    "is_active_profile": profile_id == active_id,
                }
                key = f"{profile_id}/{model}"
                if key in exclude:
                    continue
                if not self._route_is_eligible(profile, model_entry, key):
                    continue
                candidates.append((row, profile, model_entry))

        selection_key = "&".join(capabilities)
        selected = self._pick(selection_key, candidates, strategy)
        if selected is None:
            return None, declared
        row, profile, model_entry = selected
        return ModelRoute(row, profile, model_entry), declared

    def _select_adapter_from_snapshot(
        self,
        config: Json,
        capabilities: tuple[str, ...],
        exclude: set[str],
        strategy: str,
    ) -> tuple[ModelRoute | None, bool]:
        from .gateway_config import _normalized_profiles

        adaptable = set(capabilities) & _TEXT_TOOL_ADAPTER_CAPABILITIES
        if not adaptable:
            return None, False

        native_required = tuple(
            capability
            for capability in capabilities
            if capability not in _TEXT_TOOL_ADAPTER_CAPABILITIES
        )
        active_id = str(config.get("active_upstream_id") or "")
        declared = False
        candidates: list[tuple[Json, Json, Json]] = []
        for profile in _normalized_profiles(config):
            profile_id = str(profile.get("id") or "")
            mode = canonical_tools_enabled(profile.get("tools_enabled"))
            for model_entry in profile.get("models") or []:
                if not isinstance(model_entry, dict):
                    continue
                model = str(model_entry.get("name") or "").strip()
                caps = dict(model_entry.get("capabilities") or {})
                native_tools = bool(caps.get("supports_tools")) and bool(
                    caps.get("supports_function_calls")
                )
                adapter_enabled = mode in _TEXT_TOOL_ADAPTER_MODES or (
                    mode == "auto" and not native_tools
                )
                if not adapter_enabled or not all(
                    caps.get(capability) for capability in native_required
                ):
                    continue
                declared = True
                row = {
                    "profile_id": profile_id,
                    "profile_name": str(profile.get("name") or profile_id),
                    "model": model,
                    "capabilities": caps,
                    "is_active_profile": profile_id == active_id,
                }
                key = f"{profile_id}/{model}"
                if key in exclude:
                    continue
                if not self._route_is_eligible(profile, model_entry, key):
                    continue
                candidates.append((row, profile, model_entry))

        selection_key = "adapter:" + "&".join(capabilities)
        selected = self._pick(selection_key, candidates, strategy)
        if selected is None:
            return None, declared
        row, profile, model_entry = selected
        return ModelRoute(row, profile, model_entry), declared

    def _route_is_eligible(self, profile: Json, model: Json, key: str) -> bool:
        if profile.get("enabled") is False or profile.get("load_balance_enabled") is False:
            return False
        if model.get("enabled") is False or model.get("load_balance_enabled") is False:
            return False
        if not str(profile.get("base_url") or "").strip():
            return False
        if not str(model.get("name") or "").strip():
            return False
        if profile.get("healthy") is False or model.get("healthy") is False:
            return False
        if str(profile.get("health_status") or "").lower() in {"down", "failed", "unhealthy"}:
            return False
        if str(model.get("health_status") or "").lower() in {"down", "failed", "unhealthy"}:
            return False
        with self._lock:
            unhealthy_until = float(
                self._route_state.get(key, {}).get("unhealthy_until") or 0.0
            )
        return unhealthy_until <= time.monotonic()

    def _pick(
        self,
        capability: str,
        candidates: list[tuple[Json, Json, Json]],
        strategy: str,
    ) -> tuple[Json, Json, Json] | None:
        if not candidates:
            return None
        if strategy in {"first", "failover"}:
            return candidates[0]
        if strategy == "random":
            return random.choice(candidates)
        if strategy == "least_connections":
            return min(candidates, key=lambda item: self._load_score(item[0]))
        if strategy != "round_robin":
            raise ConfigError(f"unknown model routing strategy: {strategy!r}")
        with self._lock:
            index = self._round_robin_indices.get(capability, 0) % len(candidates)
            self._round_robin_indices[capability] = index + 1
        return candidates[index]

    def _load_score(self, row: Json) -> tuple[int, float, int]:
        key = f"{row.get('profile_id')}/{row.get('model')}"
        with self._lock:
            state = self._route_state.get(key, {})
            active = int(state.get("active_requests") or 0)
            latency = float(state.get("latency_seconds") or 0.0)
        return active, latency, 0 if row.get("is_active_profile") else 1


_model_router: ModelRouter | None = None
_model_router_lock = threading.Lock()


def get_model_router() -> ModelRouter:
    """Return the process-wide router so strategy and health state persist."""
    global _model_router
    if _model_router is None:
        with _model_router_lock:
            if _model_router is None:
                from .gateway_config import load_config

                _model_router = ModelRouter(load_config)
    return _model_router


def reset_model_router() -> None:
    """Drop process-wide routing/health state after a config mutation or in tests."""
    global _model_router
    with _model_router_lock:
        _model_router = None


def _iter_request_content_parts(path: str, body: Json) -> Iterable[Json]:
    containers: list[Any] = []
    if "/responses" in path:
        raw_input = body.get("input")
        for item in raw_input if isinstance(raw_input, list) else []:
            if not isinstance(item, dict):
                continue
            containers.append([item])
            containers.append(item.get("content"))
    else:
        for message in body.get("messages") or []:
            if isinstance(message, dict):
                containers.append(message.get("content"))

    while containers:
        value = containers.pop()
        if not isinstance(value, list):
            continue
        for part in value:
            if not isinstance(part, dict):
                continue
            yield part
            if part.get("type") == "tool_result":
                containers.append(part.get("content"))


def required_capabilities_for_request(path: str, body: Json) -> tuple[str, ...]:
    """Infer only transport/model capabilities required by this request.

    The returned tuple contains no tenant, workspace, prompt or credential
    data and is safe to pass to the process-wide router.
    """
    required: set[str] = set()
    if body.get("stream") is True:
        required.add("supports_streaming")
    modalities = body.get("modalities")
    if isinstance(modalities, list) and any(
        str(modality).strip().lower() == "audio" for modality in modalities
    ):
        required.add("supports_speech")

    tools = body.get("tools") if isinstance(body.get("tools"), list) else []
    functions = body.get("functions") if isinstance(body.get("functions"), list) else []
    if tools or functions or body.get("tool_choice") not in (None, "", "none"):
        required.add("supports_tools")
    if functions:
        required.add("supports_function_calls")
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = str(tool.get("type") or "").strip().lower()
        if tool_type.startswith("web_search"):
            required.update({"supports_web_search", "supports_network"})
        elif tool_type == "function" or isinstance(tool.get("function"), dict):
            required.add("supports_function_calls")
        elif not tool_type and (tool.get("name") or tool.get("input_schema")):
            # Anthropic Messages function tools have no explicit type.
            required.add("supports_function_calls")
    if body.get("web_search_options") is not None:
        required.update(
            {"supports_tools", "supports_web_search", "supports_network"}
        )
    if body.get("parallel_tool_calls") is True:
        required.update(
            {
                "supports_tools",
                "supports_function_calls",
                "supports_parallel_tool_calls",
            }
        )

    response_format = body.get("response_format")
    responses_text = body.get("text")
    output_config = body.get("output_config")
    format_candidates = [
        response_format,
        responses_text.get("format") if isinstance(responses_text, dict) else None,
        output_config.get("format") if isinstance(output_config, dict) else None,
    ]
    if any(
        isinstance(candidate, dict)
        and str(candidate.get("type") or "").strip().lower() == "json_schema"
        for candidate in format_candidates
    ):
        required.add("supports_json_schema")

    for part in _iter_request_content_parts(path, body):
        part_type = str(part.get("type") or "").strip().lower()
        if part_type in {"image", "image_url", "input_image"}:
            required.add("supports_vision")
        elif part_type in {"audio", "input_audio"}:
            required.add("supports_audio_recognition")

    return tuple(
        capability
        for capability in MODEL_CAPABILITY_KEYS
        if capability in required
    )


def client_for_request(
    path: str,
    body: Json,
    *,
    strategy: str | None = None,
) -> Any:
    """Build a request-local client for the best matching model route.

    A profile explicitly configured for text-tool adaptation may substitute
    only tool-protocol capabilities. Native-only profiles and non-adaptable
    requirements never bypass the complete capability check.
    """
    from .gateway_proxy import NativeProxyClient

    required = required_capabilities_for_request(path, body)
    if not required:
        return NativeProxyClient()
    router = get_model_router()
    try:
        route, resolved_strategy, adapter_fallback = router.select_request_route(
            required,
            strategy=strategy,
        )
    except (NoModelCapabilityError, NoHealthyModelRouteError):
        # Ordinary conversation requests retain the legacy profile/adapter
        # path when the capability matrix is incomplete or its matching routes
        # are temporarily unavailable.  This is required for Gateway-owned and
        # downstream-owned tools, which may complete without a capability-
        # native upstream.  Recognition entry points remain strict.
        return NativeProxyClient()
    return NativeProxyClient(
        profile=route.profile,
        model=route.model,
        _allow_failover=False,
        _model_router=router,
        _model_route=route,
        _model_required_capabilities=required,
        _model_strategy=resolved_strategy,
        _model_adapter_fallback=adapter_fallback,
    )
