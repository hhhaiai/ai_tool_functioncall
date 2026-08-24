"""Tests for per-model capability routing and the recognize_* llm tools.

These cover the pieces the user asked for: upstream models declare which
capabilities they support (tool call, function call, web search, image /
music / video recognition, ...), the router picks a model for a given
capability, and the recognize_* builtin tools dispatch through it.
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

import pytest

from src import gateway_builtin_tools as bt
from src.gateway_builtin_tools import (
    BUILTIN_TOOLS,
    _media_part_from_input,
    call_upstream_llm,
)
from src.gateway_config import _default_config
from src.gateway_errors import BadRequestError, ToolExecutionError, UpstreamHTTPError
from src.gateway_model_router import (
    ModelRouter,
    get_model_router,
    reset_model_router,
)


class TestRecognizeToolRegistry(unittest.TestCase):
    def test_recognize_tools_registered_with_llm_flag(self):
        for name in ("recognize_image", "recognize_music", "recognize_video"):
            tool = BUILTIN_TOOLS.get(name)
            self.assertIsNotNone(tool, f"{name} not registered")
            self.assertTrue(tool.llm, f"{name} should be an llm tool")
            self.assertEqual(tool.name, name)

    def test_recognize_aliases_resolve_to_canonical(self):
        aliases = {
            "vision": "recognize_image",
            "see_image": "recognize_image",
            "look_at_image": "recognize_image",
            "music_recognition": "recognize_music",
            "listen_music": "recognize_music",
            "analyze_audio": "recognize_music",
            "video_recognition": "recognize_video",
            "watch_video": "recognize_video",
            "analyze_video": "recognize_video",
        }
        for alias, canonical in aliases.items():
            tool = BUILTIN_TOOLS.get(alias)
            self.assertIsNotNone(tool, f"alias {alias!r} not registered")
            self.assertEqual(tool.name, canonical, f"{alias} -> {tool.name}, expected {canonical}")


def test_legacy_multi_profile_models_ignore_environment_default_during_reload(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config

    config_path = tmp_path / "gateway.json"
    monkeypatch.setattr(gateway_config, "CONFIG_PATH", config_path)
    monkeypatch.setenv("UPSTREAM_MODEL", "env-model")
    monkeypatch.setenv("GATEWAY_ALLOW_PLAINTEXT_CONFIG", "1")
    legacy_profiles = [
        {
            "id": "p1",
            "name": "p1",
            "base_url": "https://p1.example.test",
            "model": "profile-one",
        },
        {
            "id": "p2",
            "name": "p2",
            "base_url": "https://p2.example.test",
            "model": "profile-two",
        },
    ]
    config_path.write_text(
        json.dumps(
            {
                "upstream_profiles": legacy_profiles,
                "active_upstream_id": "p1",
                "upstream": legacy_profiles[0],
            }
        ),
        encoding="utf-8",
    )

    loaded = gateway_config.load_config()
    assert [(row["profile_id"], row["model"]) for row in gateway_config.flatten_profile_models(loaded)] == [
        ("p1", "profile-one"),
        ("p2", "profile-two"),
    ]

    gateway_config.save_config(loaded)
    reloaded = gateway_config.load_config()
    assert [(row["profile_id"], row["model"]) for row in gateway_config.flatten_profile_models(reloaded)] == [
        ("p1", "profile-one"),
        ("p2", "profile-two"),
    ]


def test_config_load_rejects_non_boolean_model_capability(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config
    from src.gateway_errors import ConfigError

    config_path = tmp_path / "gateway.json"
    monkeypatch.setattr(gateway_config, "CONFIG_PATH", config_path)
    config_path.write_text(
        json.dumps(
            {
                "upstream": {
                    "id": "p1",
                    "base_url": "https://p1.example.test",
                    "model": "vision-pro",
                    "models": [
                        {
                            "name": "vision-pro",
                            "capabilities": {"supports_image_recognition": "false"},
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="JSON boolean"):
        gateway_config.load_config()


class TestMediaPartFromInput(unittest.TestCase):
    def test_data_url_passthrough(self):
        part = _media_part_from_input({"data_url": "data:image/png;base64,YWJj"}, "image")
        self.assertEqual(
            part,
            {
                "type": "gateway_media",
                "media_kind": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"},
            },
        )

    def test_http_url_passthrough(self):
        part = _media_part_from_input({"url": "https://example.com/x.png"}, "image")
        self.assertEqual(
            part,
            {
                "type": "gateway_media",
                "media_kind": "image",
                "source": {"type": "url", "url": "https://example.com/x.png"},
            },
        )

    def test_base64_input(self):
        part = _media_part_from_input({"base64": "YWJj", "mime_type": "image/jpeg"}, "image")
        self.assertEqual(part["media_kind"], "image")
        self.assertEqual(part["source"]["media_type"], "image/jpeg")
        self.assertEqual(part["source"]["data"], "YWJj")

    def test_local_path(self):
        tmp_path = pathlib.Path(tempfile.mkdtemp())
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n")
        old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        os.environ["GATEWAY_WORKSPACE_ROOT"] = str(tmp_path)
        try:
            part = _media_part_from_input({"path": "pic.png"}, "image")
        finally:
            if old_root is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
        self.assertEqual(part["media_kind"], "image")
        self.assertEqual(part["source"]["media_type"], "image/png")
        self.assertEqual(
            part["source"]["data"],
            base64.b64encode(b"\x89PNG\r\n").decode("ascii"),
        )

    def test_missing_input_raises(self):
        with self.assertRaises(ToolExecutionError):
            _media_part_from_input({}, "image")

    def test_missing_file_raises(self):
        with self.assertRaises(ToolExecutionError):
            _media_part_from_input({"path": "/no/such/file.png"}, "image")

    def test_invalid_base64_is_rejected(self):
        with self.assertRaises(ToolExecutionError) as cm:
            _media_part_from_input(
                {"base64": "not base64!", "mime_type": "image/png"}, "image"
            )
        self.assertEqual(cm.exception.failure_type, "invalid_input")

    def test_mime_must_match_media_kind(self):
        with self.assertRaises(ToolExecutionError) as cm:
            _media_part_from_input(
                {"base64": "YWJj", "mime_type": "audio/mpeg"}, "image"
            )
        self.assertEqual(cm.exception.failure_type, "invalid_input")

    def test_file_url_and_metadata_ip_are_rejected(self):
        for url in ("file:///etc/passwd", "http://169.254.169.254/latest/meta-data"):
            with self.subTest(url=url):
                with self.assertRaises(ToolExecutionError) as cm:
                    _media_part_from_input({"url": url}, "image")
                self.assertEqual(cm.exception.failure_type, "invalid_input")

    def test_oversize_local_media_is_rejected_before_read(self):
        tmp_path = pathlib.Path(tempfile.mkdtemp())
        image = tmp_path / "large.png"
        image.write_bytes(b"0123456789")
        old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        old_limit = os.environ.get("GATEWAY_MAX_MEDIA_INPUT_BYTES")
        os.environ["GATEWAY_WORKSPACE_ROOT"] = str(tmp_path)
        os.environ["GATEWAY_MAX_MEDIA_INPUT_BYTES"] = "4"
        try:
            with mock.patch.object(
                pathlib.Path,
                "read_bytes",
                side_effect=AssertionError("oversize media must not be read"),
            ):
                with self.assertRaises(ToolExecutionError) as cm:
                    _media_part_from_input({"path": "large.png"}, "image")
            self.assertEqual(cm.exception.failure_type, "input_too_large")
        finally:
            if old_root is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root
            if old_limit is None:
                os.environ.pop("GATEWAY_MAX_MEDIA_INPUT_BYTES", None)
            else:
                os.environ["GATEWAY_MAX_MEDIA_INPUT_BYTES"] = old_limit


class TestCapabilityRouting(unittest.TestCase):
    def _router_with_models(self, models: list[dict]) -> ModelRouter:
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        profile = cfg["upstream"]
        profile["base_url"] = "https://upstream.example.test"
        profile["models"] = models
        profile.pop("model", None)
        router = ModelRouter(lambda: cfg)
        return router

    def test_select_for_capability_picks_declaring_model(self):
        models = [
            {"model": "text-only", "capabilities": {"supports_image_recognition": False}},
            {"model": "vision-pro", "capabilities": {"supports_image_recognition": True}},
        ]
        router = self._router_with_models(models)
        route = router.select_or_raise("supports_image_recognition")
        self.assertEqual(route.model, "vision-pro")

    def test_no_capability_raises(self):
        models = [
            {"model": "text-only", "capabilities": {"supports_image_recognition": False}},
        ]
        router = self._router_with_models(models)
        with self.assertRaises(Exception):
            router.select_or_raise("supports_image_recognition")

    def test_models_for_capability_returns_only_declaring(self):
        models = [
            {"model": "a", "capabilities": {"supports_music_recognition": True}},
            {"model": "b", "capabilities": {"supports_music_recognition": False}},
            {"model": "c", "capabilities": {"supports_music_recognition": True}},
        ]
        router = self._router_with_models(models)
        rows = router.models_for_capability("supports_music_recognition")
        self.assertEqual({r["model"] for r in rows}, {"a", "c"})

    def test_unknown_capability_raises_config_error(self):
        router = self._router_with_models([])
        from src.gateway_errors import ConfigError
        with self.assertRaises(ConfigError):
            router.models_for_capability("not_a_real_capability")

    def test_disabled_and_empty_base_profiles_are_not_selected(self):
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        disabled = dict(cfg["upstream"])
        disabled.update(
            {
                "id": "disabled",
                "name": "disabled",
                "enabled": False,
                "base_url": "",
                "model": "bad-model",
                "models": [
                    {
                        "name": "bad-model",
                        "capabilities": {"supports_image_recognition": True},
                    }
                ],
            }
        )
        healthy = dict(cfg["upstream"])
        healthy.update(
            {
                "id": "healthy",
                "name": "healthy",
                "enabled": True,
                "base_url": "https://healthy.example.test",
                "model": "vision-pro",
                "models": [
                    {
                        "name": "vision-pro",
                        "capabilities": {"supports_image_recognition": True},
                    }
                ],
            }
        )
        cfg["upstream_profiles"] = [disabled, healthy]
        cfg["active_upstream_id"] = "disabled"
        router = ModelRouter(lambda: cfg)

        route = router.select_or_raise("supports_image_recognition", strategy="first")

        self.assertEqual(route.profile_id, "healthy")
        self.assertEqual(route.model, "vision-pro")

    def test_declared_capability_without_usable_route_is_distinct(self):
        from src.gateway_model_router import NoHealthyModelRouteError

        cfg = _default_config()
        cfg["upstream"].update(
            {
                "base_url": "",
                "model": "vision-pro",
                "models": [
                    {
                        "name": "vision-pro",
                        "capabilities": {"supports_image_recognition": True},
                    }
                ],
            }
        )
        with self.assertRaises(NoHealthyModelRouteError):
            ModelRouter(lambda: cfg).select_or_raise("supports_image_recognition")

    def test_selection_uses_one_config_snapshot(self):
        snapshots = []
        for model, url in (("snapshot-a", "https://a.example.test"), ("snapshot-b", "https://b.example.test")):
            cfg = _default_config()
            cfg["upstream"]["base_url"] = "https://upstream.example.test"
            cfg["upstream"].update(
                {
                    "id": model,
                    "base_url": url,
                    "model": model,
                    "models": [
                        {
                            "name": model,
                            "capabilities": {"supports_image_recognition": True},
                        }
                    ],
                }
            )
            cfg["upstream_profiles"] = [cfg["upstream"]]
            cfg["active_upstream_id"] = model
            snapshots.append(cfg)
        calls = 0

        def provider():
            nonlocal calls
            value = snapshots[min(calls, 1)]
            calls += 1
            return value

        route = ModelRouter(provider).select_or_raise("supports_image_recognition")
        self.assertEqual(calls, 1)
        self.assertEqual((route.model, route.base_url), ("snapshot-a", "https://a.example.test"))

    def test_round_robin_state_is_process_shared(self):
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["models"] = [
            {"name": "a", "capabilities": {"supports_image_recognition": True}},
            {"name": "b", "capabilities": {"supports_image_recognition": True}},
        ]
        router = ModelRouter(lambda: cfg)
        self.assertEqual(
            router.select_or_raise("supports_image_recognition", strategy="round_robin").model,
            "a",
        )
        self.assertEqual(
            router.select_or_raise("supports_image_recognition", strategy="round_robin").model,
            "b",
        )

    def test_round_robin_is_lock_safe_under_concurrent_users(self):
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["models"] = [
            {"name": "a", "capabilities": {"supports_image_recognition": True}},
            {"name": "b", "capabilities": {"supports_image_recognition": True}},
        ]
        router = ModelRouter(lambda: cfg)

        def select(_index: int) -> str:
            return router.select_or_raise(
                "supports_image_recognition", strategy="round_robin"
            ).model

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            selected = list(pool.map(select, range(100)))
        self.assertEqual(selected.count("a"), 50)
        self.assertEqual(selected.count("b"), 50)


def test_get_model_router_returns_process_wide_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    from src import gateway_config

    cfg = _default_config()
    cfg["upstream"]["base_url"] = "https://upstream.example.test"
    monkeypatch.setattr(gateway_config, "load_config", lambda: cfg)
    reset_model_router()
    try:
        assert get_model_router() is get_model_router()
    finally:
        reset_model_router()


@pytest.mark.parametrize(
    ("protocol", "expected_part"),
    [
        (
            "openai_chat",
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}},
        ),
        (
            "openai_responses",
            {"type": "input_image", "image_url": "data:image/png;base64,YWJj"},
        ),
        (
            "anthropic_messages",
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"},
            },
        ),
    ],
)
def test_image_reaches_each_upstream_protocol_with_valid_wire_shape(
    protocol: str,
    expected_part: dict,
) -> None:
    from src.gateway_protocol import _convert_request_to_upstream

    body = {
        "model": "vision-pro",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,YWJj"},
                    },
                    {"type": "text", "text": "describe"},
                ],
            }
        ],
    }

    upstream_path, wire_body = _convert_request_to_upstream(
        "/v1/chat/completions", body, protocol
    )

    if protocol == "openai_responses":
        assert upstream_path == "/v1/responses"
        parts = wire_body["input"][0]["content"]
    else:
        assert upstream_path in {"/v1/chat/completions", "/v1/messages"}
        parts = wire_body["messages"][0]["content"]
    assert expected_part in parts


def test_legacy_string_shaped_image_url_is_normalized_without_crashing() -> None:
    from src.gateway_protocol import _convert_request_to_upstream

    body = {
        "model": "vision-pro",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": "data:image/png;base64,YWJj"}
                ],
            }
        ],
    }
    _, anthropic = _convert_request_to_upstream(
        "/v1/chat/completions", body, "anthropic_messages"
    )
    assert anthropic["messages"][0]["content"][0]["type"] == "image"


def test_anthropic_image_response_never_creates_list_valued_output_text() -> None:
    from src.gateway_protocol import _convert_response_to_downstream

    response = {
        "id": "msg_1",
        "model": "vision-pro",
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": "result"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"},
            },
        ],
    }
    converted = _convert_response_to_downstream(
        "/v1/responses", response, "anthropic_messages"
    )
    output_text = converted["output"][0]["content"][0]
    assert output_text["type"] == "output_text"
    assert isinstance(output_text["text"], str)
    assert "YWJj" not in output_text["text"]


def test_recognition_ownership_depends_on_local_path_access() -> None:
    from src.gateway_builtin_tools import ToolCall
    from src.gateway_tool_runtime import _tool_call_requires_downstream_execution

    remote = ToolCall(
        "call_remote",
        "recognize_image",
        {"base64": "YWJj", "mime_type": "image/png"},
        {},
    )
    local = ToolCall("call_local", "recognize_image", {"path": "pic.png"}, {})
    assert _tool_call_requires_downstream_execution(remote, {}) is False
    assert _tool_call_requires_downstream_execution(local, {}) is True


def test_public_direct_recognition_reaches_router_and_preserves_no_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_model_router, gateway_tool_runtime

    cfg = _default_config()
    cfg["upstream"]["base_url"] = "https://upstream.example.test"
    cfg["upstream"]["model"] = "text-only"
    cfg["upstream"]["models"] = [
        {
            "name": "text-only",
            "capabilities": {"supports_image_recognition": False},
        }
    ]
    router = ModelRouter(lambda: cfg)
    monkeypatch.setattr(gateway_model_router, "get_model_router", lambda: router)

    result = gateway_tool_runtime.execute_direct_tool_call(
        {
            "name": "gateway__recognize_image",
            "arguments": {"base64": "YWJj", "mime_type": "image/png"},
        }
    )

    assert result["success"] is False
    assert result["failure_type"] == "no_capability"
    assert "model router unavailable" not in result["content"]


def test_public_direct_local_media_path_is_rejected_before_server_workspace_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_tool_runtime

    monkeypatch.setenv("GATEWAY_EXECUTE_USER_SIDE_TOOLS", "1")
    with mock.patch(
        "src.gateway_builtin_tools._media_part_from_input",
        side_effect=AssertionError("server must not read a user's local media path"),
    ):
        with pytest.raises(BadRequestError) as caught:
            gateway_tool_runtime.execute_direct_tool_call(
                {
                    "name": "gateway__recognize_image",
                    "arguments": {"path": "user-project/private.png"},
                }
            )
    assert caught.value.detail["failure_type"] == (
        "direct_user_side_tool_requires_downstream_client"
    )


class TestCallUpstreamLlm(unittest.TestCase):
    @staticmethod
    def _two_model_router() -> ModelRouter:
        cfg = _default_config()
        cfg["upstream"].update(
            {
                "base_url": "https://upstream.example.test",
                "protocol": "openai_chat",
                "model": "broken",
                "models": [
                    {
                        "name": "broken",
                        "capabilities": {"supports_image_recognition": True},
                    },
                    {
                        "name": "healthy",
                        "capabilities": {"supports_image_recognition": True},
                    },
                ],
            }
        )
        return ModelRouter(lambda: cfg)

    def test_routes_by_capability_and_returns_text(self):
        models = [
            {"model": "vision-pro", "capabilities": {"supports_image_recognition": True}},
        ]
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["models"] = models
        cfg["upstream"].pop("model", None)
        router = ModelRouter(lambda: cfg)
        reset_model_router()
        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            fake_response = {
                "choices": [{"message": {"role": "assistant", "content": "a cat"}}]
            }
            with mock.patch(
                "src.gateway_proxy.NativeProxyClient.forward", return_value=fake_response
            ) as forward_mock:
                content = [{"type": "image_url", "image_url": "data:image/png;base64,abc"}]
                text = call_upstream_llm("supports_image_recognition", content, question="what is this?")
                self.assertEqual(text, "a cat")
                forward_mock.assert_called_once()
                body = forward_mock.call_args[0][1]
                self.assertEqual(body["model"], "vision-pro")
                self.assertIn("image_url", json.dumps(body["messages"][0]["content"]))

    def test_no_capability_raises_tool_execution_error(self):
        models = [
            {"model": "text-only", "capabilities": {"supports_image_recognition": False}},
        ]
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["models"] = models
        cfg["upstream"].pop("model", None)
        router = ModelRouter(lambda: cfg)
        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            with self.assertRaises(ToolExecutionError) as cm:
                call_upstream_llm(
                    "supports_image_recognition",
                    [{"type": "text", "text": "hi"}],
                )
            self.assertEqual(cm.exception.failure_type, "no_capability")

    def test_upstream_failure_raises_execution_failed(self):
        models = [
            {"model": "vision-pro", "capabilities": {"supports_image_recognition": True}},
        ]
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["models"] = models
        cfg["upstream"].pop("model", None)
        router = ModelRouter(lambda: cfg)
        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            with mock.patch(
                "src.gateway_proxy.NativeProxyClient.forward", side_effect=RuntimeError("boom")
            ):
                with self.assertRaises(ToolExecutionError) as cm:
                    call_upstream_llm(
                        "supports_image_recognition",
                        [{"type": "image_url", "image_url": "data:image/png;base64,abc"}],
                    )
                self.assertEqual(cm.exception.failure_type, "execution_failed")

    def test_retryable_failure_fails_over_to_second_model(self):
        router = self._two_model_router()
        calls: list[str] = []

        def forward(client, _path, body):
            calls.append(body["model"])
            if body["model"] == "broken":
                raise UpstreamHTTPError(503, {"type": "busy"})
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            with mock.patch(
                "src.gateway_proxy.NativeProxyClient.forward",
                autospec=True,
                side_effect=forward,
            ):
                result = call_upstream_llm(
                    "supports_image_recognition", [{"type": "text", "text": "hello"}]
                )

        self.assertEqual(result, "ok")
        self.assertEqual(calls, ["broken", "healthy"])

    def test_non_retryable_4xx_does_not_fail_over(self):
        router = self._two_model_router()
        calls: list[str] = []

        def forward(_client, _path, body):
            calls.append(body["model"])
            raise UpstreamHTTPError(400, {"type": "bad_request"})

        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            with mock.patch(
                "src.gateway_proxy.NativeProxyClient.forward",
                autospec=True,
                side_effect=forward,
            ):
                with self.assertRaises(ToolExecutionError) as cm:
                    call_upstream_llm(
                        "supports_image_recognition", [{"type": "text", "text": "hello"}]
                    )

        self.assertEqual(cm.exception.failure_type, "execution_failed")
        self.assertEqual(calls, ["broken"])

    def test_selected_model_preserves_transport_and_token_limits(self):
        cfg = _default_config()
        cfg["upstream"].update(
            {
                "base_url": "https://upstream.example.test",
                "protocol": "openai_chat",
                "retry_max_attempts": 7,
                "retry_initial_delay_seconds": 0.25,
                "retry_max_delay_seconds": 2.5,
                "retry_max_elapsed_seconds": 11,
                "max_response_bytes": 34567,
                "max_stderr_bytes": 4567,
                "max_stream_event_bytes": 5678,
                "max_stream_events": 6789,
                "max_input_tokens": 999,
                "max_output_tokens": 888,
                "models": [
                    {
                        "name": "bounded-model",
                        "max_input_tokens": 123,
                        "max_output_tokens": 45,
                        "capabilities": {"supports_image_recognition": True},
                    }
                ],
            }
        )
        router = ModelRouter(lambda: cfg)
        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            with mock.patch("src.gateway_proxy.NativeProxyClient") as client_cls:
                client_cls.return_value.forward.return_value = {
                    "choices": [{"message": {"role": "assistant", "content": "ok"}}]
                }
                result = call_upstream_llm(
                    "supports_image_recognition", [{"type": "text", "text": "hello"}]
                )

        self.assertEqual(result, "ok")
        profile = client_cls.call_args.kwargs["profile"]
        self.assertEqual(profile["retry_max_attempts"], 7)
        self.assertEqual(profile["retry_max_elapsed_seconds"], 11)
        self.assertEqual(profile["max_response_bytes"], 34567)
        self.assertEqual(profile["max_stderr_bytes"], 4567)
        self.assertEqual(profile["max_stream_event_bytes"], 5678)
        self.assertEqual(profile["max_stream_events"], 6789)
        self.assertEqual(profile["max_input_tokens"], 123)
        self.assertEqual(profile["max_output_tokens"], 45)
        self.assertEqual(client_cls.return_value.forward.call_args.args[1]["max_tokens"], 45)


class TestRecognizeToolEndToEnd(unittest.TestCase):
    def test_recognize_image_dispatches_to_upstream(self):
        tmp_path = pathlib.Path(tempfile.mkdtemp())
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG\r\n")
        models = [
            {"model": "vision-pro", "capabilities": {"supports_image_recognition": True}},
        ]
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["models"] = models
        cfg["upstream"].pop("model", None)
        router = ModelRouter(lambda: cfg)
        tool = BUILTIN_TOOLS["recognize_image"]
        old_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
        os.environ["GATEWAY_WORKSPACE_ROOT"] = str(tmp_path)
        try:
            with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
                with mock.patch(
                    "src.gateway_proxy.NativeProxyClient.forward",
                    return_value={"choices": [{"message": {"role": "assistant", "content": "a cat"}}]},
                ) as forward_mock:
                    result = tool.handler({"path": "pic.png"})
                    self.assertEqual(result, "a cat")
                    body = forward_mock.call_args[0][1]
                    self.assertEqual(body["model"], "vision-pro")
                    # the image data url must be present in the request
                    self.assertIn("data:image/png;base64,", json.dumps(body["messages"][0]["content"]))
        finally:
            if old_root is None:
                os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
            else:
                os.environ["GATEWAY_WORKSPACE_ROOT"] = old_root

    def test_recognize_music_dispatches_to_upstream(self):
        models = [
            {"model": "music-pro", "capabilities": {"supports_music_recognition": True}},
        ]
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["protocol"] = "openai_chat"
        cfg["upstream"]["models"] = models
        cfg["upstream"].pop("model", None)
        router = ModelRouter(lambda: cfg)
        tool = BUILTIN_TOOLS["recognize_music"]
        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            with mock.patch(
                "src.gateway_proxy.NativeProxyClient.forward",
                return_value={"choices": [{"message": {"role": "assistant", "content": "jazz"}}]},
            ) as forward_mock:
                result = tool.handler({"base64": "YWJj", "mime_type": "audio/mpeg"})
                self.assertEqual(result, "jazz")
                body = forward_mock.call_args[0][1]
                self.assertEqual(body["model"], "music-pro")
                audio_part = body["messages"][0]["content"][0]
                self.assertEqual(audio_part["type"], "input_audio")
                self.assertEqual(audio_part["input_audio"], {"data": "YWJj", "format": "mp3"})

    def test_recognize_video_fails_closed_without_transport_adapter(self):
        models = [
            {"model": "video-pro", "capabilities": {"supports_video_recognition": True}},
        ]
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["models"] = models
        cfg["upstream"].pop("model", None)
        router = ModelRouter(lambda: cfg)
        tool = BUILTIN_TOOLS["recognize_video"]
        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            with mock.patch(
                "src.gateway_proxy.NativeProxyClient.forward",
                return_value={"choices": [{"message": {"role": "assistant", "content": "a dog running"}}]},
            ) as forward_mock:
                with self.assertRaises(ToolExecutionError) as cm:
                    tool.handler({"base64": "YWJj", "mime_type": "video/mp4"})
                self.assertEqual(cm.exception.failure_type, "unsupported_media_transport")
                forward_mock.assert_not_called()

    def test_anthropic_audio_fails_closed_instead_of_becoming_image(self):
        models = [
            {"model": "music-pro", "capabilities": {"supports_music_recognition": True}},
        ]
        cfg = _default_config()
        cfg["upstream"]["base_url"] = "https://upstream.example.test"
        cfg["upstream"]["protocol"] = "anthropic_messages"
        cfg["upstream"]["models"] = models
        cfg["upstream"].pop("model", None)
        router = ModelRouter(lambda: cfg)
        tool = BUILTIN_TOOLS["recognize_music"]
        with mock.patch("src.gateway_builtin_tools.get_model_router", return_value=router):
            with mock.patch("src.gateway_proxy.NativeProxyClient.forward") as forward_mock:
                with self.assertRaises(ToolExecutionError) as cm:
                    tool.handler({"base64": "YWJj", "mime_type": "audio/mpeg"})
                self.assertEqual(cm.exception.failure_type, "unsupported_media_transport")
                forward_mock.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
