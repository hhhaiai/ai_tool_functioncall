from __future__ import annotations

import subprocess
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from src.gateway_errors import UpstreamHTTPError
from src.gateway_errors import UpstreamTimeoutError
from src.gateway_proxy import NativeProxyClient


def _bounded_result(stdout="", stderr="", returncode=0, *, stdout_truncated=False, stderr_truncated=False):
    return SimpleNamespace(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        stdout_total_bytes=len(stdout.encode()),
        stderr_total_bytes=len(stderr.encode()),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def test_curl_upstream_http_error_preserves_status_and_detail(monkeypatch):
    """Regression: curl transport must not double-pass upstream_status."""

    def fake_run(*args, **kwargs):
        return _bounded_result('{"error":{"message":"not supported"}}\n__HTTP_CODE__404')

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/assistants", {}, b"{}")

    assert exc_info.value.upstream_status == 404
    assert exc_info.value.detail == {"error": {"message": "not supported"}}
    assert str(exc_info.value) == "upstream HTTP 404"


def test_curl_temp_payload_file_is_removed_on_timeout(monkeypatch, tmp_path):
    """curl transport must clean temp payload files even when subprocess times out."""

    payload_path = tmp_path / "payload.json"

    def fake_named_tempfile(*args, **kwargs):
        return payload_path.open("wb")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr("tempfile.NamedTemporaryFile", fake_named_tempfile)
    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamTimeoutError):
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b'{"hello":"world"}')

    assert not payload_path.exists()


def test_curl_nonzero_exit_is_upstream_connection_error(monkeypatch):
    def fake_run(*args, **kwargs):
        return _bounded_result("\n__HTTP_CODE__000", "Could not resolve host", 6)

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b"{}")

    assert exc_info.value.upstream_status == 502
    assert exc_info.value.detail["type"] == "curl_transport_error"
    assert exc_info.value.detail["curl_exit_code"] == 6
    assert "resolve host" in exc_info.value.detail["stderr"]


def test_curl_exit_28_maps_to_gateway_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        return _bounded_result("\n__HTTP_CODE__000", "operation timed out", 28)

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamTimeoutError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b"{}")

    assert exc_info.value.status == 504
    assert exc_info.value.detail["curl_exit_code"] == 28


def test_http_zero_is_never_treated_as_empty_success(monkeypatch):
    def fake_run(*args, **kwargs):
        return _bounded_result("\n__HTTP_CODE__000")

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b"{}")

    assert exc_info.value.upstream_status == 502
    assert exc_info.value.detail["type"] == "upstream_connection_failed"


def test_missing_http_status_marker_is_invalid_upstream_response(monkeypatch):
    def fake_run(*args, **kwargs):
        return _bounded_result('{"choices":[]}')

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b"{}")

    assert exc_info.value.detail["type"] == "missing_upstream_status_marker"


def test_empty_http_200_is_invalid_but_204_is_allowed(monkeypatch):
    responses = iter([
        _bounded_result("\n__HTTP_CODE__200"),
        _bounded_result("\n__HTTP_CODE__204"),
    ])
    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", lambda *args, **kwargs: next(responses))
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b"{}")
    assert exc_info.value.detail["type"] == "empty_upstream_response"

    assert client._do_request_once("DELETE", "http://upstream.local/resource", {}, None) == {}


def _retry_client() -> NativeProxyClient:
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")
    client.retry_max_attempts = 3
    client.retry_initial_delay = 0.01
    client.retry_max_delay = 1.0
    client.retry_max_elapsed = 30.0
    return client


def test_transient_503_retries_then_succeeds(monkeypatch):
    client = _retry_client()
    attempts = []
    sleeps = []

    def fake_once(*args, **kwargs):
        attempts.append(kwargs.get("timeout"))
        if len(attempts) < 3:
            raise UpstreamHTTPError(503, {"error": "busy"})
        return {"ok": True}

    monkeypatch.setattr(client, "_do_request_once", fake_once)
    monkeypatch.setattr(client, "_url", lambda path: "http://upstream.local/v1/chat/completions")
    monkeypatch.setattr("src.gateway_proxy.random.uniform", lambda low, high: 1.0)
    monkeypatch.setattr("src.gateway_proxy.time.sleep", lambda delay: sleeps.append(delay))

    assert client._do_request("POST", "/v1/chat/completions", {}) == {"ok": True}
    assert len(attempts) == 3
    assert sleeps == [0.01, 0.02]


def test_retry_after_header_controls_delay(monkeypatch):
    client = _retry_client()
    attempts = 0
    sleeps = []

    def fake_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise UpstreamHTTPError(429, {"error": "rate limited"}, headers={"retry-after": "2"})
        return {"ok": True}

    monkeypatch.setattr(client, "_do_request_once", fake_once)
    monkeypatch.setattr(client, "_url", lambda path: "http://upstream.local/v1/chat/completions")
    monkeypatch.setattr("src.gateway_proxy.time.sleep", lambda delay: sleeps.append(delay))

    assert client._do_request("POST", "/v1/chat/completions", {}) == {"ok": True}
    assert attempts == 2
    assert sleeps == [2.0]


