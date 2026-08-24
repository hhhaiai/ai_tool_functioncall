"""Capability-aware model routing for ordinary conversation requests."""
from __future__ import annotations

import concurrent.futures
import copy
import threading
from collections import Counter
from typing import Any

import pytest

from src.gateway_config import _default_config
from src.gateway_errors import UpstreamHTTPError
from src.gateway_model_router import (
    ModelRouter,
    client_for_request,
    required_capabilities_for_request,
    reset_model_router,
)
from src.gateway_proxy import NativeProxyClient


Json = dict[str, Any]


def _profile(
    profile_id: str,
    model: str,
    capabilities: Json,
    *,
    base_url: str | None = None,
    protocol: str = "openai_chat",
    tools_enabled: str = "native",
    enabled: bool = True,
) -> Json:
    base = copy.deepcopy(_default_config()["upstream"])
    base.update(
        {
            "id": profile_id,
            "name": profile_id,
            "enabled": enabled,
            "load_balance_enabled": True,
            "base_url": base_url or f"https://{profile_id}.example.test",
            "api_key": "",
            "model": model,
            "protocol": protocol,
            "tools_enabled": tools_enabled,
            "capabilities": {key: False for key in base["capabilities"]},
            "models": [
                {
                    "name": model,
                    "capabilities": capabilities,
                }
            ],
            "retry_max_attempts": 1,
            "retry_initial_delay_seconds": 0,
            "retry_max_delay_seconds": 0,
        }
    )
    return base


def _config(*profiles: Json, active: str | None = None) -> Json:
    cfg = _default_config()
    selected = next(
        (profile for profile in profiles if profile["id"] == active),
        profiles[0],
    )
    cfg["upstream_profiles"] = [copy.deepcopy(profile) for profile in profiles]
    cfg["active_upstream_id"] = str(active or selected["id"])
    cfg["upstream"] = copy.deepcopy(selected)
    return cfg


@pytest.fixture(autouse=True)
def _reset_shared_router() -> Any:
    reset_model_router()
    yield
    reset_model_router()


@pytest.mark.parametrize(
    ("path", "body", "expected"),
    [
        (
            "/v1/chat/completions",
            {
                "tools": [{"type": "function", "function": {"name": "lookup"}}],
            },
            {"supports_tools", "supports_function_calls"},
        ),
        (
            "/v1/messages",
            {
                "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
                "parallel_tool_calls": True,
                "stream": True,
            },
            {
                "supports_tools",
                "supports_function_calls",
                "supports_parallel_tool_calls",
                "supports_streaming",
            },
        ),
        (
            "/v1/responses",
            {"tools": [{"type": "web_search_preview"}]},
            {"supports_tools", "supports_web_search", "supports_network"},
        ),
        (
            "/v1/chat/completions",
            {"response_format": {"type": "json_schema", "json_schema": {}}},
            {"supports_json_schema"},
        ),
        (
            "/v1/responses",
            {
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_image",
                                "image_url": "https://cdn.example.test/a.png",
                            }
                        ],
                    }
                ]
            },
            {"supports_vision"},
        ),
        (
            "/v1/chat/completions",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {"data": "YQ==", "format": "mp3"},
                            }
                        ],
                    }
                ]
            },
            {"supports_audio_recognition"},
        ),
        (
            "/v1/chat/completions",
            {"modalities": ["text", "audio"]},
            {"supports_speech"},
        ),
    ],
)
def test_required_capabilities_are_inferred_without_request_identity(
    path: str,
    body: Json,
    expected: set[str],
) -> None:
    enriched = {
        **body,
        "workspace_root": "/tenant/private/workspace",
        "metadata": {"tenant": "tenant-secret", "session": "session-secret"},
    }

    inferred = required_capabilities_for_request(path, enriched)

    assert set(inferred) == expected
    assert all("tenant" not in value and "workspace" not in value for value in inferred)


