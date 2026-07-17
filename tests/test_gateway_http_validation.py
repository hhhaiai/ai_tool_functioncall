from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from src import gateway_config
from src.gateway_http_handler import GatewayHandler, _semantic_cache_request_fingerprint


def _start_gateway(tmp_path):
    gateway_config.CONFIG_PATH = tmp_path / "gateway.json"
    cfg = gateway_config._default_config()
    cfg["downstream_keys"] = []
    cfg["cache"]["enabled"] = False
    gateway_config.save_config(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _post_raw(base_url: str, payload: bytes):
    request = urllib.request.Request(
        base_url + "/v1/chat/completions",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))
    raise AssertionError("invalid request unexpectedly succeeded")


def test_malformed_and_non_object_json_return_http_400(tmp_path):
    original_path = gateway_config.CONFIG_PATH
    server, thread = _start_gateway(tmp_path)
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        malformed_status, malformed = _post_raw(base_url, b'{"model":')
        array_status, array = _post_raw(base_url, b"[]")
        scalar_status, scalar = _post_raw(base_url, b'"hello"')

        assert malformed_status == 400
        assert array_status == 400
        assert scalar_status == 400
        assert "invalid JSON" in malformed["error"]["message"]
        assert "must be an object" in array["error"]["message"]
        assert "must be an object" in scalar["error"]["message"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_config.CONFIG_PATH = original_path


def test_invalid_content_length_returns_http_400(tmp_path):
    original_path = gateway_config.CONFIG_PATH
    server, thread = _start_gateway(tmp_path)
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        connection.putrequest("POST", "/v1/chat/completions")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "not-a-number")
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))

        assert response.status == 400
        assert payload["error"]["message"] == "invalid Content-Length header"
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_config.CONFIG_PATH = original_path


def test_package_mode_marketplace_imports_resolve():
    from src import gateway_http_handler, gateway_tool_runtime, marketplace

    assert callable(marketplace.list_mcp_marketplace)
    assert gateway_http_handler.__package__ == "src"
    assert gateway_tool_runtime._get_marketplace() is marketplace.list_mcp_marketplace


def test_semantic_cache_fingerprint_is_canonical_and_response_sensitive():
    first = {
        "model": "model-a",
        "stream": False,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "system-a"},
            {"role": "user", "content": "continue"},
        ],
    }
    reordered = {
        "messages": [
            {"content": "system-a", "role": "system"},
            {"content": "continue", "role": "user"},
        ],
        "temperature": 0,
        "stream": True,
        "model": "model-a",
    }

    baseline = _semantic_cache_request_fingerprint("/v1/chat/completions", first)
    assert baseline == _semantic_cache_request_fingerprint("/v1/chat/completions", reordered)

    variants = [
        {**first, "model": "model-b"},
        {**first, "temperature": 0.8},
        {**first, "messages": [{"role": "system", "content": "system-b"}, first["messages"][1]]},
        {**first, "messages": [{"role": "assistant", "content": "different history"}, first["messages"][1]]},
    ]
    for variant in variants:
        assert _semantic_cache_request_fingerprint("/v1/chat/completions", variant) != baseline

    assert _semantic_cache_request_fingerprint("/v1/responses", first) != baseline
