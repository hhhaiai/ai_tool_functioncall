"""Pluggable LLM provider used by Gateway intelligence features.

The built-in provider deliberately reuses :class:`NativeProxyClient`.  It
therefore inherits the Gateway's upstream profile selection, credential
handling, protocol conversion, retry/failover limits, observability, and
response-size bounds instead of creating a second outbound HTTP stack.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .gateway_errors import GatewayUnavailableError

Json = dict[str, Any]


class IntelligenceProviderError(GatewayUnavailableError):
    """Raised when an explicitly required intelligence provider is unusable."""


@dataclass(frozen=True)
class ProviderSettings:
    provider: str = "gateway_upstream"
    model: str = ""
    timeout_seconds: float = 15.0
    max_input_chars: int = 20_000
    temperature: float = 0.0
    strict_mode: bool = False


class LLMProvider(Protocol):
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        settings: ProviderSettings,
    ) -> str: ...


class GatewayUpstreamProvider:
    """Call the configured upstream through the canonical Gateway transport."""

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        settings: ProviderSettings,
    ) -> str:
        from .gateway_proxy import NativeProxyClient

        client = NativeProxyClient()
        client.timeout = min(client.timeout, settings.timeout_seconds)
        client.retry_max_elapsed = min(client.retry_max_elapsed, settings.timeout_seconds)
        model = settings.model or client.model
        body: Json = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "temperature": settings.temperature,
            "max_tokens": max(1, int(max_tokens)),
        }
        response = client.forward("/v1/chat/completions", body)
        text = _response_text(response)
        if not text.strip():
            raise IntelligenceProviderError(
                "intelligence provider returned an empty response",
                detail={"provider": settings.provider},
            )
        return text.strip()


_PROVIDER_LOCK = threading.RLock()
_PROVIDERS: dict[str, LLMProvider] = {"gateway_upstream": GatewayUpstreamProvider()}
_RUNTIME: Json = {
    "calls": 0,
    "successes": 0,
    "failures": 0,
    "last_success_at": None,
    "last_failure_at": None,
    "last_error_type": "",
}


def register_llm_provider(name: str, provider: LLMProvider) -> None:
    """Register a process-local provider implementation by stable name."""
    normalized = str(name or "").strip().lower()
    if not normalized:
        raise ValueError("provider name is required")
    if not callable(getattr(provider, "complete", None)):
        raise TypeError("provider must implement complete()")
    with _PROVIDER_LOCK:
        _PROVIDERS[normalized] = provider


def unregister_llm_provider(name: str) -> None:
    """Remove a custom provider; the built-in Gateway provider is permanent."""
    normalized = str(name or "").strip().lower()
    if normalized == "gateway_upstream":
        raise ValueError("the gateway_upstream provider cannot be removed")
    with _PROVIDER_LOCK:
        _PROVIDERS.pop(normalized, None)


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def provider_settings(raw: Json | None = None) -> ProviderSettings:
    if raw is None:
        from .gateway_config import load_config

        loaded = load_config()
        raw_value = loaded.get("intelligence")
        raw = raw_value if isinstance(raw_value, dict) else {}
    return ProviderSettings(
        provider=str(raw.get("provider") or "gateway_upstream").strip().lower(),
        model=str(raw.get("model") or "").strip(),
        timeout_seconds=_bounded_float(raw.get("llm_timeout"), 15.0, 0.1, 300.0),
        max_input_chars=_bounded_int(raw.get("max_input_chars"), 20_000, 256, 200_000),
        temperature=_bounded_float(raw.get("temperature"), 0.0, 0.0, 2.0),
        strict_mode=bool(raw.get("strict_mode", False)),
    )


def _response_text(response: Json) -> str:
    choices = response.get("choices") if isinstance(response.get("choices"), list) else []
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        parts.append(str(item["text"]))
                return "".join(parts)
        if isinstance(choices[0].get("text"), str):
            return str(choices[0]["text"])
    if isinstance(response.get("output_text"), str):
        return str(response["output_text"])
    if isinstance(response.get("text"), str):
        return str(response["text"])
    return ""


def _provider_complete(
    *,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
) -> str:
    settings = provider_settings()
    bounded_user = str(user_prompt or "")[: settings.max_input_chars]
    with _PROVIDER_LOCK:
        provider = _PROVIDERS.get(settings.provider)
        _RUNTIME["calls"] = int(_RUNTIME.get("calls") or 0) + 1
    if provider is None:
        exc = IntelligenceProviderError(
            "configured intelligence provider is not registered",
            detail={"provider": settings.provider},
        )
        _record_failure(exc)
        raise exc
    try:
        result = provider.complete(
            system_prompt=system_prompt,
            user_prompt=bounded_user,
            max_tokens=max_tokens,
            settings=settings,
        )
    except IntelligenceProviderError as exc:
        _record_failure(exc)
        raise
    except Exception as exc:
        wrapped = IntelligenceProviderError(
            "intelligence provider request failed",
            detail={"provider": settings.provider, "failure_type": exc.__class__.__name__},
        )
        _record_failure(wrapped)
        raise wrapped from exc
    with _PROVIDER_LOCK:
        _RUNTIME["successes"] = int(_RUNTIME.get("successes") or 0) + 1
        _RUNTIME["last_success_at"] = time.time()
        _RUNTIME["last_error_type"] = ""
    return result


def _record_failure(exc: BaseException) -> None:
    with _PROVIDER_LOCK:
        _RUNTIME["failures"] = int(_RUNTIME.get("failures") or 0) + 1
        _RUNTIME["last_failure_at"] = time.time()
        _RUNTIME["last_error_type"] = exc.__class__.__name__


def _json_object(text: str) -> Json:
    candidate = str(text or "").strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise IntelligenceProviderError("intelligence provider returned invalid JSON")
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise IntelligenceProviderError("intelligence provider returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise IntelligenceProviderError("intelligence provider JSON must be an object")
    return value


def _string_list(value: Any, *, maximum: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:1000] for item in value[:maximum] if str(item).strip()]


def llm_analyze_question(text: str, context: str = "") -> Json:
    prompt = (
        "Question:\n"
        + str(text or "")
        + ("\n\nRecent conversation context:\n" + str(context or "") if context else "")
    )
    result = _json_object(
        _provider_complete(
            system_prompt=(
                "Analyze the question and return only a JSON object with keys: "
                "complexity (simple|moderate|complex), domain "
                "(code|math|general|creative|factual), requires_tools (boolean), "
                "requires_context (boolean), sub_questions (array of strings), "
                "reflection_notes (array of strings), suggested_approach (string)."
            ),
            user_prompt=prompt,
            max_tokens=700,
        )
    )
    complexity = str(result.get("complexity") or "moderate").lower()
    domain = str(result.get("domain") or "general").lower()
    if complexity not in {"simple", "moderate", "complex"}:
        raise IntelligenceProviderError("intelligence provider returned invalid complexity")
    if domain not in {"code", "math", "general", "creative", "factual"}:
        domain = "general"
    return {
        "complexity": complexity,
        "domain": domain,
        "requires_tools": bool(result.get("requires_tools", False)),
        "requires_context": bool(result.get("requires_context", False)),
        "sub_questions": _string_list(result.get("sub_questions")),
        "reflection_notes": _string_list(result.get("reflection_notes")),
        "suggested_approach": str(result.get("suggested_approach") or "")[:2000],
    }


def llm_assess_quality(question: str, answer: str) -> Json:
    result = _json_object(
        _provider_complete(
            system_prompt=(
                "Assess the answer and return only a JSON object with keys: "
                "score (number 0 through 1), issues (array of strings), and "
                "suggestions (array of strings)."
            ),
            user_prompt=f"Question:\n{question}\n\nAnswer:\n{answer}",
            max_tokens=500,
        )
    )
    raw_score = result.get("score")
    if not isinstance(raw_score, (str, int, float)):
        raise IntelligenceProviderError("intelligence provider returned invalid quality score")
    try:
        score = float(raw_score)
    except (TypeError, ValueError, OverflowError) as exc:
        raise IntelligenceProviderError("intelligence provider returned invalid quality score") from exc
    return {
        "score": max(0.0, min(1.0, score)),
        "issues": _string_list(result.get("issues")),
        "suggestions": _string_list(result.get("suggestions")),
    }


def llm_reflect(question: str, answer: str, max_tokens: int = 500) -> str:
    return _provider_complete(
        system_prompt=(
            "Rewrite the answer to be accurate, complete, relevant, and clear. "
            "Return only the improved user-facing answer; do not describe the review process."
        ),
        user_prompt=f"Question:\n{question}\n\nCurrent answer:\n{answer}",
        max_tokens=max(1, min(int(max_tokens), 8192)),
    ).strip()


def provider_status() -> Json:
    from .gateway_config import load_config

    config = load_config()
    raw = config.get("intelligence") if isinstance(config.get("intelligence"), dict) else {}
    settings = provider_settings(raw)
    upstream = config.get("upstream") if isinstance(config.get("upstream"), dict) else {}
    with _PROVIDER_LOCK:
        runtime = dict(_RUNTIME)
        registered = sorted(_PROVIDERS)
    enabled = bool(raw.get("enabled", True))
    use_llm = bool(raw.get("use_llm", False))
    return {
        "enabled": enabled,
        "use_llm": use_llm,
        "mode": "llm" if enabled and use_llm else "rules",
        "provider": settings.provider,
        "provider_registered": settings.provider in registered,
        "registered_providers": registered,
        "strict_mode": settings.strict_mode,
        "model": settings.model or str(upstream.get("model") or ""),
        "timeout_seconds": settings.timeout_seconds,
        "max_input_chars": settings.max_input_chars,
        "upstream_configured": bool(str(upstream.get("base_url") or "").strip()),
        "fallback": "disabled" if settings.strict_mode else "rules",
        "runtime": runtime,
    }


def reset_provider_runtime() -> None:
    with _PROVIDER_LOCK:
        _RUNTIME.update({
            "calls": 0,
            "successes": 0,
            "failures": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_error_type": "",
        })


__all__ = [
    "GatewayUpstreamProvider",
    "IntelligenceProviderError",
    "LLMProvider",
    "ProviderSettings",
    "llm_analyze_question",
    "llm_assess_quality",
    "llm_reflect",
    "provider_settings",
    "provider_status",
    "register_llm_provider",
    "reset_provider_runtime",
    "unregister_llm_provider",
]
