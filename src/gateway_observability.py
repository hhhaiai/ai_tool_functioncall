"""Bounded-cardinality metrics and privacy-safe in-process trace spans."""
from __future__ import annotations

import contextvars
import re
import threading
import time
import uuid
from collections import deque
from typing import Any

Json = dict[str, Any]

_REQUEST_ID = contextvars.ContextVar("gateway_request_id", default="")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)
_PUBLIC_ROUTES = {
    "/",
    "/healthz",
    "/livez",
    "/readyz",
    "/capabilities",
    "/client-config",
    "/client-config.json",
    "/v1/models",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/messages",
    "/v1/assistants",
    "/v1/threads",
    "/v1/tools/call",
    "/tools/call",
}
_FAILURE_TYPES = {
    "none",
    "timeout",
    "cancelled",
    "permission_denied",
    "tool_not_found",
    "execution_failed",
    "process_exit",
    "sandbox_setup_failed",
    "conflict",
    "connector_required",
    "upstream_http",
    "upstream_timeout",
    "auth",
    "busy",
    "unavailable",
    "client_disconnect",
    "internal",
    "other",
}


def normalized_request_id(value: Any = None) -> str:
    candidate = str(value or "").strip()
    return candidate if _REQUEST_ID_RE.fullmatch(candidate) else f"req_{uuid.uuid4().hex}"


def current_request_id() -> str:
    return str(_REQUEST_ID.get() or "")


def begin_request(value: Any = None):
    request_id = normalized_request_id(value)
    return request_id, _REQUEST_ID.set(request_id)


def end_request(token: contextvars.Token) -> None:
    _REQUEST_ID.reset(token)


def normalize_route(path: Any) -> str:
    text = str(path or "").split("?", 1)[0]
    if text in _PUBLIC_ROUTES:
        return text
    if text.startswith("/admin/") or text in {"/admin", "/ui", "/config"}:
        return "/admin/*"
    if text.startswith("/v1/"):
        return "/v1/*"
    return "/other"


def normalize_protocol(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"openai_chat", "openai_responses", "anthropic_messages"} else "other"


def normalize_failure(value: Any) -> str:
    text = _SAFE_NAME_RE.sub("_", str(value or "none").strip().lower())[:64] or "none"
    if text in _FAILURE_TYPES:
        return text
    if "timeout" in text:
        return "timeout"
    if "upstream" in text and "http" in text:
        return "upstream_http"
    if "cancel" in text or "disconnect" in text:
        return "cancelled"
    if "permission" in text or "auth" in text:
        return "permission_denied"
    if "busy" in text or "limit" in text:
        return "busy"
    if "unavailable" in text or "connection" in text:
        return "unavailable"
    return "other"


def normalize_tool_label(name: Any, tool_class: str) -> str:
    category = str(tool_class or "unknown")
    if category != "builtin":
        return category if category in {"mcp", "http_action", "unknown"} else "unknown"
    text = _SAFE_NAME_RE.sub("_", str(name or "unknown"))[:80]
    return text or "unknown"