def test_router_requires_one_model_to_satisfy_the_complete_capability_set() -> None:
    tools_only = _profile("tools", "tools-only", {"supports_tools": True})
    functions_only = _profile(
        "functions",
        "functions-only",
        {"supports_function_calls": True},
    )
    native = _profile(
        "native",
        "native-complete",
        {"supports_tools": True, "supports_function_calls": True},
    )
    router = ModelRouter(lambda: _config(tools_only, functions_only, native))

    route = router.select_or_raise_for_capabilities(
        ("supports_tools", "supports_function_calls")
    )

    assert route.key == "native/native-complete"


def test_client_for_request_selects_capable_profile_model_and_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config

    weak = _profile("weak", "text-only", {}, tools_enabled="adapter")
    native = _profile(
        "native",
        "tool-pro",
        {"supports_tools": True, "supports_function_calls": True},
        protocol="anthropic_messages",
    )
    cfg = _config(weak, native, active="weak")
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    monkeypatch.setenv("UPSTREAM_PROTOCOL", "openai_chat")
    body = {
        "model": "downstream-alias",
        "messages": [{"role": "user", "content": "use a tool"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }

    client = client_for_request("/v1/chat/completions", body)

    assert client.profile_id == "native"
    assert client.model == "tool-pro"
    assert client.base_url == "https://native.example.test"
    assert client.protocol == "anthropic_messages"
    assert client._model_required_capabilities == (
        "supports_tools",
        "supports_function_calls",
    )
    assert client._model_router is not None


def test_client_for_request_preserves_legacy_adapter_when_no_model_declares_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config
    from src import gateway_upstream_pool

    weak = _profile("weak", "text-only", {}, tools_enabled="adapter")
    cfg = _config(weak)
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    gateway_upstream_pool.reset_upstream_pool()

    client = client_for_request(
        "/v1/chat/completions",
        {"tools": [{"type": "function", "function": {"name": "lookup"}}]},
    )

    assert client.model == "text-only"
    assert client._model_route.key == "weak/text-only"
    assert client._model_adapter_fallback is True
    assert client._model_required_capabilities == (
        "supports_tools",
        "supports_function_calls",
    )


def test_declared_but_unhealthy_matching_route_preserves_legacy_request_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config

    unhealthy = _profile(
        "unhealthy",
        "tool-pro",
        {"supports_tools": True, "supports_function_calls": True},
        enabled=False,
    )
    cfg = _config(unhealthy)
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)

    client = client_for_request(
        "/v1/chat/completions",
        {"tools": [{"type": "function", "function": {"name": "lookup"}}]},
    )

    assert client.model == "tool-pro"
    assert client._model_route is None


def test_selected_model_overrides_downstream_alias_at_final_wire_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config

    vision = _profile("vision", "vision-pro", {"supports_vision": True})
    cfg = _config(vision)
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    client = client_for_request(
        "/v1/chat/completions",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example.test/a.png"},
                        }
                    ],
                }
            ]
        },
    )
    captured: list[Json] = []

    def fake_request(method: str, path: str, body: Json | None = None) -> Json:
        captured.append(copy.deepcopy(body or {}))
        return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    monkeypatch.setattr(client, "_do_request", fake_request)

    client._forward_once(
        "/v1/chat/completions",
        {
            "model": "downstream-alias",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example.test/a.png"},
                        }
                    ],
                }
            ],
        },
    )

    assert captured[0]["model"] == "vision-pro"


