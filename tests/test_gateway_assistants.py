from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import src.toolcall_gateway as gateway
from src.gateway_assistants import create_assistant_response, create_thread_response


def test_gateway_owned_assistant_create_response_defaults_model(monkeypatch):
    monkeypatch.setenv("GATEWAY_UPSTREAM_MODEL", "fallback-model")
    response = create_assistant_response({"name": "probe", "instructions": "hi"})
    assert response["object"] == "assistant"
    assert response["id"].startswith("asst_")
    assert response["model"]
    assert response["name"] == "probe"
    assert response["tools"] == []


def test_gateway_owned_thread_create_response_does_not_echo_message_content():
    response = create_thread_response({"messages": [{"role": "user", "content": "secret"}], "metadata": {"tenant": "a"}})
    assert response["object"] == "thread"
    assert response["id"].startswith("thread_")
    assert response["metadata"] == {"tenant": "a"}
    assert response["gateway_message_count"] == 1
    assert "secret" not in json.dumps(response)


def test_assistants_and_threads_http_endpoints_are_gateway_owned_not_forwarded(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        old_config = gateway.CONFIG_PATH
        old_proxy_client = gateway.NativeProxyClient
        gateway.CONFIG_PATH = pathlib.Path(td) / "config.json"
        gateway.save_config(gateway._default_config())

        class ExplodingClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError("assistants/threads must not construct upstream client")

        gateway.NativeProxyClient = ExplodingClient
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), gateway.GatewayHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            headers = {"authorization": "Bearer local-gateway-key", "content-type": "application/json"}
            for path, body, expected_object in [
                ("/v1/assistants", {"model": "m", "name": "probe"}, "assistant"),
                ("/v1/threads", {"messages": [{"role": "user", "content": "hi"}]}, "thread"),
            ]:
                req = urllib.request.Request(
                    base + path,
                    data=json.dumps(body).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    assert resp.status == 200
                    payload = json.loads(resp.read().decode("utf-8"))
                assert payload["object"] == expected_object
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            gateway.NativeProxyClient = old_proxy_client
            gateway.CONFIG_PATH = old_config


def test_workspace_metadata_key_overrides_service_env(monkeypatch):
    from src.gateway_tool_runtime import _request_workspace_root

    monkeypatch.setenv("GATEWAY_WORKSPACE_ROOT", "/service/workspace")
    body = {"metadata": {"workspace": "/client/workspace", "user_id": "tenant-a"}}
    assert str(_request_workspace_root(body)) == "/client/workspace"


def test_top_level_workspace_key_overrides_service_env(monkeypatch):
    from src.gateway_tool_runtime import _request_workspace_root

    monkeypatch.setenv("GATEWAY_WORKSPACE_ROOT", "/service/workspace")
    body = {"workspace": "/client/top-level"}
    assert str(_request_workspace_root(body)) == "/client/top-level"
