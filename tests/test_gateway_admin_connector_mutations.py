from __future__ import annotations

import copy
from typing import Any

import pytest

from src import gateway_admin_connector_mutations as mutations
from src import gateway_config, gateway_http_actions


def _profile(profile_id: str, *, api_key: str, protocol: str = "anthropic_messages") -> dict[str, Any]:
    return {
        "id": profile_id,
        "name": profile_id,
        "base_url": f"https://{profile_id}.example.com",
        "api_key": api_key,
        "model": f"{profile_id}-model",
        "protocol": protocol,
        "tools_enabled": "adapter",
        "timeout_seconds": 60.0,
        "max_input_tokens": 1000,
        "max_output_tokens": 100,
        "max_concurrency": 4,
        "paths": {
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
            "responses": "/v1/responses",
            "messages": "/v1/messages",
        },
        "capabilities": {"supports_streaming": True},
    }


def _base_config() -> dict[str, Any]:
    active = _profile("active", api_key="active-secret")
    backup = _profile("backup", api_key="backup-secret", protocol="openai_chat")
    return {
        "mcp": {"servers": [{"name": "existing-mcp", "command": ["existing"]}]},
        "http_actions": {
            "enabled": True,
            "actions": [{"name": "existing-action", "url": "https://example.com", "method": "GET"}],
        },
        "upstream_profiles": [active, backup],
        "active_upstream_id": "active",
        "active_upstream": "active",
        "upstream": copy.deepcopy(active),
    }


def _capture_saves(monkeypatch: pytest.MonkeyPatch, events: list[str] | None = None):
    saved: list[tuple[dict[str, Any], str | None]] = []

    def save(config, *, expected_revision=None):
        if events is not None:
            events.append("save")
        saved.append((copy.deepcopy(config), expected_revision))

    monkeypatch.setattr(gateway_config, "save_config", save)
    return saved


def test_unmatched_connector_path_has_no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    reloads: list[str] = []
    result = mutations.apply_admin_connector_mutation(
        "/admin/password",
        config,
        "revision",
        {},
        reload_mcp=lambda: reloads.append("reload"),
    )
    assert result.matched is False
    assert config == original
    assert saved == []
    assert reloads == []


def test_mcp_add_saves_before_reloading_and_parses_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    saved = _capture_saves(monkeypatch, events)
    config = _base_config()
    original = copy.deepcopy(config)
    result = mutations.apply_admin_connector_mutation(
        "/admin/mcp",
        config,
        "revision-mcp",
        {"action": "add", "name": "filesystem", "command": "npx -y '@mcp/server' --flag"},
        reload_mcp=lambda: events.append("reload"),
    )
    assert result.success is True
    assert events == ["save", "reload"]
    assert config == original
    candidate, revision = saved[0]
    assert revision == "revision-mcp"
    assert candidate["mcp"]["servers"][-1] == {
        "name": "filesystem",
        "command": ["npx", "-y", "@mcp/server", "--flag"],
        "enabled": True,
    }


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        ({"action": "add", "name": "existing-mcp", "command": "other"}, 409, "name already exists"),
        ({"action": "add", "name": "new", "command": "'unterminated"}, 400, "invalid MCP command"),
        ({"action": "add", "name": "new", "command": "''"}, 400, "invalid MCP command"),
        ({"action": "add", "name": "", "command": "npx"}, 400, "missing name or command"),
        ({"action": "delete", "name": ""}, 400, "missing name"),
        ({"action": "disable", "name": "existing-mcp"}, 400, "invalid MCP action"),
    ],
)
def test_invalid_mcp_mutations_do_not_save_or_reload(
    form: dict[str, str],
    status: int,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    reloads: list[str] = []
    result = mutations.apply_admin_connector_mutation(
        "/admin/mcp",
        _base_config(),
        "revision",
        form,
        reload_mcp=lambda: reloads.append("reload"),
    )
    assert result.status == status
    assert message in result.error
    assert saved == []
    assert reloads == []


def test_mcp_delete_is_idempotent_and_reload_endpoint_does_not_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    saved = _capture_saves(monkeypatch, events)
    result = mutations.apply_admin_connector_mutation(
        "/admin/mcp",
        _base_config(),
        "revision",
        {"action": "delete", "name": "missing"},
        reload_mcp=lambda: events.append("reload"),
    )
    assert result.success is True
    assert events == ["save", "reload"]
    events.clear()
    saved.clear()
    result = mutations.apply_admin_connector_mutation(
        "/admin/mcp-reload",
        _base_config(),
        "revision",
        {},
        reload_mcp=lambda: events.append("reload"),
    )
    assert result.success is True
    assert events == ["reload"]
    assert saved == []


def test_mcp_save_failure_never_reloads_or_mutates_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("save marker")),
    )
    reloads: list[str] = []
    with pytest.raises(RuntimeError, match="save marker"):
        mutations.apply_admin_connector_mutation(
            "/admin/mcp",
            config,
            "revision",
            {"action": "add", "name": "new", "command": "npx server"},
            reload_mcp=lambda: reloads.append("reload"),
        )
    assert config == original
    assert reloads == []


