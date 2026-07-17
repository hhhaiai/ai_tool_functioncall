from __future__ import annotations

import base64
import io
from typing import Any, cast

import pytest

from src import gateway_http_io as http_io
from src.gateway_errors import BadRequestError, RequestBodyTooLargeError


class _Handler:
    def __init__(self, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.headers = dict(headers or {})
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.statuses: list[int] = []
        self.response_headers: list[tuple[str, str]] = []
        self.end_calls = 0

    def send_response(self, code: int, message: str | None = None) -> None:
        del message
        self.statuses.append(code)

    def send_header(self, keyword: str, value: str) -> None:
        self.response_headers.append((keyword, value))

    def end_headers(self) -> None:
        self.end_calls += 1


def _headers(handler: _Handler) -> dict[str, str]:
    return dict(handler.response_headers)


def test_json_response_uses_utf8_byte_length_and_custom_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cors_calls: list[Any] = []
    monkeypatch.setattr(
        http_io,
        "send_cors_headers",
        lambda handler: cors_calls.append(handler) or True,
    )
    handler = _Handler()
    http_io.json_response(
        handler,
        201,
        {"message": "你好"},
        headers={"X-Request-ID": "req-1"},
    )
    body = handler.wfile.getvalue()
    assert handler.statuses == [201]
    assert handler.end_calls == 1
    assert _headers(handler) == {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body)),
        "X-Request-ID": "req-1",
    }
    assert body.decode("utf-8") == '{"message": "你好"}'
    assert cors_calls == [handler]


def test_text_response_uses_encoded_length_and_default_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_io, "send_cors_headers", lambda _handler: True)
    handler = _Handler()
    http_io.text_response(handler, 200, "网关")
    body = "网关".encode("utf-8")
    assert handler.wfile.getvalue() == body
    assert _headers(handler)["Content-Length"] == str(len(body))
    assert _headers(handler)["Content-Type"] == "text/html; charset=utf-8"


def test_safe_json_response_falls_back_to_empty_500_on_serialization_failure() -> None:
    handler = _Handler()
    payload = cast(dict[str, Any], {"not_json": {1, 2}})
    http_io.safe_json_response(handler, 200, payload)
    assert handler.statuses == [500]
    assert handler.end_calls == 1
    assert handler.wfile.getvalue() == b""


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ({"max_request_body_bytes": 123}, 123),
        ({"max_request_body_bytes": 0}, 64 * 1024 * 1024),
        ({"max_request_body_bytes": -4}, 1),
        ({"max_request_body_bytes": "invalid"}, 64 * 1024 * 1024),
    ],
)
def test_request_body_limit_is_positive_and_tolerates_bad_config(
    configured: dict[str, Any],
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config

    monkeypatch.setattr(gateway_config, "_gateway_config", lambda: configured)
    assert http_io.request_body_limit() == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0), ("", 0), ("0", 0), ("-8", 0), ("12", 12)],
)
def test_request_content_length_normalization(raw: str | None, expected: int) -> None:
    headers = {} if raw is None else {"Content-Length": raw}
    assert http_io.request_content_length(_Handler(headers=headers)) == expected


def test_invalid_content_length_is_bad_request() -> None:
    with pytest.raises(BadRequestError, match="invalid Content-Length header"):
        http_io.request_content_length(_Handler(headers={"Content-Length": "nope"}))


def test_read_limited_body_rejects_oversize_without_consuming_input() -> None:
    handler = _Handler(b"abcdef", {"Content-Length": "6"})
    with pytest.raises(RequestBodyTooLargeError, match="6 bytes exceeds limit 5"):
        http_io.read_limited_body(handler, limit=5)
    assert handler.rfile.tell() == 0


def test_read_limited_body_rejects_short_read_with_size_detail() -> None:
    handler = _Handler(b"abc", {"Content-Length": "5"})
    with pytest.raises(BadRequestError, match="incomplete request body") as caught:
        http_io.read_limited_body(handler, limit=10)
    assert caught.value.detail == {"expected_bytes": 5, "received_bytes": 3}


def test_read_limited_body_accepts_exact_body_and_empty_body() -> None:
    assert http_io.read_limited_body(
        _Handler(b"abc", {"Content-Length": "3"}),
        limit=3,
    ) == b"abc"
    assert http_io.read_limited_body(_Handler()) == b""


