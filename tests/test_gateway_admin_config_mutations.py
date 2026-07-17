from __future__ import annotations

import copy
from typing import Any

import pytest

from src import gateway_admin_config_mutations as mutations
from src import gateway_config


def _upstream() -> dict[str, Any]:
    return {
        "id": "default",
        "name": "default",
        "base_url": "https://api.example.com/v1",
        "api_key": "existing-secret",
        "model": "existing-model",
        "protocol": "anthropic_messages",
        "tools_enabled": "adapter",
        "timeout_seconds": 12.5,
        "max_input_tokens": 1000,
        "max_output_tokens": 100,
        "max_concurrency": 4,
        "paths": {
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "responses": "/v1/responses",
            "messages": "/v1/messages",
        },
        "capabilities": {
            "supports_streaming": True,
            "supports_tools": False,
            "supports_function_calls": False,
            "supports_parallel_tool_calls": False,
            "supports_vision": False,
            "supports_network": False,
            "supports_web_search": False,
            "supports_json_schema": True,
        },
    }


def _base_config() -> dict[str, Any]:
    upstream = _upstream()
    return {
        "upstream": copy.deepcopy(upstream),
        "upstream_profiles": [copy.deepcopy(upstream)],
        "active_upstream_id": "default",
        "active_upstream": "default",
        "gateway": {
            "tool_mode": "orchestrate",
            "max_tool_rounds": 9,
            "max_concurrent_requests": 11,
            "text_tool_adapter_compact_token_limit": 7777,
            "concurrency_queue_timeout_seconds": 2.5,
            "tool_execution_timeout_seconds": 33.5,
            "allow_write_tools": False,
            "allow_shell_tools": False,
            "request_logging": False,
            "record_unsupported_tools": False,
            "text_tool_call_fallback_enabled": False,
            "cors_enabled": False,
            "cors_allowed_origins": [],
        },
        "context": {
            "enabled": False,
            "fanout_enabled": False,
            "quality_review_enabled": False,
            "max_input_tokens": 34567,
            "fanout_chunk_tokens": 4567,
            "fanout_max_chunks": 3,
            "fanout_max_workers": 2,
        },
    }


def _capture_saves(monkeypatch: pytest.MonkeyPatch):
    saved: list[tuple[dict[str, Any], str | None]] = []
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda config, *, expected_revision=None: saved.append(
            (copy.deepcopy(config), expected_revision)
        ),
    )
    return saved


def _valid_form() -> dict[str, str]:
    return {
        "profile_id": "primary",
        "profile_name": "Primary",
        "base_url": "https://gateway-upstream.example.com/api",
        "model": "new-model",
        "protocol": "openai_chat",
        "tools_enabled": "adapter",
        "upstream_timeout_seconds": "45",
        "upstream_max_input_tokens": "200000",
        "upstream_max_output_tokens": "16000",
        "upstream_max_concurrency": "64",
        "path_models": "/openai/models",
        "path_chat_completions": "/openai/chat",
        "path_responses": "/openai/responses",
        "path_messages": "/anthropic/messages",
        "tool_mode": "orchestrate",
        "max_tool_rounds": "7",
        "max_concurrent_requests": "48",
        "concurrency_queue_timeout_seconds": "3",
        "tool_execution_timeout_seconds": "90",
        "text_tool_adapter_compact_token_limit": "10000",
        "allow_write_tools": "1",
        "allow_shell_tools": "1",
        "request_logging": "1",
        "record_unsupported_tools": "1",
        "text_tool_call_fallback_enabled": "1",
        "cors_enabled": "1",
        "cors_allowed_origins": "https://console.example.com, https://console.example.com:443",
        "context_enabled": "1",
        "context_fanout_enabled": "1",
        "context_quality_review_enabled": "1",
        "context_max_input_tokens": "8000",
        "context_fanout_chunk_tokens": "6000",
        "context_fanout_max_chunks": "0",
        "context_fanout_max_workers": "6",
    }


def test_unmatched_config_path_has_no_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_config_mutation(
        "/admin/client-config", config, "revision", {}
    )
    assert result.matched is False
    assert config == original
    assert saved == []


def test_full_admin_config_form_is_atomic_normalized_and_revision_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_config_mutation(
        "/admin/config", config, "revision-1", _valid_form()
    )
    assert result.success is True
    assert config == original
    assert len(saved) == 1
    candidate, revision = saved[0]
    assert revision == "revision-1"
    assert candidate["active_upstream_id"] == "primary"
    assert candidate["active_upstream"] == "primary"
    assert candidate["upstream"]["id"] == "primary"
    assert any(item["id"] == "primary" for item in candidate["upstream_profiles"])
    assert candidate["gateway"]["max_concurrent_requests"] == 48
    assert candidate["gateway"]["cors_allowed_origins"] == ["https://console.example.com"]
    assert candidate["context"]["fanout_max_chunks"] == 0
    assert candidate["context"]["fanout_max_workers"] == 6