def test_retryable_failure_fails_over_across_capable_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config

    first = _profile(
        "first",
        "tool-a",
        {"supports_tools": True, "supports_function_calls": True},
    )
    second = _profile(
        "second",
        "tool-b",
        {"supports_tools": True, "supports_function_calls": True},
    )
    cfg = _config(first, second)
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    attempts: list[tuple[str, str]] = []

    def fake_impl(
        self: NativeProxyClient,
        method: str,
        path: str,
        body: Json | None = None,
    ) -> Json:
        attempts.append((self.model, str((body or {}).get("model") or "")))
        if self.model == "tool-a":
            raise UpstreamHTTPError(503, {"error": "busy"})
        return {
            "model": self.model,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }

    monkeypatch.setattr(NativeProxyClient, "_do_request_impl", fake_impl)
    client = client_for_request(
        "/v1/chat/completions",
        {
            "model": "downstream-alias",
            "messages": [{"role": "user", "content": "use a tool"}],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
        },
    )

    response = client.forward(
        "/v1/chat/completions",
        {
            "model": "downstream-alias",
            "messages": [{"role": "user", "content": "use a tool"}],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
        },
    )

    assert response["model"] == "tool-b"
    assert attempts == [("tool-a", "tool-a"), ("tool-b", "tool-b")]


def test_non_retryable_client_error_does_not_fail_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config

    profiles = [
        _profile(
            profile_id,
            model,
            {"supports_tools": True, "supports_function_calls": True},
        )
        for profile_id, model in (("first", "tool-a"), ("second", "tool-b"))
    ]
    cfg = _config(*profiles)
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    attempts: list[str] = []

    def fake_impl(
        self: NativeProxyClient,
        method: str,
        path: str,
        body: Json | None = None,
    ) -> Json:
        attempts.append(self.model)
        raise UpstreamHTTPError(400, {"error": "bad request"})

    monkeypatch.setattr(NativeProxyClient, "_do_request_impl", fake_impl)
    client = client_for_request(
        "/v1/chat/completions",
        {"tools": [{"type": "function", "function": {"name": "lookup"}}]},
    )

    with pytest.raises(UpstreamHTTPError) as caught:
        client.forward(
            "/v1/chat/completions",
            {
                "model": "downstream-alias",
                "messages": [{"role": "user", "content": "use a tool"}],
                "tools": [
                    {"type": "function", "function": {"name": "lookup"}}
                ],
            },
        )

    assert caught.value.upstream_status == 400
    assert attempts == ["tool-a"]


def test_round_robin_request_selection_is_shared_and_lock_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config

    profiles = [
        _profile(profile_id, model, {"supports_streaming": True})
        for profile_id, model in (("a", "stream-a"), ("b", "stream-b"))
    ]
    cfg = _config(*profiles)
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    body = {"stream": True, "messages": [{"role": "user", "content": "hello"}]}

    def select(_index: int) -> str:
        return client_for_request(
            "/v1/chat/completions",
            body,
            strategy="round_robin",
        ).model

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        selected = list(pool.map(select, range(100)))

    assert Counter(selected) == Counter({"stream-a": 50, "stream-b": 50})


