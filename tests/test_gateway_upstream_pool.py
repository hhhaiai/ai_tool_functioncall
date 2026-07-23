from __future__ import annotations

import copy
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import src.toolcall_gateway as gateway
from src.gateway_errors import UpstreamHTTPError, UpstreamTimeoutError
from src.gateway_proxy import NativeProxyClient
from src.gateway_upstream_pool import UpstreamProfilePool, reset_upstream_pool


def _profile(profile_id: str, base_url: str, *, protocol: str = "openai_chat") -> dict:
    return {
        "id": profile_id,
        "name": profile_id,
        "base_url": base_url,
        "api_key": f"secret-{profile_id}",
        "model": f"model-{profile_id}",
        "protocol": protocol,
        "tools_enabled": "adapter",
        "timeout_seconds": 1.0,
        "retry_max_attempts": 1,
        "retry_initial_delay_seconds": 0.0,
        "retry_max_delay_seconds": 0.0,
        "retry_max_elapsed_seconds": 2.0,
        "max_input_tokens": 10000,
        "max_output_tokens": 1000,
        "max_response_bytes": 1024 * 1024,
        "max_stderr_bytes": 64 * 1024,
        "max_stream_event_bytes": 64 * 1024,
        "max_stream_events": 1000,
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
            "supports_json_schema": False,
        },
    }


def _pool_config(profiles, **concurrency):
    return {
        "upstream": copy.deepcopy(profiles[0]),
        "upstream_profiles": copy.deepcopy(profiles),
        "active_upstream_id": profiles[0]["id"],
        "concurrency": {
            "multi_upstream_enabled": True,
            "load_balance_strategy": "round_robin",
            "multi_upstream_failure_threshold": 1,
            "multi_upstream_recovery_seconds": 60,
            **concurrency,
        },
    }


def test_pool_round_robin_least_connections_and_circuit_breaker():
    profiles = [_profile("a", "http://a.test"), _profile("b", "http://b.test")]
    pool = UpstreamProfilePool(_pool_config(profiles))
    assert [pool.select_profile()["id"], pool.select_profile()["id"]] == ["a", "b"]

    least = UpstreamProfilePool(_pool_config(profiles, load_balance_strategy="least_connections"))
    least.request_start("a")
    assert least.select_profile()["id"] == "b"
    least.request_end("a")

    started = pool.request_start("a")
    pool.request_failure("a", UpstreamTimeoutError("timeout"))
    pool.request_end("a")
    assert started > 0
    assert pool.select_profile()["id"] == "b"
    snapshot = pool.snapshot()
    a_health = next(item for item in snapshot["profiles"] if item["id"] == "a")
    assert a_health["healthy"] is False
    assert a_health["failure_count"] == 1
    assert "secret-a" not in json.dumps(snapshot)


def test_pool_excludes_profiles_with_incompatible_protocol_contract():
    pool = UpstreamProfilePool(
        _pool_config([
            _profile("chat", "http://chat.test"),
            _profile("anthropic", "http://anthropic.test", protocol="anthropic_messages"),
        ])
    )
    assert [item["id"] for item in pool.profiles] == ["chat"]
    assert pool.excluded == [{
        "id": "anthropic",
        "base_url": "http://anthropic.test",
        "reason": "incompatible_routing_contract",
    }]


