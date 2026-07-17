from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from src import gateway_config
from src.gateway_http_handler import GatewayHandler, _capability_contract
from src.gateway_observability import (
    OBSERVABILITY,
    ObservabilityRegistry,
    begin_request,
    end_request,
    normalize_route,
    observe_request,
    observe_tool,
    observe_upstream,
)
from src.gateway_proxy import NativeProxyClient, UpstreamSSEEvent
from src.gateway_tool_runtime import ToolCall, _execute_tool_call
from src import gateway_logging


def test_route_and_failure_labels_are_bounded() -> None:
    assert normalize_route("/v1/chat/completions?secret=yes") == "/v1/chat/completions"
    assert normalize_route("/admin/private-user-specific") == "/admin/*"
    assert normalize_route("/tenant/alice/secret") == "/other"

    registry = ObservabilityRegistry(trace_limit=10)
    for index in range(100):
        registry.observe(
            "gateway_test_duration_seconds",
            0.01,
            route=normalize_route(f"/tenant/private-{index}"),
            outcome="success",
        )
    text = registry.prometheus()
    assert "private-" not in text
    assert text.count('route="/other"') > 0


def test_trace_ring_is_bounded_and_drops_unapproved_attributes() -> None:
    registry = ObservabilityRegistry(trace_limit=10)
    request_id, token = begin_request("safe-request-id")
    try:
        for index in range(25):
            registry.trace(
                "tool",
                name="calculator",
                duration_seconds=0.01,
                outcome="success",
                attributes={
                    "tool_class": "builtin",
                    "prompt": "must-not-appear",
                    "api_key": "secret",
                    "event_count": index,
                },
            )
    finally:
        end_request(token)
    traces = registry.traces(100)
    assert len(traces) == 10
    assert all(item["request_id"] == request_id for item in traces)
    encoded = json.dumps(traces)
    assert "must-not-appear" not in encoded
    assert "secret" not in encoded
    assert traces[0]["attributes"]["event_count"] == 24


def test_request_tool_and_upstream_metrics_render_histograms() -> None:
    OBSERVABILITY.clear()
    request_id, token = begin_request("metric-request")
    try:
        observe_request("POST", "/v1/responses", 200, 0.2)
        observe_tool(
            "calculator",
            tool_class="builtin",
            success=True,
            failure_type="none",
            duration_seconds=0.01,
        )
        observe_tool(
            "private-mcp-name-for-user-a",
            tool_class="mcp",
            success=False,
            failure_type="connection exploded for tenant-a",
            duration_seconds=0.02,
        )
        observe_upstream(
            method="POST",
            path="/v1/responses",
            protocol="openai_responses",
            stream=True,
            success=True,
            failure_type="none",
            duration_seconds=0.3,
            first_event_seconds=0.05,
            event_count=3,
        )
    finally:
        end_request(token)
    text = OBSERVABILITY.prometheus()
    assert "gateway_http_request_duration_seconds_bucket" in text
    assert "gateway_tool_duration_seconds_bucket" in text
    assert "gateway_upstream_duration_seconds_bucket" in text
    assert "gateway_upstream_first_event_seconds_bucket" in text
    assert 'tool="mcp"' in text
    assert "private-mcp-name" not in text
    assert "tenant-a" not in text


def test_tool_execution_wrapper_observes_success_and_failure() -> None:
    OBSERVABILITY.clear()
    success = _execute_tool_call(
        ToolCall("call-ok", "calculator", {"expression": "20+22"}, {}),
        provider="observability-test",
    )
    failure = _execute_tool_call(
        ToolCall("call-missing", "private_user_function_123", {}, {}),
        provider="observability-test",
    )
    assert success.success is True
    assert failure.success is False
    text = OBSERVABILITY.prometheus()
    assert 'tool="calculator"' in text
    assert 'tool="unknown"' in text
    assert "private_user_function_123" not in text