def test_run_tool_orchestration_uses_selected_route_behavior_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config
    from src.gateway_tool_runtime import run_tool_orchestration

    weak = _profile("weak", "text-only", {}, tools_enabled="adapter")
    native = _profile(
        "native",
        "tool-pro",
        {"supports_tools": True, "supports_function_calls": True},
        tools_enabled="native",
    )
    cfg = _config(weak, native, active="weak")
    cfg["gateway"]["tool_mode"] = "proxy"
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    captured: list[tuple[str, Json]] = []

    def fake_impl(
        self: NativeProxyClient,
        method: str,
        path: str,
        body: Json | None = None,
    ) -> Json:
        captured.append((self.profile_id, copy.deepcopy(body or {})))
        return {
            "model": self.model,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }

    monkeypatch.setattr(NativeProxyClient, "_do_request_impl", fake_impl)
    body = {
        "model": "downstream-alias",
        "messages": [{"role": "user", "content": "use a tool"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
    }

    response = run_tool_orchestration("/v1/chat/completions", body)

    assert response["model"] == "tool-pro"
    assert captured[0][0] == "native"
    assert captured[0][1]["model"] == "tool-pro"


def test_selected_behavior_context_is_credential_free_and_request_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config
    from src.gateway_streaming import (
        _tools_enabled_for_upstream,
        _upstream_native_tools_capable,
    )
    from src.gateway_tool_runtime import (
        _REQUEST_UPSTREAM_CONFIG,
        _runtime_upstream_config_for_client,
        _weak_upstream_text_tools_active,
    )

    native = _profile(
        "native",
        "tool-pro",
        {"supports_tools": True, "supports_function_calls": True},
        tools_enabled="native",
    )
    cfg = _config(native)
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    client = client_for_request(
        "/v1/chat/completions",
        {"tools": [{"type": "function", "function": {"name": "lookup"}}]},
    )
    selected = _runtime_upstream_config_for_client(client)

    assert selected is not None
    assert set(selected) == {
        "model",
        "protocol",
        "tools_enabled",
        "max_input_tokens",
        "capabilities",
    }
    assert "api_key" not in selected
    assert "base_url" not in selected
    assert "workspace" not in str(selected).lower()
    assert "tenant" not in str(selected).lower()
    assert "session" not in str(selected).lower()
    token = _REQUEST_UPSTREAM_CONFIG.set(selected)
    try:
        assert _tools_enabled_for_upstream() == "native"
        assert _upstream_native_tools_capable() is True
        assert _weak_upstream_text_tools_active("orchestrate") is False
    finally:
        _REQUEST_UPSTREAM_CONFIG.reset(token)

    assert _REQUEST_UPSTREAM_CONFIG.get() is None


def test_concurrent_requests_keep_model_behavior_and_workspace_data_isolated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from src import gateway_config
    from src.gateway_tool_runtime import _request_upstream_config, run_tool_orchestration

    tools = _profile(
        "tools",
        "tool-pro",
        {"supports_tools": True, "supports_function_calls": True},
    )
    vision = _profile("vision", "vision-pro", {"supports_vision": True})
    cfg = _config(tools, vision)
    cfg["gateway"]["tool_mode"] = "proxy"
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)

    workspaces = [tmp_path / "tenant-a", tmp_path / "tenant-b"]
    for workspace in workspaces:
        workspace.mkdir()
    barrier = threading.Barrier(2)
    observed: list[tuple[str, str, str, set[str]]] = []

    def fake_impl(
        self: NativeProxyClient,
        method: str,
        path: str,
        body: Json | None = None,
    ) -> Json:
        before = _request_upstream_config()
        barrier.wait(timeout=5)
        after = _request_upstream_config()
        observed.append(
            (
                self.model,
                str(before.get("model") or ""),
                str(after.get("model") or ""),
                set((body or {}).keys()),
            )
        )
        return {
            "model": self.model,
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        }

    monkeypatch.setattr(NativeProxyClient, "_do_request_impl", fake_impl)
    requests = [
        {
            "model": "downstream-alias",
            "workspace_root": str(workspaces[0]),
            "metadata": {"tenant": "tenant-a", "session": "session-a"},
            "messages": [{"role": "user", "content": "use a tool"}],
            "tools": [{"type": "function", "function": {"name": "lookup"}}],
        },
        {
            "model": "downstream-alias",
            "workspace_root": str(workspaces[1]),
            "metadata": {"tenant": "tenant-b", "session": "session-b"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://cdn.example.test/a.png"},
                        }
                    ],
                }
            ],
        },
    ]

    def invoke(index: int) -> Json:
        return run_tool_orchestration(
            "/v1/chat/completions",
            requests[index],
            client_id=f"client-{index}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(invoke, range(2)))

    assert {response["model"] for response in responses} == {"tool-pro", "vision-pro"}
    assert {
        (model, before_model, after_model)
        for model, before_model, after_model, _keys in observed
    } == {
        ("tool-pro", "tool-pro", "tool-pro"),
        ("vision-pro", "vision-pro", "vision-pro"),
    }
    for _model, _before_model, _after_model, keys in observed:
        assert "workspace_root" not in keys
        assert "client_id" not in keys
