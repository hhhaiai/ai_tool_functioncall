from __future__ import annotations

import json

import pytest

from src import gateway_config, gateway_llm
from src.gateway_intelligence import IntelligenceConfig, _analyze_question


def _config(*, provider: str = "gateway_upstream", strict: bool = False) -> dict:
    return {
        "intelligence": {
            "enabled": True,
            "use_llm": True,
            "provider": provider,
            "strict_mode": strict,
            "llm_timeout": 2.0,
            "max_input_chars": 1000,
            "temperature": 0.0,
        },
        "upstream": {
            "base_url": "http://upstream.invalid",
            "model": "configured-model",
            "api_key": "must-not-appear",
        },
    }


class _FakeProxy:
    responses: list[dict] = []
    requests: list[tuple[str, dict]] = []

    def __init__(self) -> None:
        self.timeout = 60.0
        self.retry_max_elapsed = 90.0
        self.model = "profile-model"

    def forward(self, path: str, body: dict) -> dict:
        self.requests.append((path, body))
        if not self.responses:
            raise AssertionError("no fake provider response")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch: pytest.MonkeyPatch):
    from src import gateway_proxy

    _FakeProxy.responses = []
    _FakeProxy.requests = []
    gateway_llm.reset_provider_runtime()
    monkeypatch.setattr(gateway_proxy, "NativeProxyClient", _FakeProxy)
    monkeypatch.setattr(gateway_config, "load_config", lambda: _config())
    yield


def _chat(content: str) -> dict:
    return {
        "choices": [{
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }]
    }


def test_gateway_upstream_provider_analyzes_question_through_canonical_transport() -> None:
    _FakeProxy.responses.append(_chat(json.dumps({
        "complexity": "complex",
        "domain": "code",
        "requires_tools": True,
        "requires_context": False,
        "sub_questions": ["inspect files", "run tests"],
        "reflection_notes": ["use evidence"],
        "suggested_approach": "inspect before changing",
    })))

    result = gateway_llm.llm_analyze_question("review this repository")

    assert result["complexity"] == "complex"
    assert result["domain"] == "code"
    assert result["requires_tools"] is True
    path, body = _FakeProxy.requests[0]
    assert path == "/v1/chat/completions"
    assert body["model"] == "profile-model"
    assert body["stream"] is False
    assert body.get("tools") is None


def test_quality_and_reflection_accept_bounded_provider_output() -> None:
    _FakeProxy.responses.extend([
        _chat("```json\n{\"score\": 0.25, \"issues\": [\"short\"], \"suggestions\": [\"expand\"]}\n```"),
        _chat("A substantially improved and complete answer for the user."),
    ])

    quality = gateway_llm.llm_assess_quality("question", "answer")
    reflection = gateway_llm.llm_reflect("question", "answer", 300)

    assert quality == {"score": 0.25, "issues": ["short"], "suggestions": ["expand"]}
    assert reflection.startswith("A substantially improved")
    assert _FakeProxy.requests[1][1]["max_tokens"] == 300


def test_provider_failure_falls_back_to_rules_unless_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_text: str, _context: str = "") -> dict:
        raise gateway_llm.IntelligenceProviderError("provider unavailable")

    monkeypatch.setattr(gateway_llm, "llm_analyze_question", fail)
    fallback = _analyze_question(
        "如何检查代码并运行测试？",
        IntelligenceConfig(use_llm=True, strict_mode=False),
    )
    assert fallback.source == "rules"

    with pytest.raises(gateway_llm.IntelligenceProviderError):
        _analyze_question(
            "如何检查代码并运行测试？",
            IntelligenceConfig(use_llm=True, strict_mode=True),
        )


def test_unregistered_provider_has_structured_error_and_redacted_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(provider="missing", strict=True)
    monkeypatch.setattr(gateway_config, "load_config", lambda: config)

    with pytest.raises(gateway_llm.IntelligenceProviderError) as caught:
        gateway_llm.llm_analyze_question("question")

    assert caught.value.detail == {"provider": "missing"}
    status = gateway_llm.provider_status()
    assert status["provider_registered"] is False
    assert status["fallback"] == "disabled"
    assert status["runtime"]["failures"] == 1
    assert "must-not-appear" not in json.dumps(status)


def test_custom_provider_registration_is_pluggable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CustomProvider:
        def complete(self, **_kwargs) -> str:
            return json.dumps({
                "complexity": "simple",
                "domain": "general",
                "requires_tools": False,
                "requires_context": False,
                "sub_questions": [],
                "reflection_notes": [],
                "suggested_approach": "answer directly",
            })

    gateway_llm.register_llm_provider("custom", CustomProvider())
    monkeypatch.setattr(gateway_config, "load_config", lambda: _config(provider="custom"))
    try:
        result = gateway_llm.llm_analyze_question("hello")
        assert result["complexity"] == "simple"
        assert gateway_llm.provider_status()["provider_registered"] is True
    finally:
        gateway_llm.unregister_llm_provider("custom")
