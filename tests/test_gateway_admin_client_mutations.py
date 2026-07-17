from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any

import pytest

from src import gateway_admin_client_mutations as mutations
from src import gateway_config


def _base_config() -> dict[str, Any]:
    return {
        "admin": {
            "username": "admin",
            "password_hash": "current-hash",
            "must_change_password": True,
        },
        "gateway": {
            "public_base_url": "http://before.example:8885",
            "client_snippet_api_key": "old-snippet-key",
            "downstream_model_alias": "before-model",
            "review_model_alias": "before-review",
            "codex_reasoning_effort": "high",
            "client_context_window": 1000,
            "client_auto_compact_token_limit": 900,
            "client_output_token_limit": 100,
        },
        "downstream_keys": [
            {
                "id": "client_snippet_old",
                "name": "client-snippet",
                "key_hash": gateway_config._hash_secret("old-snippet-key"),
                "enabled": True,
            },
            {
                "id": "client_explicit",
                "name": "explicit",
                "key_hash": gateway_config._hash_secret("explicit-key"),
                "enabled": True,
            },
        ],
    }


def _capture_saves(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict[str, Any], str | None]]:
    saved: list[tuple[dict[str, Any], str | None]] = []
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda config, *, expected_revision=None: saved.append(
            (copy.deepcopy(config), expected_revision)
        ),
    )
    return saved


