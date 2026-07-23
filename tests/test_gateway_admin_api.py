from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from src import gateway_config
from src.gateway_http_handler import GatewayHandler


def _auth(password: str = "test-admin-password") -> str:
    token = base64.b64encode(f"admin:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    origin: str | None = None,
    password: str = "test-admin-password",
) -> tuple[int, dict | str]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": _auth(password)}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(base_url + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            if "application/json" in str(response.headers.get("content-type") or ""):
                return response.status, json.loads(raw)
            return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


@pytest.fixture
def admin_server(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from src import gateway_encryption

    old_path = gateway_config.CONFIG_PATH
    old_key = gateway_encryption._encryption_key
    old_fernet = gateway_encryption._fernet
    gateway_encryption._encryption_key = None
    gateway_encryption._fernet = None
    monkeypatch.setenv("GATEWAY_ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("GATEWAY_STATS_DB_PATH", str(tmp_path / "stats.sqlite3"))
    gateway_config.CONFIG_PATH = tmp_path / "gateway.json"
    cfg = gateway_config._default_config()
    cfg["upstream"].update({
        "base_url": "http://127.0.0.1:9",
        "api_key": "upstream-secret",
        "model": "test-model",
    })
    cfg["upstream_profiles"] = [{"id": "default", "name": "default", **cfg["upstream"]}]
    cfg["active_upstream_id"] = "default"
    cfg["active_upstream"] = "default"
    gateway_config.save_config(cfg)
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway_config.CONFIG_PATH = old_path
        gateway_encryption._encryption_key = old_key
        gateway_encryption._fernet = old_fernet
        from src import gateway_stats
        if gateway_stats._db_conn is not None:
            gateway_stats._db_conn.close()
            gateway_stats._db_conn = None


def test_admin_config_ui_schema_and_redacted_get_are_live(admin_server: str) -> None:
    ui_status, ui = _request(admin_server, "/ui/config")
    schema_status, schema = _request(admin_server, "/api/config/schema")
    config_status, config = _request(admin_server, "/api/config")

    assert ui_status == 200
    assert "Gateway 配置中心" in str(ui)
    assert "/api/config/update" in str(ui)
    assert schema_status == 200
    assert len(schema["tabs"]) == 9
    assert {tab["id"] for tab in schema["tabs"]} >= {"intelligence", "concurrency", "web2api"}
    assert config_status == 200
    assert config["revision"]
    serialized = json.dumps(config)
    assert "upstream-secret" not in serialized
    assert config["config"]["upstream"]["api_key"] == "***"


def test_config_update_is_revision_aware_schema_bound_and_reloads_runtime(admin_server: str) -> None:
    _, current = _request(admin_server, "/api/config")
    payload = {
        "revision": current["revision"],
        "config": {
            "cache": {"enabled": False, "max_entries": "222"},
            "concurrency": {
                "multi_upstream_enabled": True,
                "multi_upstream_failure_threshold": 2,
            },
            "intelligence": {"use_llm": True, "strict_mode": False},
        },
    }
    status, result = _request(
        admin_server,
        "/api/config/update",
        method="POST",
        payload=payload,
        origin=admin_server,
    )

    assert status == 200
    assert result["ok"] is True
    assert result["revision"] != current["revision"]
    assert "cache.enabled" in result["changed_fields"]
    saved = gateway_config.load_config()
    assert saved["cache"]["enabled"] is False
    assert saved["cache"]["max_entries"] == 222
    assert saved["concurrency"]["multi_upstream_enabled"] is True
    assert saved["intelligence"]["use_llm"] is True
    assert saved["upstream"]["api_key"] == "upstream-secret"

    stale_status, stale = _request(
        admin_server,
        "/api/config/update",
        method="POST",
        payload={"revision": current["revision"], "config": {"cache": {"enabled": True}}},
        origin=admin_server,
    )
    assert stale_status == 409
    assert "changed while it was being edited" in stale["error"]["message"]


def test_config_update_rejects_cross_origin_unknown_fields_and_invalid_invariants(
    admin_server: str,
) -> None:
    _, current = _request(admin_server, "/api/config")
    cross_status, _ = _request(
        admin_server,
        "/api/config/update",
        method="POST",
        payload={"config": {"cache": {"enabled": False}}},
        origin="https://attacker.example",
    )
    unknown_status, unknown = _request(
        admin_server,
        "/api/config/update",
        method="POST",
        payload={"config": {"admin": {"username": "attacker"}}},
        origin=admin_server,
    )
    invalid_status, invalid = _request(
        admin_server,
        "/api/config/update",
        method="POST",
        payload={
            "revision": current["revision"],
            "config": {"context": {"max_input_tokens": 1000, "fanout_chunk_tokens": 2000}},
        },
        origin=admin_server,
    )

    assert cross_status == 403
    assert unknown_status == 400
    assert "not editable" in unknown["error"]["message"]
    assert invalid_status == 400
    assert "must not exceed" in invalid["error"]["message"]
    assert gateway_config.load_config()["admin"]["username"] == "admin"


def test_stats_cache_and_cache_clear_admin_apis(admin_server: str) -> None:
    stats_status, stats = _request(admin_server, "/api/stats/dashboard")
    cache_status, cache = _request(admin_server, "/api/cache/stats")
    clear_status, cleared = _request(
        admin_server,
        "/api/cache/clear",
        method="POST",
        payload={},
        origin=admin_server,
    )

    assert stats_status == 200
    assert {"dashboard", "http", "hourly", "top_paths", "top_tools", "upstream_pool"} <= set(stats)
    assert cache_status == 200
    assert {"semantic", "tools", "persistence"} <= set(cache["cache"])
    assert clear_status == 200
    assert cleared["ok"] is True
    assert {"semantic_memory", "tool_memory", "semantic_persistent", "tool_persistent"} <= set(cleared["cleared"])


def test_all_admin_api_routes_require_basic_auth(admin_server: str) -> None:
    request = urllib.request.Request(admin_server + "/api/config")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=5)
    assert caught.value.code == 401