def test_omitted_numeric_fields_preserve_existing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_config_mutation(
        "/admin/config",
        _base_config(),
        "revision",
        {
            "base_url": "https://after.example.com",
            "model": "after-model",
            "protocol": "anthropic_messages",
            "tool_mode": "orchestrate",
        },
    )
    assert result.success is True
    candidate = saved[0][0]
    assert candidate["upstream"]["timeout_seconds"] == 12.5
    assert candidate["upstream"]["max_input_tokens"] == 1000
    assert candidate["upstream"]["max_output_tokens"] == 100
    assert candidate["gateway"]["max_tool_rounds"] == 9
    assert candidate["gateway"]["max_concurrent_requests"] == 11
    assert candidate["context"]["max_input_tokens"] == 34567
    assert candidate["context"]["fanout_chunk_tokens"] == 4567


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"base_url": "ftp://example.com"}, "invalid upstream base_url"),
        ({"base_url": "https://user:pass@example.com"}, "invalid upstream base_url"),
        ({"base_url": "https://example.com/api?token=x"}, "invalid upstream base_url"),
        ({"protocol": "invalid"}, "invalid upstream protocol"),
        ({"upstream_timeout_seconds": "0"}, "invalid numeric field: timeout_seconds"),
        (
            {"upstream_max_input_tokens": "100", "upstream_max_output_tokens": "101"},
            "must not exceed upstream_max_input_tokens",
        ),
        ({"path_models": "https://evil.example/models"}, "invalid upstream path: models"),
        ({"path_models": "/v1/../secret"}, "invalid upstream path: models"),
        ({"path_messages": "/messages?secret=x"}, "invalid upstream path: messages"),
        ({"tool_mode": "unknown"}, "invalid gateway tool_mode"),
        ({"max_tool_rounds": "0"}, "invalid numeric field: max_tool_rounds"),
        ({"max_tool_rounds": "101"}, "invalid numeric field: max_tool_rounds"),
        ({"max_concurrent_requests": "10001"}, "invalid numeric field: max_concurrent_requests"),
        ({"concurrency_queue_timeout_seconds": "-1"}, "invalid numeric field: concurrency_queue_timeout_seconds"),
        ({"tool_execution_timeout_seconds": "0"}, "invalid numeric field: tool_execution_timeout_seconds"),
        ({"context_fanout_max_chunks": "-1"}, "invalid numeric field: context_fanout_max_chunks"),
        ({"context_fanout_max_workers": "257"}, "invalid numeric field: context_fanout_max_workers"),
        (
            {"context_max_input_tokens": "100", "context_fanout_chunk_tokens": "101"},
            "must not exceed context_max_input_tokens",
        ),
        ({"cors_allowed_origins": "https://*.example.com"}, "invalid CORS origin"),
    ],
)
def test_invalid_admin_config_never_saves_or_mutates_source(
    changes: dict[str, str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    form = _valid_form()
    form.update(changes)
    config = _base_config()
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_config_mutation(
        "/admin/config", config, "revision", form
    )
    assert result.status == 400
    assert message in result.error
    assert config == original
    assert saved == []


def test_malformed_and_duplicate_profile_collections_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for profiles, message in [
        ("broken", "invalid upstream profile configuration"),
        ([_upstream(), _upstream()], "duplicate upstream profile id"),
    ]:
        config = _base_config()
        config["upstream_profiles"] = profiles
        saved = _capture_saves(monkeypatch)
        result = mutations.apply_admin_config_mutation(
            "/admin/config", config, "revision", _valid_form()
        )
        assert message in result.error
        assert saved == []


@pytest.mark.parametrize("tool_mode", ["orchestrate", "passthrough", "native_passthrough", "proxy"])
def test_supported_gateway_tool_modes_are_preserved(
    tool_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    form = _valid_form()
    form["tool_mode"] = tool_mode
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_config_mutation(
        "/admin/config", _base_config(), "revision", form
    )
    assert result.success is True
    assert saved[0][0]["gateway"]["tool_mode"] == tool_mode


def test_save_conflict_propagates_without_mutating_loaded_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("conflict marker")),
    )
    with pytest.raises(RuntimeError, match="conflict marker"):
        mutations.apply_admin_config_mutation(
            "/admin/config", config, "stale", _valid_form()
        )
    assert config == original

