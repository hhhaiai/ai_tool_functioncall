#!/usr/bin/env python3
"""Capability-aware, health-aware upstream model routing."""
from __future__ import annotations

import copy
import random
import threading
import time
from typing import Any, Callable

from .gateway_config import MODEL_CAPABILITY_KEYS, flatten_profile_models, model_capability_snapshot
from .gateway_errors import ConfigError, UpstreamHTTPError, UpstreamTimeoutError

Json = dict[str, Any]
_RETRYABLE_STATUS_CODES = {429, 502, 503, 504}


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

    def _select_from_snapshot(
        self,
        config: Json,
        capability: str,
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
                if not caps.get(capability):
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

        selected = self._pick(capability, candidates, strategy)
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