def test_decode_json_object_contract() -> None:
    assert http_io.decode_json_object(b"") == {}
    assert http_io.decode_json_object(b'{"ok": true}') == {"ok": True}
    with pytest.raises(BadRequestError, match="invalid JSON"):
        http_io.decode_json_object(b'{"broken":')
    with pytest.raises(BadRequestError, match="invalid JSON"):
        http_io.decode_json_object(b"\xff")
    with pytest.raises(BadRequestError, match="must be an object"):
        http_io.decode_json_object(b"[]")
    with pytest.raises(BadRequestError, match="must be an object"):
        http_io.decode_json_object(b'"scalar"')


def test_read_json_combines_bounded_read_and_object_validation() -> None:
    body = b'{"value": 42}'
    handler = _Handler(body, {"Content-Length": str(len(body))})
    assert http_io.read_json(handler) == {"value": 42}


def test_decode_json_form_accepts_case_and_content_type_parameters() -> None:
    assert http_io.decode_form(
        b'{"count": 3, "enabled": true}',
        content_type="Application/JSON; Charset=UTF-8",
    ) == {"count": "3", "enabled": "True"}


def test_decode_json_form_rejects_malformed_non_object_and_invalid_utf8() -> None:
    with pytest.raises(BadRequestError, match="invalid JSON form"):
        http_io.decode_form(b'{"broken":', content_type="application/json")
    with pytest.raises(BadRequestError, match="must be an object"):
        http_io.decode_form(b"[]", content_type="application/json")
    with pytest.raises(BadRequestError, match="invalid form UTF-8"):
        http_io.decode_form(b"\xff", content_type="application/x-www-form-urlencoded")


def test_decode_urlencoded_form_preserves_first_duplicate_and_ignores_blank() -> None:
    assert http_io.decode_form(
        b"name=first&name=second&message=hello+world&blank=",
        content_type="application/x-www-form-urlencoded",
    ) == {"name": "first", "message": "hello world"}


def test_decode_urlencoded_form_bounds_field_count() -> None:
    with pytest.raises(BadRequestError, match="invalid form data"):
        http_io.decode_form(
            b"one=1&two=2&three=3",
            content_type="application/x-www-form-urlencoded",
            max_fields=2,
        )


def test_read_form_uses_bounded_body_and_content_type() -> None:
    body = b'{"action": "save"}'
    handler = _Handler(
        body,
        {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    assert http_io.read_form(handler) == {"action": "save"}


def test_basic_auth_parsing_preserves_colons_in_password() -> None:
    encoded = base64.b64encode("admin:part:two".encode("utf-8")).decode("ascii")
    assert http_io.parse_basic_auth(f"Basic {encoded}") == ("admin", "part:two")
    assert http_io.parse_basic_auth(None) is None
    assert http_io.parse_basic_auth("Bearer token") is None
    assert http_io.parse_basic_auth("Basic invalid%%%") is None
    no_colon = base64.b64encode(b"admin-only").decode("ascii")
    assert http_io.parse_basic_auth(f"Basic {no_colon}") is None


def test_constant_time_equal_handles_unicode_and_different_types() -> None:
    assert http_io.constant_time_equal("密钥", "密钥") is True
    assert http_io.constant_time_equal("密钥", "不同") is False
    assert http_io.constant_time_equal(123, "123") is True


def test_handler_and_facade_keep_legacy_http_io_exports() -> None:
    from src import gateway_app, gateway_http_handler

    assert gateway_http_handler._json_response is http_io._json_response
    assert gateway_http_handler._safe_json_response is http_io._safe_json_response
    assert gateway_http_handler._text_response is http_io._text_response
    assert gateway_http_handler._parse_basic_auth is http_io._parse_basic_auth
    assert gateway_http_handler._constant_time_equal is http_io._constant_time_equal
    assert gateway_app._json_response is http_io._json_response
    assert gateway_app._safe_json_response is http_io._safe_json_response
    assert gateway_app._text_response is http_io._text_response
    assert gateway_app._parse_basic_auth is http_io._parse_basic_auth
    assert gateway_app._read_json is gateway_http_handler._read_json


def test_handler_read_wrapper_preserves_body_limit_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_http_handler

    body = b'{"value": 42}'
    handler = _Handler(body, {"Content-Length": str(len(body))})
    monkeypatch.setattr(gateway_http_handler, "_request_body_limit", lambda: len(body) - 1)
    with pytest.raises(RequestBodyTooLargeError):
        gateway_http_handler._read_json(handler)


def test_handler_form_wrapper_preserves_body_limit_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_http_handler

    body = b"action=save"
    handler = _Handler(
        body,
        {
            "Content-Length": str(len(body)),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    monkeypatch.setattr(gateway_http_handler, "_request_body_limit", lambda: len(body) - 1)
    with pytest.raises(RequestBodyTooLargeError):
        gateway_http_handler._read_form(handler)

