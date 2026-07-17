from __future__ import annotations

import http.client
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from src import gateway_config
from src.gateway_errors import ConfigError
from src.gateway_http_handler import GatewayHandler
from src.gateway_http_security import (
    is_loopback_host,
    normalize_origin,
    public_exposure_mode,
    validate_bind_security,
)
from src.gateway_streaming import _send_sse_headers


def _secure_config() -> dict:
    cfg = gateway_config._default_config()
    cfg["admin"] = {
        "username": "admin",
        "password_hash": gateway_config._hash_password("not-the-default", iterations=1_000),
        "must_change_password": False,
    }
    cfg["downstream_keys"] = [{"id": "client", "key_hash": "sha256-value", "enabled": True}]
    return cfg


def test_listener_exposure_contract_fails_closed():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert public_exposure_mode("127.0.0.1", "auto") == "private"
    assert public_exposure_mode("0.0.0.0", "auto") == "external"

    weak = gateway_config._default_config()
    with pytest.raises(ConfigError, match="default Admin password"):
        validate_bind_security("0.0.0.0", weak, exposure="external")

    no_key = _secure_config()
    no_key["downstream_keys"] = []
    with pytest.raises(ConfigError, match="downstream API key"):
        validate_bind_security("0.0.0.0", no_key, exposure="external")

    assert validate_bind_security("0.0.0.0", _secure_config(), exposure="external") == "external"
    assert validate_bind_security("0.0.0.0", weak, exposure="private") == "private"
    with pytest.raises(ConfigError, match="invalid GATEWAY_PUBLIC_EXPOSURE"):
        validate_bind_security("127.0.0.1", weak, exposure="public-ish")


def test_origin_normalization_is_exact_and_http_only():
    assert normalize_origin("HTTPS://Console.Example.com:443/path?q=1") == "https://console.example.com"
    assert normalize_origin("http://localhost:3000/path") == "http://localhost:3000"
    assert normalize_origin("http://[::1]:8885/path") == "http://[::1]:8885"
    assert normalize_origin("file:///tmp/test") is None
    assert normalize_origin("*") is None


def _start_gateway(tmp_path, *, cors_enabled: bool, allowed_origins: list[str]):
    original_path = gateway_config.CONFIG_PATH
    gateway_config.CONFIG_PATH = tmp_path / "gateway.json"
    cfg = gateway_config._default_config()
    cfg["downstream_keys"] = []
    cfg["gateway"]["cors_enabled"] = cors_enabled
    cfg["gateway"]["cors_allowed_origins"] = allowed_origins
    gateway_config.save_config(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return original_path, server, thread


def _stop_gateway(original_path, server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    gateway_config.CONFIG_PATH = original_path


def test_cors_disabled_and_exact_allowlist_behavior(tmp_path):
    original_path, server, thread = _start_gateway(
        tmp_path,
        cors_enabled=False,
        allowed_origins=["https://console.example.com"],
    )
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        disabled = urllib.request.Request(base_url + "/capabilities", headers={"Origin": "https://console.example.com"})
        with urllib.request.urlopen(disabled, timeout=5) as response:
            assert response.headers.get("Access-Control-Allow-Origin") is None
            assert response.headers.get("Vary") == "Origin"

        cfg = gateway_config.load_config()
        cfg["gateway"]["cors_enabled"] = True
        gateway_config.save_config(cfg)

        allowed = urllib.request.Request(base_url + "/capabilities", headers={"Origin": "https://console.example.com"})
        with urllib.request.urlopen(allowed, timeout=5) as response:
            assert response.headers["Access-Control-Allow-Origin"] == "https://console.example.com"
            assert response.headers["Vary"] == "Origin"

        disallowed = urllib.request.Request(base_url + "/capabilities", headers={"Origin": "https://attacker.example"})
        with urllib.request.urlopen(disallowed, timeout=5) as response:
            assert response.headers.get("Access-Control-Allow-Origin") is None
            assert response.headers.get("Vary") == "Origin"
    finally:
        _stop_gateway(original_path, server, thread)


def test_cors_preflight_and_sse_share_policy(tmp_path):
    original_path, server, thread = _start_gateway(
        tmp_path,
        cors_enabled=True,
        allowed_origins=["https://console.example.com"],
    )
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        connection.request(
            "OPTIONS",
            "/v1/responses",
            headers={
                "Origin": "https://console.example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 204
        assert response.getheader("Access-Control-Allow-Origin") == "https://console.example.com"
        assert "X-API-Key" in response.getheader("Access-Control-Allow-Headers")
        connection.close()

        denied = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        denied.request("OPTIONS", "/v1/responses", headers={"Origin": "https://attacker.example"})
        denied_response = denied.getresponse()
        denied_response.read()
        assert denied_response.status == 403
        assert denied_response.getheader("Access-Control-Allow-Origin") is None
        denied.close()

        invalid = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        invalid.request("OPTIONS", "/v1/responses", headers={"Origin": "null"})
        invalid_response = invalid.getresponse()
        invalid_response.read()
        assert invalid_response.status == 403
        invalid.close()

        class FakeHandler:
            headers = {"Origin": "https://console.example.com"}

            def __init__(self):
                self.status = None
                self.sent_headers = {}
                self.ended = False

            def send_response(self, status):
                self.status = status

            def send_header(self, key, value):
                self.sent_headers[key] = value

            def end_headers(self):
                self.ended = True

        fake = FakeHandler()
        _send_sse_headers(fake)
        assert fake.status == 200
        assert fake.sent_headers["Access-Control-Allow-Origin"] == "https://console.example.com"
        assert fake.sent_headers["Vary"] == "Origin"
        assert fake.ended
    finally:
        _stop_gateway(original_path, server, thread)
