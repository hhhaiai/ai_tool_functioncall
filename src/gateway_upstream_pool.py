"""Thread-safe profile selection, health tracking, and failover for upstreams."""
from __future__ import annotations

import copy
import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

from .gateway_concurrency import _concurrency_config
from .gateway_errors import UpstreamHTTPError, UpstreamTimeoutError

Json = dict[str, Any]


@dataclass
class ProfileHealth:
    profile_id: str
    base_url: str
    active_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    consecutive_failures: int = 0
    response_time_seconds: float = 0.0
    unhealthy_until: float = 0.0
    last_error_type: str = ""


def _profile_id(profile: Json, index: int = 0) -> str:
    value = str(profile.get("id") or profile.get("name") or f"profile-{index}").strip()
    return value or f"profile-{index}"


def _routing_signature(profile: Json) -> str:
    raw_capabilities = profile.get("capabilities")
    capabilities: Json = raw_capabilities if isinstance(raw_capabilities, dict) else {}
    raw_paths = profile.get("paths")
    paths: Json = raw_paths if isinstance(raw_paths, dict) else {}
    payload = {
        "protocol": str(profile.get("protocol") or "openai_chat"),
        "tools_enabled": str(profile.get("tools_enabled") or "adapter"),
        "paths": {key: str(paths.get(key) or "") for key in sorted(paths)},
        "capabilities": {
            key: bool(capabilities.get(key))
            for key in (
                "supports_streaming",
                "supports_tools",
                "supports_function_calls",
                "supports_parallel_tool_calls",
                "supports_json_schema",
            )
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, UpstreamTimeoutError):
        return True
    if isinstance(exc, UpstreamHTTPError):
        return exc.upstream_status in {429, 502, 503, 504}
    return False


class UpstreamProfilePool:
    """Select compatible configured profiles and retain bounded health state."""

    def __init__(self, config: Json) -> None:
        raw_concurrency = config.get("concurrency")
        concurrency_raw: Json = raw_concurrency if isinstance(raw_concurrency, dict) else {}
        parsed = _concurrency_config(concurrency_raw)
        self.enabled = bool(concurrency_raw.get("multi_upstream_enabled", False))
        self.strategy = str(parsed.load_balance_strategy or "round_robin")
        try:
            self.failure_threshold = max(1, int(concurrency_raw.get("multi_upstream_failure_threshold") or 3))
        except (TypeError, ValueError):
            self.failure_threshold = 3
        try:
            self.recovery_seconds = max(0.1, float(concurrency_raw.get("multi_upstream_recovery_seconds") or 30.0))
        except (TypeError, ValueError):
            self.recovery_seconds = 30.0
        try:
            configured_max_attempts = int(concurrency_raw.get("multi_upstream_max_attempts") or 0)
        except (TypeError, ValueError):
            configured_max_attempts = 0

        configured_profiles = config.get("upstream_profiles")
        raw_profiles: list[Any] = configured_profiles if isinstance(configured_profiles, list) else []
        raw_active = config.get("upstream")
        active: Json = raw_active if isinstance(raw_active, dict) else {}
        active_id = str(config.get("active_upstream_id") or active.get("id") or "")
        profiles = [dict(item) for item in raw_profiles if isinstance(item, dict)]
        if not profiles and active:
            profiles = [dict(active)]
        active_profile = next(
            (item for index, item in enumerate(profiles) if _profile_id(item, index) == active_id),
            dict(active) if active else (profiles[0] if profiles else {}),
        )
        anchor_signature = _routing_signature(active_profile) if active_profile else ""

        included: list[Json] = []
        excluded: list[Json] = []
        for index, profile in enumerate(profiles):
            profile = copy.deepcopy(profile)
            profile["id"] = _profile_id(profile, index)
            reason = ""
            if not str(profile.get("base_url") or "").strip():
                reason = "missing_base_url"
            elif profile.get("enabled", True) is False or profile.get("load_balance_enabled", True) is False:
                reason = "disabled"
            elif self.enabled and anchor_signature and _routing_signature(profile) != anchor_signature:
                reason = "incompatible_routing_contract"
            elif not self.enabled and str(profile["id"]) != str(active_profile.get("id") or active_id):
                reason = "multi_upstream_disabled"
            if reason:
                excluded.append({
                    "id": profile["id"],
                    "base_url": str(profile.get("base_url") or ""),
                    "reason": reason,
                })
            else:
                included.append(profile)

        if active_profile and not included:
            fallback = copy.deepcopy(active_profile)
            fallback["id"] = _profile_id(fallback)
            included = [fallback]
        if active_id:
            included.sort(key=lambda item: 0 if str(item.get("id")) == active_id else 1)
        self.profiles = included
        self.excluded = excluded
        self.max_attempts = configured_max_attempts
        if self.max_attempts <= 0:
            self.max_attempts = len(included) if self.enabled else 1
        self.max_attempts = max(1, min(self.max_attempts, max(1, len(included))))
        self._health = {
            str(profile["id"]): ProfileHealth(
                profile_id=str(profile["id"]),
                base_url=str(profile.get("base_url") or ""),
            )
            for profile in included
        }
        self._index = 0
        self._lock = threading.RLock()

    def select_profile(self, *, exclude: set[str] | None = None) -> Json | None:
        exclude = set(exclude or set())
        with self._lock:
            now = time.monotonic()
            candidates = [
                profile
                for profile in self.profiles
                if str(profile.get("id")) not in exclude
                and self._health[str(profile.get("id"))].unhealthy_until <= now
            ]
            if not candidates:
                candidates = [profile for profile in self.profiles if str(profile.get("id")) not in exclude]
            if not candidates:
                return None
            if self.strategy == "least_connections":
                selected = min(
                    candidates,
                    key=lambda item: (
                        self._health[str(item.get("id"))].active_requests,
                        self._health[str(item.get("id"))].response_time_seconds,
                    ),
                )
            elif self.strategy == "random":
                selected = random.choice(candidates)
            else:
                selected = candidates[self._index % len(candidates)]
                self._index += 1
            return copy.deepcopy(selected)

    def failover_profiles(self, current_profile_id: str) -> list[Json]:
        selected: list[Json] = []
        excluded = {str(current_profile_id)}
        while len(selected) < max(0, self.max_attempts - 1):
            profile = self.select_profile(exclude=excluded)
            if profile is None:
                break
            profile_id = str(profile.get("id") or "")
            excluded.add(profile_id)
            selected.append(profile)
        return selected

    def request_start(self, profile_id: str) -> float:
        with self._lock:
            health = self._health.get(str(profile_id))
            if health is not None:
                health.active_requests += 1
        return time.monotonic()

    def request_success(self, profile_id: str, started: float) -> None:
        duration = max(0.0, time.monotonic() - started)
        with self._lock:
            health = self._health.get(str(profile_id))
            if health is None:
                return
            health.success_count += 1
            health.consecutive_failures = 0
            health.unhealthy_until = 0.0
            health.response_time_seconds = duration
            health.last_error_type = ""

    def request_failure(self, profile_id: str, exc: BaseException) -> None:
        with self._lock:
            health = self._health.get(str(profile_id))
            if health is None:
                return
            health.failure_count += 1
            health.last_error_type = exc.__class__.__name__
            if _retryable(exc):
                health.consecutive_failures += 1
                if health.consecutive_failures >= self.failure_threshold:
                    health.unhealthy_until = time.monotonic() + self.recovery_seconds
            else:
                # A valid 4xx proves the upstream is reachable; it should not
                # open the transport circuit or cause cross-provider retries.
                health.consecutive_failures = 0

    def request_end(self, profile_id: str) -> None:
        with self._lock:
            health = self._health.get(str(profile_id))
            if health is not None:
                health.active_requests = max(0, health.active_requests - 1)

    def snapshot(self) -> Json:
        with self._lock:
            now = time.monotonic()
            profiles = []
            for profile in self.profiles:
                profile_id = str(profile.get("id") or "")
                health = self._health[profile_id]
                profiles.append({
                    "id": profile_id,
                    "name": str(profile.get("name") or profile_id),
                    "base_url": str(profile.get("base_url") or ""),
                    "model": str(profile.get("model") or ""),
                    "protocol": str(profile.get("protocol") or "openai_chat"),
                    "healthy": health.unhealthy_until <= now,
                    "active_requests": health.active_requests,
                    "success_count": health.success_count,
                    "failure_count": health.failure_count,
                    "consecutive_failures": health.consecutive_failures,
                    "response_time_seconds": round(health.response_time_seconds, 6),
                    "recovery_in_seconds": round(max(0.0, health.unhealthy_until - now), 3),
                    "last_error_type": health.last_error_type,
                })
            return {
                "enabled": self.enabled,
                "strategy": self.strategy,
                "max_attempts": self.max_attempts,
                "profiles": profiles,
                "excluded_profiles": copy.deepcopy(self.excluded),
            }


_POOL_LOCK = threading.RLock()
_POOL: UpstreamProfilePool | None = None
_POOL_FINGERPRINT = ""


def _pool_fingerprint(config: Json) -> str:
    concurrency = config.get("concurrency") if isinstance(config.get("concurrency"), dict) else {}
    profiles = config.get("upstream_profiles") if isinstance(config.get("upstream_profiles"), list) else []
    payload = {
        "active": config.get("active_upstream_id"),
        "concurrency": concurrency,
        "profiles": profiles,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def get_upstream_pool() -> UpstreamProfilePool:
    from .gateway_config import load_config

    global _POOL, _POOL_FINGERPRINT
    config = load_config()
    fingerprint = _pool_fingerprint(config)
    with _POOL_LOCK:
        if _POOL is None or _POOL_FINGERPRINT != fingerprint:
            _POOL = UpstreamProfilePool(config)
            _POOL_FINGERPRINT = fingerprint
        return _POOL


def reset_upstream_pool() -> None:
    global _POOL, _POOL_FINGERPRINT
    with _POOL_LOCK:
        _POOL = None
        _POOL_FINGERPRINT = ""


def upstream_pool_snapshot() -> Json:
    return get_upstream_pool().snapshot()


__all__ = [
    "ProfileHealth",
    "UpstreamProfilePool",
    "get_upstream_pool",
    "reset_upstream_pool",
    "upstream_pool_snapshot",
]