def test_non_retryable_400_is_attempted_once(monkeypatch):
    client = _retry_client()
    attempts = 0

    def fake_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise UpstreamHTTPError(400, {"error": "bad request"})

    monkeypatch.setattr(client, "_do_request_once", fake_once)
    monkeypatch.setattr(client, "_url", lambda path: "http://upstream.local/v1/chat/completions")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request("POST", "/v1/chat/completions", {})

    assert exc_info.value.upstream_status == 400
    assert attempts == 1


def test_retry_budget_is_bounded_by_attempt_count(monkeypatch):
    client = _retry_client()
    attempts = 0

    def fake_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise UpstreamHTTPError(502, {"error": "transport"})

    monkeypatch.setattr(client, "_do_request_once", fake_once)
    monkeypatch.setattr(client, "_url", lambda path: "http://upstream.local/v1/chat/completions")
    monkeypatch.setattr("src.gateway_proxy.random.uniform", lambda low, high: 1.0)
    monkeypatch.setattr("src.gateway_proxy.time.sleep", lambda delay: None)

    with pytest.raises(UpstreamHTTPError):
        client._do_request("POST", "/v1/chat/completions", {})

    assert attempts == 3


def test_malformed_http_status_is_invalid_upstream_response(monkeypatch):
    def fake_run(*args, **kwargs):
        return _bounded_result('{"choices":[]}\n__HTTP_CODE__not-a-status')

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b"{}")

    assert exc_info.value.detail["type"] == "invalid_upstream_status"


def test_upstream_credentials_do_not_appear_in_curl_argv(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["input"] = kwargs.get("input_data")
        return _bounded_result('{"ok":true}\n__HTTP_CODE__200')

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(
        base_url="http://upstream.local",
        api_key="secret-upstream-value",
        model="test-model",
    )

    assert client._do_request_once(
        "POST",
        "http://upstream.local/v1/chat/completions",
        client._headers(),
        b"{}",
    ) == {"ok": True}
    assert "secret-upstream-value" not in " ".join(captured["cmd"])
    assert b"secret-upstream-value" in captured["input"]
    assert captured["cmd"][captured["cmd"].index("--config") + 1] == "-"


def test_retry_after_beyond_deadline_does_not_sleep_or_retry(monkeypatch):
    client = _retry_client()
    client.retry_max_elapsed = 0.25
    attempts = 0
    sleeps = []

    def fake_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise UpstreamHTTPError(429, {"error": "rate limited"}, headers={"retry-after": "30"})

    monkeypatch.setattr(client, "_do_request_once", fake_once)
    monkeypatch.setattr(client, "_url", lambda path: "http://upstream.local/v1/chat/completions")
    monkeypatch.setattr("src.gateway_proxy.time.sleep", lambda delay: sleeps.append(delay))

    with pytest.raises(UpstreamHTTPError):
        client._do_request("POST", "/v1/chat/completions", {})

    assert attempts == 1
    assert sleeps == []


def test_curl_output_limit_is_reported_before_response_parsing(monkeypatch):
    def fake_run(*args, **kwargs):
        return _bounded_result(
            "A" * 5000,
            returncode=63,
            stdout_truncated=True,
        )

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")
    client.max_response_bytes = 4096

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b"{}")

    assert exc_info.value.upstream_status == 502
    assert exc_info.value.detail["type"] == "upstream_response_too_large"
    assert exc_info.value.detail["max_bytes"] == 4096


def test_curl_stderr_truncation_is_disclosed(monkeypatch):
    def fake_run(*args, **kwargs):
        return _bounded_result(
            "\n__HTTP_CODE__000",
            "head\n[gateway: truncated 999 bytes]\ntail",
            returncode=6,
            stderr_truncated=True,
        )

    monkeypatch.setattr("src.gateway_proxy.run_bounded_process", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/chat/completions", {}, b"{}")

    assert exc_info.value.detail["stderr_truncated"] is True
    assert "tail" in exc_info.value.detail["stderr"]


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is required by the production transport")
def test_real_curl_rejects_oversized_upstream_body():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = b'{"text":"' + (b"X" * 20_000) + b'"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = NativeProxyClient(
            base_url=f"http://127.0.0.1:{server.server_address[1]}",
            api_key="",
            model="test-model",
        )
        client.max_response_bytes = 4096
        with pytest.raises(UpstreamHTTPError) as exc_info:
            client._do_request_once("POST", client.base_url + "/v1/chat/completions", {}, b"{}")
        assert exc_info.value.detail["type"] == "upstream_response_too_large"
        assert exc_info.value.detail["max_bytes"] == 4096
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