def test_http_action_add_validates_without_dns_and_persists_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gateway_http_actions,
        "_validate_action_dns_targets",
        lambda *_args: (_ for _ in ()).throw(AssertionError("DNS must not run")),
    )
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_connector_mutation(
        "/admin/http-actions",
        _base_config(),
        "revision-http",
        {
            "action": "add",
            "name": "search",
            "url": "https://api.example.test/search",
            "method": "post",
            "description": "Search service",
        },
    )
    assert result.success is True
    action = saved[0][0]["http_actions"]["actions"][-1]
    assert action == {
        "name": "search",
        "url": "https://api.example.test/search",
        "method": "POST",
        "description": "Search service",
        "enabled": True,
        "allow_private_network": False,
    }


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        ({"action": "add", "name": "existing-action", "url": "https://example.org"}, 409, "name already exists"),
        ({"action": "add", "name": "new", "url": "ftp://example.org"}, 400, "absolute http(s)"),
        ({"action": "add", "name": "new", "url": "http://127.0.0.1"}, 400, "private network"),
        ({"action": "add", "name": "new", "url": "https://example.org", "method": "TRACE"}, 400, "invalid HTTP Action method"),
        ({"action": "delete", "name": ""}, 400, "missing name"),
        ({"action": "disable", "name": "existing-action"}, 400, "invalid HTTP Action action"),
    ],
)
def test_invalid_http_action_mutations_do_not_save(
    form: dict[str, str],
    status: int,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_connector_mutation(
        "/admin/http-actions",
        _base_config(),
        "revision",
        form,
    )
    assert result.status == status
    assert message in result.error
    assert saved == []


def test_private_http_action_requires_explicit_admin_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_connector_mutation(
        "/admin/http-actions",
        _base_config(),
        "revision",
        {
            "action": "add",
            "name": "local-approved",
            "url": "http://127.0.0.1:9000/hook",
            "allow_private_network": "on",
        },
    )
    assert result.success is True
    assert saved[0][0]["http_actions"]["actions"][-1]["allow_private_network"] is True


def test_editing_active_profile_preserves_secret_and_updates_active_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_connector_mutation(
        "/admin/upstream-profile",
        _base_config(),
        "revision-profile",
        {
            "action": "save",
            "id": "active",
            "name": "active-renamed",
            "model": "new-model",
            # API key, protocol, and tools mode deliberately omitted.
        },
    )
    assert result.success is True
    candidate = saved[0][0]
    profile = next(item for item in candidate["upstream_profiles"] if item["id"] == "active")
    assert profile["api_key"] == "active-secret"
    assert profile["protocol"] == "anthropic_messages"
    assert profile["tools_enabled"] == "adapter"
    assert profile["model"] == "new-model"
    assert candidate["active_upstream_id"] == "active"
    assert candidate["active_upstream"] == "active"
    assert candidate["upstream"] == profile


def test_editing_inactive_profile_does_not_replace_active_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    original = _base_config()
    result = mutations.apply_admin_connector_mutation(
        "/admin/upstream-profile",
        original,
        "revision",
        {"action": "save", "id": "backup", "model": "backup-new"},
    )
    assert result.success is True
    assert saved[0][0]["upstream"] == original["upstream"]


def test_activate_profile_updates_both_active_aliases_and_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_connector_mutation(
        "/admin/upstream-profile",
        _base_config(),
        "revision",
        {"action": "activate", "id": "backup"},
    )
    assert result.success is True
    candidate = saved[0][0]
    assert candidate["active_upstream_id"] == "backup"
    assert candidate["active_upstream"] == "backup"
    assert candidate["upstream"]["id"] == "backup"


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        ({"action": "activate", "id": ""}, 400, "missing upstream profile id"),
        ({"action": "activate", "id": "missing"}, 404, "upstream profile not found"),
        ({"action": "delete", "id": "active"}, 409, "cannot delete active"),
        ({"action": "delete", "id": ""}, 400, "missing upstream profile id"),
        ({"action": "unknown", "id": "backup"}, 400, "invalid upstream profile action"),
        ({"action": "save", "id": "new", "protocol": "invalid"}, 400, "invalid upstream protocol"),
        ({"action": "save", "id": "new", "timeout_seconds": "0"}, 400, "invalid numeric field: timeout_seconds"),
    ],
)
def test_invalid_profile_mutations_do_not_save(
    form: dict[str, str],
    status: int,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_connector_mutation(
        "/admin/upstream-profile",
        _base_config(),
        "revision",
        form,
    )
    assert result.status == status
    assert message in result.error
    assert saved == []


def test_delete_inactive_profile_is_idempotent_and_keeps_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_connector_mutation(
        "/admin/upstream-profile",
        _base_config(),
        "revision",
        {"action": "delete", "id": "backup"},
    )
    assert result.success is True
    candidate = saved[0][0]
    assert [item["id"] for item in candidate["upstream_profiles"]] == ["active"]
    assert candidate["upstream"]["id"] == "active"


def test_malformed_connector_collections_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for path, patch, form, message in [
        ("/admin/mcp", {"mcp": {"servers": "broken"}}, {"action": "add", "name": "x", "command": "x"}, "invalid MCP"),
        ("/admin/http-actions", {"http_actions": {"actions": "broken"}}, {"action": "add", "name": "x", "url": "https://x.example"}, "invalid HTTP Action"),
        ("/admin/upstream-profile", {"upstream_profiles": "broken"}, {"action": "save", "id": "x"}, "invalid upstream profile"),
    ]:
        config = _base_config()
        config.update(patch)
        saved = _capture_saves(monkeypatch)
        result = mutations.apply_admin_connector_mutation(path, config, "revision", form)
        assert message in result.error
        assert saved == []