def _escape_label(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class ObservabilityRegistry:
    def __init__(self, *, trace_limit: int = 1000) -> None:
        self._lock = threading.RLock()
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], Json] = {}
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._traces: deque[Json] = deque(maxlen=max(10, int(trace_limit)))

    @staticmethod
    def _labels(labels: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(key), str(value)) for key, value in labels.items()))

    def observe(self, metric: str, seconds: float, **labels: Any) -> None:
        key = (metric, self._labels(labels))
        value = max(0.0, float(seconds))
        with self._lock:
            state = self._histograms.setdefault(
                key,
                {"count": 0, "sum": 0.0, "buckets": [0 for _ in _BUCKETS]},
            )
            state["count"] += 1
            state["sum"] += value
            for index, boundary in enumerate(_BUCKETS):
                if value <= boundary:
                    state["buckets"][index] += 1

    def increment(self, metric: str, *, amount: int = 1, **labels: Any) -> None:
        key = (metric, self._labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + int(amount)

    def trace(
        self,
        span: str,
        *,
        name: str,
        duration_seconds: float,
        outcome: str,
        failure_type: str = "none",
        attributes: Json | None = None,
    ) -> None:
        safe_attributes: Json = {}
        for key, value in (attributes or {}).items():
            if key not in {"method", "route", "protocol", "stream", "status", "first_event_ms", "event_count", "tool_class"}:
                continue
            if isinstance(value, (int, float, bool)):
                safe_attributes[key] = value
            else:
                safe_attributes[key] = _SAFE_NAME_RE.sub("_", str(value))[:80]
        with self._lock:
            self._traces.append({
                "ts": time.time(),
                "request_id": current_request_id(),
                "span": _SAFE_NAME_RE.sub("_", str(span))[:64],
                "name": _SAFE_NAME_RE.sub("_", str(name))[:120],
                "duration_ms": round(max(0.0, float(duration_seconds)) * 1000, 3),
                "outcome": "success" if outcome == "success" else "failure",
                "failure_type": normalize_failure(failure_type),
                "attributes": safe_attributes,
            })

    def traces(self, limit: int = 100) -> list[Json]:
        bounded = max(1, min(int(limit), 1000))
        with self._lock:
            return list(self._traces)[-bounded:][::-1]

    def prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            histograms = list(self._histograms.items())
            counters = list(self._counters.items())
        declared: set[str] = set()
        for (metric, labels), state in sorted(histograms):
            if metric not in declared:
                lines.extend([f"# TYPE {metric} histogram"])
                declared.add(metric)
            base = dict(labels)
            for boundary, count in zip(_BUCKETS, state["buckets"]):
                rendered = {**base, "le": f"{boundary:g}"}
                label_text = ",".join(f'{key}="{_escape_label(value)}"' for key, value in sorted(rendered.items()))
                lines.append(f"{metric}_bucket{{{label_text}}} {int(count)}")
            inf = {**base, "le": "+Inf"}
            inf_text = ",".join(f'{key}="{_escape_label(value)}"' for key, value in sorted(inf.items()))
            base_text = ",".join(f'{key}="{_escape_label(value)}"' for key, value in labels)
            lines.append(f"{metric}_bucket{{{inf_text}}} {int(state['count'])}")
            lines.append(f"{metric}_count{{{base_text}}} {int(state['count'])}")
            lines.append(f"{metric}_sum{{{base_text}}} {float(state['sum']):.9f}")
        for (metric, labels), value in sorted(counters):
            if metric not in declared:
                lines.append(f"# TYPE {metric} counter")
                declared.add(metric)
            label_text = ",".join(f'{key}="{_escape_label(item)}"' for key, item in labels)
            lines.append(f"{metric}{{{label_text}}} {int(value)}")
        return "\n".join(lines) + ("\n" if lines else "")

    def clear(self) -> None:
        with self._lock:
            self._histograms.clear()
            self._counters.clear()
            self._traces.clear()


OBSERVABILITY = ObservabilityRegistry()


def observe_request(method: Any, path: Any, status: int, duration_seconds: float) -> None:
    route = normalize_route(path)
    status_class = f"{int(status) // 100}xx" if int(status or 0) >= 100 else "none"
    status_value = int(status or 0)
    outcome = "success" if 200 <= status_value < 400 else "failure"
    if outcome == "success":
        failure_type = "none"
    elif status_value in {401, 403}:
        failure_type = "auth"
    elif status_value == 429:
        failure_type = "busy"
    elif status_value == 503:
        failure_type = "unavailable"
    else:
        failure_type = "internal" if status_value >= 500 or status_value == 0 else "other"
    labels = {"method": str(method or "UNKNOWN").upper()[:10], "route": route, "status_class": status_class}
    OBSERVABILITY.observe("gateway_http_request_duration_seconds", duration_seconds, **labels)
    OBSERVABILITY.increment(
        "gateway_http_requests_observed_total",
        method=labels["method"],
        route=labels["route"],
        status_class=labels["status_class"],
    )
    OBSERVABILITY.trace(
        "http_request",
        name=route,
        duration_seconds=duration_seconds,
        outcome=outcome,
        failure_type=failure_type,
        attributes={"method": labels["method"], "route": route, "status": int(status or 0)},
    )


def observe_tool(
    name: Any,
    *,
    tool_class: str,
    success: bool,
    failure_type: Any,
    duration_seconds: float,
) -> None:
    label = normalize_tool_label(name, tool_class)
    failure = "none" if success else normalize_failure(failure_type)
    labels = {"tool": label, "tool_class": tool_class, "outcome": "success" if success else "failure", "failure_type": failure}
    OBSERVABILITY.observe("gateway_tool_duration_seconds", duration_seconds, **labels)
    OBSERVABILITY.increment(
        "gateway_tool_calls_observed_total",
        tool=labels["tool"],
        tool_class=labels["tool_class"],
        outcome=labels["outcome"],
        failure_type=labels["failure_type"],
    )
    OBSERVABILITY.trace(
        "tool",
        name=label,
        duration_seconds=duration_seconds,
        outcome="success" if success else "failure",
        failure_type=failure,
        attributes={"tool_class": tool_class},
    )


def observe_upstream(
    *,
    method: str,
    path: Any,
    protocol: Any,
    stream: bool,
    success: bool,
    failure_type: Any,
    duration_seconds: float,
    first_event_seconds: float | None = None,
    event_count: int = 0,
) -> None:
    route = normalize_route(path)
    failure = "none" if success else normalize_failure(failure_type)
    labels = {
        "method": str(method or "POST").upper()[:10],
        "route": route,
        "protocol": normalize_protocol(protocol),
        "stream": "true" if stream else "false",
        "outcome": "success" if success else "failure",
        "failure_type": failure,
    }
    OBSERVABILITY.observe("gateway_upstream_duration_seconds", duration_seconds, **labels)
    OBSERVABILITY.increment(
        "gateway_upstream_requests_observed_total",
        method=labels["method"],
        route=labels["route"],
        protocol=labels["protocol"],
        stream=labels["stream"],
        outcome=labels["outcome"],
        failure_type=labels["failure_type"],
    )
    if first_event_seconds is not None:
        OBSERVABILITY.observe(
            "gateway_upstream_first_event_seconds",
            first_event_seconds,
            route=route,
            protocol=labels["protocol"],
            outcome=labels["outcome"],
        )
    OBSERVABILITY.trace(
        "upstream",
        name=route,
        duration_seconds=duration_seconds,
        outcome="success" if success else "failure",
        failure_type=failure,
        attributes={
            "method": labels["method"],
            "route": route,
            "protocol": labels["protocol"],
            "stream": bool(stream),
            "first_event_ms": round(first_event_seconds * 1000, 3) if first_event_seconds is not None else -1,
            "event_count": int(event_count),
        },
    )


__all__ = [
    "OBSERVABILITY",
    "ObservabilityRegistry",
    "begin_request",
    "current_request_id",
    "end_request",
    "normalize_failure",
    "normalize_route",
    "observe_request",
    "observe_tool",
    "observe_upstream",
]