class _Server:
    def __init__(self, marker: str, status: int = 200) -> None:
        self.marker = marker
        self.status = status
        self.calls = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                owner.calls += 1
                length = int(self.headers.get("content-length") or "0")
                self.rfile.read(length)
                payload = {
                    "id": f"chatcmpl_{owner.marker}",
                    "object": "chat.completion",
                    "model": f"model-{owner.marker}",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": owner.marker},
                        "finish_reason": "stop",
                    }],
                }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(owner.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, _format, *_args):  # noqa: N802
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def _install_config(tmp_path: pathlib.Path, profiles: list[dict], monkeypatch, **concurrency) -> pathlib.Path:
    monkeypatch.delenv("GATEWAY_UPSTREAM_PROTOCOL", raising=False)
    monkeypatch.delenv("UPSTREAM_PROTOCOL", raising=False)
    gateway.CONFIG_PATH = tmp_path / "gateway.config.json"
    cfg = gateway._default_config()
    cfg["upstream"] = copy.deepcopy(profiles[0])
    cfg["upstream_profiles"] = copy.deepcopy(profiles)
    cfg["active_upstream_id"] = profiles[0]["id"]
    cfg["active_upstream"] = profiles[0]["id"]
    cfg["concurrency"].update({
        "multi_upstream_enabled": True,
        "load_balance_strategy": "round_robin",
        "multi_upstream_failure_threshold": 1,
        "multi_upstream_recovery_seconds": 60,
        **concurrency,
    })
    gateway.save_config(cfg)
    reset_upstream_pool()
    return gateway.CONFIG_PATH


def _content(response: dict) -> str:
    return str(response["choices"][0]["message"]["content"])


def test_native_proxy_round_robins_compatible_profiles(tmp_path, monkeypatch):
    first = _Server("first")
    second = _Server("second")
    old_config = gateway.CONFIG_PATH
    try:
        _install_config(tmp_path, [_profile("first", first.url), _profile("second", second.url)], monkeypatch)
        body = {"model": "downstream", "messages": [{"role": "user", "content": "hi"}]}
        responses = [NativeProxyClient().forward("/v1/chat/completions", body) for _ in range(2)]
        assert [_content(item) for item in responses] == ["first", "second"]
        assert first.calls == 1
        assert second.calls == 1
    finally:
        gateway.CONFIG_PATH = old_config
        reset_upstream_pool()
        first.close()
        second.close()


def test_native_proxy_fails_over_retryable_error_but_not_client_error(tmp_path, monkeypatch):
    unavailable = _Server("unavailable", status=503)
    healthy = _Server("healthy")
    old_config = gateway.CONFIG_PATH
    try:
        _install_config(tmp_path, [_profile("unavailable", unavailable.url), _profile("healthy", healthy.url)], monkeypatch)
        response = NativeProxyClient().forward(
            "/v1/chat/completions",
            {"model": "downstream", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert _content(response) == "healthy"
        assert unavailable.calls == 1 and healthy.calls == 1

        bad = _Server("bad-request", status=400)
        untouched = _Server("untouched")
        try:
            _install_config(tmp_path, [_profile("bad", bad.url), _profile("untouched", untouched.url)], monkeypatch)
            with pytest.raises(UpstreamHTTPError) as exc_info:
                NativeProxyClient().forward(
                    "/v1/chat/completions",
                    {"model": "downstream", "messages": [{"role": "user", "content": "bad"}]},
                )
            assert exc_info.value.upstream_status == 400
            assert bad.calls == 1
            assert untouched.calls == 0
        finally:
            bad.close()
            untouched.close()
    finally:
        gateway.CONFIG_PATH = old_config
        reset_upstream_pool()
        unavailable.close()
        healthy.close()


def test_explicit_single_attempt_disables_cross_profile_failover(tmp_path, monkeypatch):
    unavailable = _Server("unavailable", status=503)
    healthy = _Server("healthy")
    old_config = gateway.CONFIG_PATH
    try:
        _install_config(
            tmp_path,
            [_profile("unavailable", unavailable.url), _profile("healthy", healthy.url)],
            monkeypatch,
            multi_upstream_max_attempts=1,
        )
        with pytest.raises(UpstreamHTTPError):
            NativeProxyClient().forward(
                "/v1/chat/completions",
                {"model": "downstream", "messages": [{"role": "user", "content": "hi"}]},
            )
        assert unavailable.calls == 1
        assert healthy.calls == 0
    finally:
        gateway.CONFIG_PATH = old_config
        reset_upstream_pool()
        unavailable.close()
        healthy.close()