def test_unmatched_path_does_not_mutate_or_save(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_client_mutation(
        "/admin/mcp",
        config,
        "revision",
        {},
    )
    assert result.matched is False
    assert config == original
    assert saved == []


def test_client_config_is_validated_on_copy_and_saved_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_client_mutation(
        "/admin/client-config",
        config,
        "revision-1",
        {
            "public_base_url": "HTTPS://Gateway.Example.com:443/path",
            "client_snippet_api_key": "new-snippet-key",
            "downstream_model_alias": "new-model",
            "review_model_alias": "new-review",
            "codex_reasoning_effort": "xhigh",
            "client_context_window": "2000",
            "client_auto_compact_token_limit": "1800",
            "client_output_token_limit": "200",
        },
    )
    assert result.success is True
    assert config == original
    assert len(saved) == 1
    candidate, revision = saved[0]
    assert revision == "revision-1"
    assert candidate["gateway"] == {
        "public_base_url": "https://gateway.example.com",
        "client_snippet_api_key": "new-snippet-key",
        "downstream_model_alias": "new-model",
        "review_model_alias": "new-review",
        "codex_reasoning_effort": "xhigh",
        "client_context_window": 2000,
        "client_auto_compact_token_limit": 1800,
        "client_output_token_limit": 200,
    }


def test_client_config_omitted_numeric_fields_preserve_existing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_client_mutation(
        "/admin/client-config",
        _base_config(),
        "revision",
        {
            "public_base_url": "http://after.example:8885",
            "client_snippet_api_key": "next-key",
        },
    )
    assert result.success is True
    gateway = saved[0][0]["gateway"]
    assert gateway["client_context_window"] == 1000
    assert gateway["client_auto_compact_token_limit"] == 900
    assert gateway["client_output_token_limit"] == 100


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"public_base_url": "javascript:alert(1)"}, "invalid public_base_url"),
        (
            {"public_base_url": "http://gateway.example", "client_context_window": "bad"},
            "invalid numeric field: client_context_window",
        ),
        (
            {"public_base_url": "http://gateway.example", "client_output_token_limit": "0"},
            "invalid numeric field: client_output_token_limit",
        ),
        (
            {
                "public_base_url": "http://gateway.example",
                "client_context_window": "100",
                "client_auto_compact_token_limit": "101",
            },
            "must not exceed client_context_window",
        ),
    ],
)
def test_invalid_client_config_never_saves_partial_mutation(
    form: dict[str, str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_client_mutation(
        "/admin/client-config",
        config,
        "revision",
        form,
    )
    assert result.matched is True
    assert result.success is False
    assert result.status == 400
    assert message in result.error
    assert config == original
    assert saved == []


def test_clearing_snippet_key_revokes_only_auto_managed_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_client_mutation(
        "/admin/client-config",
        _base_config(),
        "revision",
        {"public_base_url": "http://gateway.example", "client_snippet_api_key": ""},
    )
    assert result.success is True
    candidate = saved[0][0]
    assert candidate["gateway"]["client_snippet_api_key"] == ""
    assert [item["name"] for item in candidate["downstream_keys"]] == ["explicit"]


@pytest.mark.parametrize(
    ("config_patch", "form", "status", "message"),
    [
        ({}, {}, 400, "missing old_password or new_password"),
        ({"admin": "broken"}, {"old_password": "old", "new_password": "new"}, 400, "invalid admin configuration"),
        ({}, {"old_password": "wrong", "new_password": "new"}, 403, "invalid old password"),
    ],
)
def test_invalid_password_rotation_does_not_save(
    config_patch: dict[str, Any],
    form: dict[str, str],
    status: int,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    config.update(config_patch)
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    monkeypatch.setattr(gateway_config, "_verify_password", lambda *_args: False)
    result = mutations.apply_admin_client_mutation(
        "/admin/password",
        config,
        "revision",
        form,
    )
    assert result.status == status
    assert message in result.error
    assert config == original
    assert saved == []


def test_password_rotation_hashes_new_password_and_clears_change_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    monkeypatch.setattr(
        gateway_config,
        "_verify_password",
        lambda password, encoded: password == "old-secret" and encoded == "current-hash",
    )
    monkeypatch.setattr(gateway_config, "_hash_password", lambda password: f"hashed:{password}")
    result = mutations.apply_admin_client_mutation(
        "/admin/password",
        _base_config(),
        "revision-2",
        {"old_password": "old-secret", "new_password": "new-secret"},
    )
    assert result.success is True
    candidate, revision = saved[0]
    assert revision == "revision-2"
    assert candidate["admin"]["password_hash"] == "hashed:new-secret"
    assert candidate["admin"]["must_change_password"] is False


def test_downstream_key_add_has_stable_id_fingerprint_protocols_and_utc_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    fixed = dt.datetime(2026, 7, 17, 1, 2, 3, tzinfo=dt.timezone.utc)
    result = mutations.apply_admin_client_mutation(
        "/admin/downstream-key",
        _base_config(),
        "revision-3",
        {"action": "add", "name": "new-client", "key": "new-secret-key"},
        now=lambda: fixed,
    )
    assert result.success is True
    candidate = saved[0][0]
    item = next(entry for entry in candidate["downstream_keys"] if entry["name"] == "new-client")
    assert item["id"].startswith("client_")
    assert item["key_hash"] == gateway_config._hash_secret("new-secret-key")
    assert item["prefix"] == gateway_config._secret_fingerprint("new-secret-key")
    assert item["protocols"] == ["models", "chat_completions", "responses", "messages", "direct_tools"]
    assert item["created_at"] == "2026-07-17T01:02:03+00:00"
    assert "new-secret-key" not in json.dumps(candidate)


@pytest.mark.parametrize(
    ("form", "status", "message"),
    [
        ({"action": "add", "name": "explicit", "key": "another"}, 409, "name already exists"),
        ({"action": "add", "name": "another", "key": "explicit-key"}, 409, "value already exists"),
        ({"action": "add", "name": "", "key": "value"}, 400, "missing name or key"),
        ({"action": "delete", "name": ""}, 400, "missing name"),
        ({"action": "disable", "name": "explicit"}, 400, "invalid downstream key action"),
    ],
)
def test_invalid_or_ambiguous_downstream_key_mutation_does_not_save(
    form: dict[str, str],
    status: int,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_client_mutation(
        "/admin/downstream-key",
        config,
        "revision",
        form,
    )
    assert result.status == status
    assert message in result.error
    assert config == original
    assert saved == []


def test_malformed_downstream_key_collection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    config["downstream_keys"] = {"bad": "mapping"}
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_client_mutation(
        "/admin/downstream-key",
        config,
        "revision",
        {"action": "add", "name": "new", "key": "secret"},
    )
    assert result.status == 400
    assert result.error == "invalid downstream key configuration"
    assert saved == []


def test_downstream_key_delete_is_idempotent_and_revision_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = _capture_saves(monkeypatch)
    result = mutations.apply_admin_client_mutation(
        "/admin/downstream-key",
        _base_config(),
        "revision-delete",
        {"action": "delete", "name": "explicit"},
    )
    assert result.success is True
    assert saved[0][1] == "revision-delete"
    assert [item["name"] for item in saved[0][0]["downstream_keys"]] == ["client-snippet"]


def test_save_conflict_propagates_and_never_mutates_loaded_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _base_config()
    original = copy.deepcopy(config)
    monkeypatch.setattr(
        gateway_config,
        "save_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("conflict marker")),
    )
    with pytest.raises(RuntimeError, match="conflict marker"):
        mutations.apply_admin_client_mutation(
            "/admin/client-config",
            config,
            "stale-revision",
            {"public_base_url": "http://gateway.example", "client_snippet_api_key": "new"},
        )
    assert config == original

