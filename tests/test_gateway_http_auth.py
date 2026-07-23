from __future__ import annotations

import base64
from typing import Any

import pytest

from src import gateway_config
from src import gateway_http_auth as http_auth
from src.gateway_errors import DownstreamAuthError


class _Handler:
    def __init__(
        self,
        path: str = "/admin/metrics",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.path = path
        self.headers = dict(headers or {})
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


def _basic(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def test_admin_config_load_failure_delegates_to_handler_error_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = RuntimeError("config marker")
    monkeypatch.setattr(
        gateway_config,
        "load_config_with_revision",
        lambda: (_ for _ in ()).throw(marker),
    )
    handler = _Handler("/admin/metrics?query=1")
    seen: list[tuple[Any, str, Exception]] = []
    assert http_auth.check_admin(
        handler,
        handle_error=lambda received, path, exc: seen.append((received, path, exc)),
    ) is False
    assert seen == [(handler, "/admin/metrics", marker)]
    assert handler.statuses == []


def test_valid_admin_credentials_without_upgrade_do_not_write_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = {"admin": {"username": "operator", "password_hash": "current-hash"}}
    monkeypatch.setattr(gateway_config, "load_config_with_revision", lambda: (cfg, "revision-1"))
    monkeypatch.setattr(
        gateway_config,
        "_verify_password",
        lambda password, encoded: password == "secret" and encoded == "current-hash",
    )
    monkeypatch.setattr(gateway_config, "_password_hash_needs_upgrade", lambda _encoded: False)
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected save")),
    )
    handler = _Handler(headers={"Authorization": _basic("operator", "secret")})
    assert http_auth.check_admin(handler, handle_error=lambda *_args: None) is True
    assert handler.statuses == []


def test_valid_legacy_admin_hash_is_upgraded_with_loaded_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = {"admin": {"username": "admin", "password_hash": "legacy-hash"}}
    saved: list[tuple[dict[str, Any], str | None]] = []
    monkeypatch.setattr(gateway_config, "load_config_with_revision", lambda: (cfg, "revision-2"))
    monkeypatch.setattr(gateway_config, "_verify_password", lambda password, encoded: password == "secret" and encoded == "legacy-hash")
    monkeypatch.setattr(gateway_config, "_password_hash_needs_upgrade", lambda encoded: encoded == "legacy-hash")
    monkeypatch.setattr(gateway_config, "_hash_password", lambda password: f"upgraded:{password}")
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda received, *, expected_revision=None: saved.append((received, expected_revision)),
    )
    handler = _Handler(headers={"Authorization": _basic("admin", "secret")})
    assert http_auth.check_admin(handler, handle_error=lambda *_args: None) is True
    assert cfg["admin"]["password_hash"] == "upgraded:secret"
    assert saved == [(cfg, "revision-2")]


def test_hash_upgrade_save_conflict_does_not_reject_verified_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = {"admin": {"username": "admin", "password_hash": "legacy"}}
    monkeypatch.setattr(gateway_config, "load_config_with_revision", lambda: (cfg, "revision"))
    monkeypatch.setattr(gateway_config, "_verify_password", lambda *_args: True)
    monkeypatch.setattr(gateway_config, "_password_hash_needs_upgrade", lambda _encoded: True)
    monkeypatch.setattr(gateway_config, "_hash_password", lambda _password: "new-hash")
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("conflict")),
    )
    handler = _Handler(headers={"Authorization": _basic("admin", "secret")})
    assert http_auth.check_admin(handler, handle_error=lambda *_args: None) is True
    assert handler.statuses == []


@pytest.mark.parametrize(
    "cfg",
    [
        {"admin": "malformed"},
        {"admin": {"username": "admin", "password_hash": "hash"}},
    ],
)
def test_invalid_admin_credentials_fail_closed_with_basic_challenge(
    cfg: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cors_calls: list[Any] = []
    monkeypatch.setattr(gateway_config, "load_config_with_revision", lambda: (cfg, "revision"))
    monkeypatch.setattr(gateway_config, "_verify_password", lambda *_args: False)
    monkeypatch.setattr(
        http_auth,
        "send_cors_headers",
        lambda handler: cors_calls.append(handler) or True,
    )
    handler = _Handler(headers={"Authorization": _basic("admin", "wrong")})
    assert http_auth.check_admin(handler, handle_error=lambda *_args: None) is False
    assert handler.statuses == [401]
    assert handler.response_headers == [("WWW-Authenticate", 'Basic realm="Gateway Admin"')]
    assert handler.end_calls == 1
    assert cors_calls == [handler]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/v1/models", "models"),
        ("/v1/chat/completions", "chat_completions"),
        ("/v1/responses", "responses"),
        ("/anthropic/v1/messages", "messages"),
        ("/v1/tools/call", "direct_tools"),
        ("/v1/functions/call", "direct_tools"),
        ("/v1/assistants", "assistants"),
        ("/v1/threads/thread_1/messages", "assistants"),
        ("/v1/web2api", "web2api"),
    ],
)
def test_downstream_route_contract(path: str, expected: str) -> None:
    assert http_auth.downstream_route(path) == expected


