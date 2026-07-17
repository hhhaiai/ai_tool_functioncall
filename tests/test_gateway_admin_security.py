from __future__ import annotations

from typing import Any

import pytest

from src import gateway_admin_security as admin_security
from src import gateway_config


class _Handler:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = dict(headers or {})


class _Responses:
    def __init__(self) -> None:
        self.items: list[tuple[Any, int, dict[str, Any]]] = []

    def json_response(
        self,
        handler: Any,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        self.items.append((handler, status, payload))


def _error_payload(message: str) -> dict[str, Any]:
    return {"error": {"message": message}}


def test_request_origin_uses_host_and_forwarded_scheme() -> None:
    handler = _Handler(
        {
            "Host": "gateway.example.com:8443",
            "X-Forwarded-Proto": "HTTPS, http",
        }
    )
    assert admin_security.request_origin(handler) == "https://gateway.example.com:8443"
    assert admin_security.request_origin(_Handler()) is None


def test_request_origin_ignores_untrusted_forwarded_host() -> None:
    handler = _Handler(
        {
            "Host": "gateway.example.com",
            "X-Forwarded-Host": "attacker.example",
            "X-Forwarded-Proto": "https",
        }
    )
    assert admin_security.request_origin(handler) == "https://gateway.example.com"


def test_cli_admin_write_without_origin_or_referer_is_allowed() -> None:
    responses = _Responses()
    handler = _Handler({"Host": "gateway.example.com"})
    assert admin_security.check_admin_origin(
        handler,
        {},
        json_response=responses.json_response,
        error_payload=_error_payload,
    ) is True
    assert responses.items == []


@pytest.mark.parametrize("source", ["null", "http://127.0.0.1:bad-port", "://broken"])
def test_malformed_browser_origin_is_rejected(source: str) -> None:
    responses = _Responses()
    handler = _Handler({"Host": "gateway.example.com", "Origin": source})
    assert admin_security.check_admin_origin(
        handler,
        {},
        json_response=responses.json_response,
        error_payload=_error_payload,
    ) is False
    assert responses.items == [
        (handler, 403, {"error": {"message": "cross-origin admin request rejected"}})
    ]


def test_same_request_origin_is_allowed() -> None:
    responses = _Responses()
    handler = _Handler(
        {
            "Host": "gateway.example.com",
            "X-Forwarded-Proto": "https",
            "Origin": "https://gateway.example.com",
        }
    )
    assert admin_security.check_admin_origin(
        handler,
        {},
        json_response=responses.json_response,
        error_payload=_error_payload,
    ) is True
    assert responses.items == []


def test_configured_public_base_url_allows_referer_with_path() -> None:
    responses = _Responses()
    handler = _Handler(
        {
            "Host": "gateway-internal:8885",
            "Referer": "https://console.example.com/ui/config?tab=security",
        }
    )
    config = {"gateway": {"public_base_url": "https://console.example.com/base/path"}}
    assert admin_security.check_admin_origin(
        handler,
        config,
        json_response=responses.json_response,
        error_payload=_error_payload,
    ) is True


def test_origin_takes_precedence_over_same_origin_referer() -> None:
    responses = _Responses()
    handler = _Handler(
        {
            "Host": "gateway.example.com",
            "X-Forwarded-Proto": "https",
            "Origin": "https://evil.example",
            "Referer": "https://gateway.example.com/ui",
        }
    )
    assert admin_security.check_admin_origin(
        handler,
        {},
        json_response=responses.json_response,
        error_payload=_error_payload,
    ) is False


def test_spoofed_forwarded_host_cannot_make_cross_origin_write_same_origin() -> None:
    responses = _Responses()
    handler = _Handler(
        {
            "Host": "gateway.example.com",
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://evil.example",
        }
    )
    assert admin_security.check_admin_origin(
        handler,
        {},
        json_response=responses.json_response,
        error_payload=_error_payload,
    ) is False


def test_admin_write_short_circuits_before_loading_config_when_auth_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gateway_config,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("config must not load")),
    )
    handler = _Handler()
    origin_calls: list[Any] = []
    assert admin_security.check_admin_write(
        handler,
        check_admin=lambda _handler: False,
        check_origin=lambda received, config: origin_calls.append((received, config)) or True,
    ) is False
    assert origin_calls == []


def test_admin_write_loads_current_config_after_successful_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {"gateway": {"public_base_url": "https://gateway.example.com"}}
    monkeypatch.setattr(gateway_config, "load_config", lambda: config)
    handler = _Handler()
    seen: list[tuple[Any, dict[str, Any]]] = []
    assert admin_security.check_admin_write(
        handler,
        check_admin=lambda received: received is handler,
        check_origin=lambda received, loaded: seen.append((received, loaded)) or True,
    ) is True
    assert seen == [(handler, config)]


def test_handler_keeps_admin_security_compatibility_entrypoints() -> None:
    from src import gateway_http_handler

    assert gateway_http_handler._request_origin is admin_security._request_origin
    assert gateway_http_handler._url_origin is admin_security._url_origin

