"""Consistent upstream tools_enabled semantics across config and runtime."""
from __future__ import annotations

import copy
from typing import Any

import pytest

from src.gateway_config import (
    _default_config,
    _normalize_upstream_profile,
    _profile_from_admin_form,
    canonical_tools_enabled,
    tools_enabled_is_disabled,
)
from src.gateway_model_router import ModelRouter


@pytest.mark.parametrize(
    "raw",
    ["off", "disabled", "false", "0", "none", " OFF ", "Disabled", False, 0],
)
def test_disabled_aliases_have_one_canonical_value(raw: Any) -> None:
    assert canonical_tools_enabled(raw) == "off"
    assert tools_enabled_is_disabled(raw) is True


@pytest.mark.parametrize("raw", [None, "", "adapter", " ADAPTER "])
def test_empty_and_adapter_values_keep_adapter_default(raw: Any) -> None:
    assert canonical_tools_enabled(raw) == "adapter"
    assert tools_enabled_is_disabled(raw) is False


@pytest.mark.parametrize("raw", ["off", "disabled", "false", "0", "none"])
def test_profile_load_and_admin_form_persist_disabled_aliases_as_off(raw: str) -> None:
    existing = copy.deepcopy(_default_config()["upstream"])
    existing.update(
        {
            "id": "primary",
            "name": "primary",
            "base_url": "https://primary.example.test",
            "model": "model-a",
            "tools_enabled": raw,
        }
    )

    loaded = _normalize_upstream_profile(existing)
    saved = _profile_from_admin_form({"tools_enabled": raw}, existing)

    assert loaded["tools_enabled"] == "off"
    assert saved["tools_enabled"] == "off"


@pytest.mark.parametrize("raw", ["off", "disabled", "false", "0", "none"])
def test_runtime_disables_tool_schema_adapter_and_intent_paths(raw: str) -> None:
    from src.gateway_streaming import _merge_builtin_tools, _tools_enabled_for_upstream
    from src.gateway_tool_runtime import (
        _REQUEST_UPSTREAM_CONFIG,
        _text_tool_call_fallback_enabled,
        _weak_upstream_text_tools_active,
    )

    token = _REQUEST_UPSTREAM_CONFIG.set(
        {
            "model": "model-a",
            "protocol": "openai_chat",
            "tools_enabled": raw,
            "max_input_tokens": 128000,
            "capabilities": {
                "supports_tools": True,
                "supports_function_calls": True,
            },
        }
    )
    try:
        merged = _merge_builtin_tools(
            "/v1/chat/completions",
            {
                "messages": [{"role": "user", "content": "use lookup"}],
                "tools": [
                    {"type": "function", "function": {"name": "lookup"}}
                ],
                "tool_choice": "required",
            },
        )

        assert _tools_enabled_for_upstream() == "off"
        assert _text_tool_call_fallback_enabled() is False
        assert _weak_upstream_text_tools_active("orchestrate") is False
        assert "tools" not in merged
        assert "tool_choice" not in merged
    finally:
        _REQUEST_UPSTREAM_CONFIG.reset(token)


@pytest.mark.parametrize("raw", ["off", "disabled", "false", "0", "none"])
def test_disabled_profiles_cannot_be_selected_as_text_tool_adapters(raw: str) -> None:
    profile = copy.deepcopy(_default_config()["upstream"])
    profile.update(
        {
            "id": "disabled-tools",
            "name": "disabled-tools",
            "base_url": "https://disabled.example.test",
            "model": "text-only",
            "tools_enabled": raw,
            "capabilities": {
                key: False for key in profile["capabilities"]
            },
            "models": [{"name": "text-only", "capabilities": {}}],
        }
    )
    config = _default_config()
    config["upstream"] = copy.deepcopy(profile)
    config["upstream_profiles"] = [profile]
    config["active_upstream_id"] = "disabled-tools"
    router = ModelRouter(lambda: config)

    route = router.select_adapter_for_capabilities(
        ("supports_tools", "supports_function_calls")
    )

    assert route is None


@pytest.mark.parametrize("raw", ["off", "disabled", "false", "0", "none"])
def test_config_center_warns_and_selects_canonical_disabled_mode(raw: str) -> None:
    from src.gateway_web_config import (
        _render_model_capability_matrix,
        render_web_config_ui,
    )

    config = {
        "upstream": {
            "id": "primary",
            "name": "primary",
            "base_url": "https://primary.example.test",
            "model": "tool-model",
            "tools_enabled": raw,
            "capabilities": {
                "supports_tools": True,
                "supports_function_calls": True,
            },
            "models": [
                {
                    "name": "tool-model",
                    "capabilities": {
                        "supports_tools": True,
                        "supports_function_calls": True,
                    },
                }
            ],
        }
    }

    matrix = _render_model_capability_matrix(config)
    page = render_web_config_ui(config)

    assert "tools_enabled=off" in matrix
    assert 'option value="off" selected' in page