def _install_downstream_config(
    monkeypatch: pytest.MonkeyPatch,
    entries: Any,
) -> None:
    monkeypatch.setattr(gateway_config, "load_config", lambda: {"downstream_keys": entries})


def _entry(
    key: str,
    *,
    client_id: str = "client-stable-id",
    name: str = "client-name",
    enabled: bool = True,
    protocols: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": client_id,
        "name": name,
        "enabled": enabled,
        "key_hash": gateway_config._hash_secret(key),
        "protocols": protocols or [],
    }


def test_no_configured_downstream_keys_disables_client_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_downstream_config(monkeypatch, [])
    assert http_auth.check_downstream_key(_Handler("/v1/models")) is None


def test_malformed_downstream_key_config_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_downstream_config(monkeypatch, {"unexpected": "mapping"})
    with pytest.raises(DownstreamAuthError, match="invalid downstream key configuration"):
        http_auth.check_downstream_key(_Handler("/v1/models"))


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer secret-key"},
        {"authorization": "Bearer secret-key"},
        {"Authorization": _basic("ignored-user", "secret-key")},
        {"x-api-key": "secret-key"},
        {"X-API-Key": "secret-key"},
    ],
)
def test_supported_downstream_credential_headers_return_stable_id(
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_downstream_config(monkeypatch, [_entry("secret-key")])
    assert http_auth.check_downstream_key(_Handler("/v1/responses", headers)) == "client-stable-id"


def test_downstream_identity_falls_back_to_name_then_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    named = _entry("named-key", client_id="", name="legacy-name")
    unknown = _entry("unknown-key", client_id="", name="")
    _install_downstream_config(monkeypatch, [named, unknown])
    assert http_auth.check_downstream_key(
        _Handler("/v1/models", {"Authorization": "Bearer named-key"})
    ) == "legacy-name"
    assert http_auth.check_downstream_key(
        _Handler("/v1/models", {"Authorization": "Bearer unknown-key"})
    ) == "unknown"


def test_models_route_is_compatible_with_conversation_protocol_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_downstream_config(
        monkeypatch,
        [_entry("chat-key", protocols=["chat_completions"])],
    )
    assert http_auth.check_downstream_key(
        _Handler("/v1/models", {"Authorization": "Bearer chat-key"})
    ) == "client-stable-id"


def test_assistants_route_accepts_explicit_or_conversation_protocol_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_downstream_config(
        monkeypatch,
        [
            _entry("assistant-key", protocols=["assistants"]),
            _entry("messages-key", protocols=["messages"], client_id="messages-client"),
        ],
    )
    assert http_auth.check_downstream_key(
        _Handler("/v1/assistants", {"Authorization": "Bearer assistant-key"})
    ) == "client-stable-id"
    assert http_auth.check_downstream_key(
        _Handler("/v1/threads/thread_1/messages", {"Authorization": "Bearer messages-key"})
    ) == "messages-client"


def test_web2api_route_accepts_explicit_or_direct_tool_protocol_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_downstream_config(
        monkeypatch,
        [
            _entry("web-key", protocols=["web2api"]),
            _entry("tool-key", protocols=["direct_tools"], client_id="tool-client"),
        ],
    )
    assert http_auth.check_downstream_key(
        _Handler("/v1/web2api", {"Authorization": "Bearer web-key"})
    ) == "client-stable-id"
    assert http_auth.check_downstream_key(
        _Handler("/api/web2api", {"Authorization": "Bearer tool-key"})
    ) == "tool-client"


def test_protocol_acl_denies_wrong_route_after_valid_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_downstream_config(
        monkeypatch,
        [_entry("chat-key", protocols=["chat_completions"])],
    )
    with pytest.raises(DownstreamAuthError, match="not allowed for responses"):
        http_auth.check_downstream_key(
            _Handler("/v1/responses", {"Authorization": "Bearer chat-key"})
        )


def test_missing_invalid_and_disabled_downstream_keys_are_distinct_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_downstream_config(
        monkeypatch,
        [_entry("disabled-key", enabled=False), _entry("valid-key")],
    )
    with pytest.raises(DownstreamAuthError, match="missing Authorization"):
        http_auth.check_downstream_key(_Handler("/v1/models"))
    with pytest.raises(DownstreamAuthError, match="invalid API key"):
        http_auth.check_downstream_key(
            _Handler("/v1/models", {"Authorization": "Bearer wrong-key"})
        )
    with pytest.raises(DownstreamAuthError, match="invalid API key"):
        http_auth.check_downstream_key(
            _Handler("/v1/models", {"Authorization": "Bearer disabled-key"})
        )


def test_handler_and_facade_keep_authentication_entrypoints() -> None:
    from src import gateway_app, gateway_http_handler

    assert gateway_app._check_admin is gateway_http_handler._check_admin
    assert gateway_app._check_downstream_key is gateway_http_handler._check_downstream_key