def test_proxy_wrappers_observe_nonstream_and_first_stream_event(monkeypatch: pytest.MonkeyPatch) -> None:
    OBSERVABILITY.clear()
    client = NativeProxyClient.__new__(NativeProxyClient)
    client.protocol = "openai_chat"
    monkeypatch.setattr(client, "_do_request_impl", lambda method, path, body=None: {"ok": True})
    assert client._do_request("POST", "/v1/chat/completions", {}) == {"ok": True}

    def events(_path, _body):
        yield UpstreamSSEEvent(None, "one", 3)
        yield UpstreamSSEEvent(None, "two", 3)

    monkeypatch.setattr(client, "_stream_impl", events)
    assert [event.data for event in client.stream("/v1/chat/completions", {})] == ["one", "two"]
    text = OBSERVABILITY.prometheus()
    assert 'stream="false"' in text
    assert 'stream="true"' in text
    assert "gateway_upstream_first_event_seconds_count" in text


def test_request_id_propagates_to_upstream_headers_and_sqlite_log(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "requests.db"
    monkeypatch.setenv("GATEWAY_SQLITE_LOG_PATH", str(database))
    monkeypatch.setenv("GATEWAY_LOGGING_BACKEND", "sqlite")
    gateway_logging.SQLITE_READY = False
    request_id, token = begin_request("req_shared_trace_12345678")
    try:
        client = NativeProxyClient.__new__(NativeProxyClient)
        client.protocol = "openai_chat"
        client.api_key = "test-only-key"
        headers = client._headers()
        assert headers["x-request-id"] == request_id
        gateway_logging._write_request_log(
            "/v1/responses",
            {"model": "test"},
            200,
            {"ok": True},
            "client",
        )
    finally:
        end_request(token)
    with __import__("sqlite3").connect(database) as connection:
        stored = connection.execute("SELECT request_id FROM request_logs").fetchone()[0]
    assert stored == request_id


def test_http_request_id_metrics_and_trace_admin_contract(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    OBSERVABILITY.clear()
    old_config = gateway_config.CONFIG_PATH
    gateway_config.CONFIG_PATH = tmp_path / "config.json"
    monkeypatch.setenv("GATEWAY_CONCURRENCY_DB_PATH", str(tmp_path / "admission.db"))
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_DB_PATH", str(tmp_path / "rate.db"))
    gateway_config.save_config(gateway_config._default_config())
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    token = base64.b64encode(b"admin:admin").decode("ascii")
    try:
        request = urllib.request.Request(base + "/capabilities", headers={"x-request-id": "caller-request-42"})
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.headers["x-request-id"] == "caller-request-42"
            capabilities = json.loads(response.read())
        assert capabilities["observability"]["trace_persistence"] is False

        invalid = urllib.request.Request(base + "/capabilities", headers={"x-request-id": "bad id with spaces"})
        with urllib.request.urlopen(invalid, timeout=5) as response:
            replacement = response.headers["x-request-id"]
            response.read()
        assert replacement.startswith("req_")

        traces_request = urllib.request.Request(
            base + "/admin/traces.json?limit=20",
            headers={"authorization": f"Basic {token}"},
        )
        traces = json.loads(urllib.request.urlopen(traces_request, timeout=5).read())["traces"]
        assert any(item["request_id"] == "caller-request-42" for item in traces)
        encoded = json.dumps(traces)
        assert "bad id with spaces" not in encoded

        metrics_request = urllib.request.Request(
            base + "/admin/metrics",
            headers={"authorization": f"Basic {token}"},
        )
        metrics = urllib.request.urlopen(metrics_request, timeout=5).read().decode("utf-8")
        assert "gateway_http_request_duration_seconds_bucket" in metrics
        assert 'route="/capabilities"' in metrics
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_config.CONFIG_PATH = old_config


def test_traces_endpoint_requires_admin(tmp_path) -> None:
    old_config = gateway_config.CONFIG_PATH
    gateway_config.CONFIG_PATH = tmp_path / "config.json"
    gateway_config.save_config(gateway_config._default_config())
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/admin/traces.json",
                timeout=5,
            )
        assert denied.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_config.CONFIG_PATH = old_config
