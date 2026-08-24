from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

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
    from src import gateway_cache, gateway_encryption, gateway_persistence, gateway_upstream_pool

    old_path = gateway_config.CONFIG_PATH
    old_key = gateway_encryption._encryption_key
    old_fernet = gateway_encryption._fernet
    gateway_encryption._encryption_key = None
    gateway_encryption._fernet = None
    monkeypatch.setenv("GATEWAY_ADMIN_PASSWORD", "test-admin-password")
    monkeypatch.setenv("GATEWAY_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("GATEWAY_STATS_DB_PATH", str(tmp_path / "stats.sqlite3"))
    gateway_config.CONFIG_PATH = tmp_path / "gateway.json"
    gateway_cache.reset_caches()
    gateway_upstream_pool.reset_upstream_pool()
    gateway_persistence.close_persistence()
    cfg = gateway_config._default_config()
    persistence_path = tmp_path / "gateway.sqlite3"
    cfg["persistence"]["db_path"] = str(persistence_path)
    cfg["upstream"].update({
        "base_url": "http://127.0.0.1:9",
        "api_key": "upstream-secret",
        "model": "test-model",
    })
    cfg["upstream_profiles"] = [{"id": "default", "name": "default", **cfg["upstream"]}]
    cfg["active_upstream_id"] = "default"
    cfg["active_upstream"] = "default"
    gateway_config.save_config(cfg)
    gateway_persistence.init_persistence(
        gateway_persistence.PersistenceConfig(db_path=str(persistence_path))
    )
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
        gateway_cache.reset_caches()
        gateway_upstream_pool.reset_upstream_pool()
        gateway_persistence.close_persistence()
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
    for endpoint in (
        "/api/stats/dashboard",
        "/api/cache/stats",
        "/api/upstreams/status",
        "/api/intelligence/status",
    ):
        assert f'data-status-endpoint="{endpoint}"' in str(ui)
    assert 'href="/client-config"' in str(ui)
    assert 'href="/ui/config/client"' not in str(ui)
    assert 'href="/stats"' not in str(ui)
    assert schema_status == 200
    assert len(schema["tabs"]) == 10
    assert {tab["id"] for tab in schema["tabs"]} >= {"intelligence", "concurrency", "web2api", "model_matrix"}
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


def test_model_capability_update_is_strict_revision_bound_and_persistent(
    admin_server: str,
) -> None:
    _, current = _request(admin_server, "/api/config")
    profile = current["config"]["upstream"]
    model = profile["models"][0]["name"]
    payload = {
        "profile_id": profile["id"],
        "model": model,
        "capability": "supports_image_recognition",
        "value": True,
        "revision": current["revision"],
    }

    status, updated = _request(
        admin_server,
        "/api/config/model-capability",
        method="POST",
        payload=payload,
        origin=admin_server,
    )

    assert status == 200
    assert updated["ok"] is True
    assert updated["revision"] != current["revision"]
    row = next(
        item
        for item in gateway_config.flatten_profile_models(gateway_config.load_config())
        if item["profile_id"] == profile["id"] and item["model"] == model
    )
    assert row["capabilities"]["supports_image_recognition"] is True
    assert row["capability_overrides"]["supports_image_recognition"] is True

    stale_status, _ = _request(
        admin_server,
        "/api/config/model-capability",
        method="POST",
        payload={**payload, "value": False},
        origin=admin_server,
    )
    assert stale_status == 409


@pytest.mark.parametrize("value", ["false", 0, None, [], {}])
def test_model_capability_update_rejects_non_boolean_values(
    admin_server: str,
    value: object,
) -> None:
    _, current = _request(admin_server, "/api/config")
    profile = current["config"]["upstream"]
    status, result = _request(
        admin_server,
        "/api/config/model-capability",
        method="POST",
        payload={
            "profile_id": profile["id"],
            "model": profile["models"][0]["name"],
            "capability": "supports_image_recognition",
            "value": value,
            "revision": current["revision"],
        },
        origin=admin_server,
    )
    assert status == 400
    assert "JSON boolean" in result["error"]["message"]


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


def test_config_center_browser_executes_status_refresh_errors_and_links(admin_server: str) -> None:
    require_browser = (
        os.environ.get("GATEWAY_REQUIRE_BROWSER_TESTS") == "1"
        or os.environ.get("CI", "").lower() in {"1", "true", "yes"}
    )
    try:
        from playwright import sync_api as playwright
    except ImportError as exc:
        if require_browser:
            pytest.fail(f"Playwright is required by this test gate: {exc}", pytrace=False)
        pytest.skip(f"Playwright is not installed: {exc}")

    from src.gateway_persistence import (
        save_memory,
        save_semantic_cache_entry,
        save_tool_cache_entry,
    )

    assert save_semantic_cache_entry("ui-key", "ui-query", [0.1], {"ok": True}, 3600)
    assert save_tool_cache_entry("ui-tool", "ui-hash", "ui-result", True, 0.01)
    assert save_memory("ui-memory", "ui-content", None, 0.5, [], None)
    cache_status, cache_payload = _request(admin_server, "/api/cache/stats")
    assert cache_status == 200
    assert isinstance(cache_payload, dict)
    persistence = cache_payload["cache"]["persistence"]
    assert isinstance(persistence, dict)
    expected_cache_summary = (
        f"持久化：语义 {persistence['semantic_cache_entries']}，"
        f"工具 {persistence['tool_cache_entries']}，记忆 {persistence['memories']}"
    )

    with playwright.sync_playwright() as runtime:
        candidates = [
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", ""),
            runtime.chromium.executable_path,
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
        ]
        executable = next((path for path in candidates if path and Path(path).exists()), "")
        if not executable:
            if require_browser:
                pytest.fail("Chromium is required by this test gate", pytrace=False)
            pytest.skip("no Chromium-compatible browser executable available")

        browser = runtime.chromium.launch(headless=True, executable_path=executable)
        context = browser.new_context(
            http_credentials={"username": "admin", "password": "test-admin-password"},
        )
        page = context.new_page()
        requested: list[str] = []
        page.on("requestfinished", lambda request: requested.append(request.url))
        try:
            response = page.goto(admin_server + "/ui/config", wait_until="networkidle")
            assert response is not None and response.status == 200
            playwright.expect(page.locator(".status-dot.status-ok")).to_have_count(3)
            playwright.expect(page.locator(".status-dot.status-degraded")).to_have_count(1)
            playwright.expect(
                page.locator('[data-status-endpoint="/api/cache/stats"] .status-summary')
            ).to_contain_text(expected_cache_summary)
            playwright.expect(
                page.locator('[data-status-endpoint="/api/upstreams/status"] .status-summary')
            ).to_contain_text("成功调用 0 次")
            for endpoint in (
                "/api/stats/dashboard",
                "/api/cache/stats",
                "/api/upstreams/status",
                "/api/intelligence/status",
            ):
                assert any(url.endswith(endpoint) for url in requested), endpoint
                payload = page.locator(
                    f'[data-status-endpoint="{endpoint}"] .status-payload'
                ).text_content()
                assert payload not in {"", "{}"}

            before = sum(url.endswith("/api/stats/dashboard") for url in requested)
            with page.expect_request(lambda request: request.url.endswith("/api/stats/dashboard")):
                page.locator("#refresh-status").click()
            page.wait_for_function(
                "Array.from(document.querySelectorAll('.status-summary')).every(node => node.textContent !== '加载中…')"
            )
            after = sum(url.endswith("/api/stats/dashboard") for url in requested)
            assert after >= before + 1

            page.route(
                "**/api/stats/dashboard",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "http": {"total_requests": None},
                        "dashboard": {
                            "timestamp": "",
                            "requests": [],
                            "tools": [],
                        },
                    }),
                ),
            )
            page.route(
                "**/api/cache/stats",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "cache": {
                            "persistence": {
                                "db_path": [],
                                "semantic_cache_entries": "0",
                                "tool_cache_entries": 0,
                                "memories": 0,
                            }
                        }
                    }),
                ),
            )
            page.route(
                "**/api/upstreams/status",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "upstream_pool": {
                            "profiles": [
                                {
                                    "id": "default",
                                    "healthy": False,
                                    "success_count": 0,
                                    "consecutive_failures": 1,
                                }
                            ]
                        }
                    }),
                ),
            )
            page.route(
                "**/api/intelligence/status",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "intelligence": {
                            "mode": "llm",
                            "provider": "missing-provider",
                            "provider_registered": False,
                            "strict_mode": False,
                            "upstream_configured": True,
                            "runtime": {
                                "calls": 0,
                                "successes": 0,
                                "failures": 0,
                                "last_error_type": "",
                            },
                        }
                    }),
                ),
            )
            with page.expect_request(lambda request: request.url.endswith("/api/intelligence/status")):
                page.locator("#refresh-status").click()
            stats_card = page.locator('[data-status-endpoint="/api/stats/dashboard"]')
            cache_card = page.locator('[data-status-endpoint="/api/cache/stats"]')
            upstream_card = page.locator('[data-status-endpoint="/api/upstreams/status"]')
            intelligence_card = page.locator(
                '[data-status-endpoint="/api/intelligence/status"]'
            )
            playwright.expect(stats_card.locator(".status-summary")).to_contain_text(
                "统计状态不可用"
            )
            playwright.expect(cache_card.locator(".status-summary")).to_contain_text(
                "持久化状态不可用"
            )
            playwright.expect(upstream_card.locator(".status-summary")).to_contain_text(
                "断路器可用 0 个"
            )
            playwright.expect(intelligence_card.locator(".status-summary")).to_contain_text(
                "Provider 未注册"
            )
            assert stats_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-degraded')"
            )
            assert cache_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-degraded')"
            )
            assert upstream_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-degraded')"
            )
            assert intelligence_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-degraded')"
            )

            page.unroute("**/api/stats/dashboard")
            page.unroute("**/api/cache/stats")
            page.unroute("**/api/upstreams/status")
            page.route(
                "**/api/upstreams/status",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "upstream_pool": {
                            "profiles": [
                                {
                                    "id": "default",
                                    "healthy": True,
                                    "success_count": 2,
                                    "consecutive_failures": 1,
                                    "last_error_type": "TimeoutError",
                                }
                            ]
                        }
                    }),
                ),
            )
            page.route(
                "**/api/cache/stats",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body="not-json",
                ),
            )
            with page.expect_request(lambda request: request.url.endswith("/api/cache/stats")):
                page.locator("#refresh-status").click()
            playwright.expect(cache_card.locator(".status-summary")).to_contain_text(
                "响应不是有效 JSON"
            )
            assert cache_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-error')"
            )
            playwright.expect(upstream_card.locator(".status-summary")).to_contain_text(
                "连续失败 1 个"
            )
            assert upstream_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-degraded')"
            )

            page.unroute("**/api/cache/stats")
            page.unroute("**/api/intelligence/status")
            page.route(
                "**/api/intelligence/status",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "intelligence": {
                            "mode": "llm",
                            "provider": "gateway_upstream",
                            "provider_registered": True,
                            "strict_mode": False,
                            "upstream_configured": True,
                            "runtime": {
                                "calls": 1,
                                "successes": 0,
                                "failures": 1,
                                "last_error_type": "IntelligenceProviderError"
                            },
                        }
                    }),
                ),
            )
            with page.expect_request(lambda request: request.url.endswith("/api/intelligence/status")):
                page.locator("#refresh-status").click()
            playwright.expect(intelligence_card.locator(".status-summary")).to_contain_text(
                "最近错误：IntelligenceProviderError"
            )
            assert intelligence_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-degraded')"
            )
            assert not intelligence_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-error')"
            )

            page.unroute("**/api/upstreams/status")
            page.route(
                "**/api/upstreams/status",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({
                        "upstream_pool": {
                            "profiles": [
                                {
                                    "id": "typed-malformed",
                                    "healthy": "false",
                                    "success_count": "2",
                                    "consecutive_failures": "0",
                                },
                                {
                                    "id": "missing-failures",
                                    "healthy": True,
                                    "success_count": 2,
                                },
                            ]
                        }
                    }),
                ),
            )
            with page.expect_request(lambda request: request.url.endswith("/api/upstreams/status")):
                page.locator("#refresh-status").click()
            playwright.expect(upstream_card.locator(".status-summary")).to_contain_text(
                "无效状态 2 个"
            )
            assert upstream_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-degraded')"
            )

            page.unroute("**/api/intelligence/status")
            page.route(
                "**/api/intelligence/status",
                lambda route: route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"error": {"message": "forced status failure"}}),
                ),
            )
            with page.expect_request(lambda request: request.url.endswith("/api/intelligence/status")):
                page.locator("#refresh-status").click()
            playwright.expect(intelligence_card.locator(".status-summary")).to_contain_text(
                "forced status failure"
            )
            assert intelligence_card.locator(".status-dot").evaluate(
                "node => node.classList.contains('status-error')"
            )

            with page.expect_response(lambda item: item.url.endswith("/client-config")) as client_response:
                page.locator('a[href="/client-config"]').click()
            assert client_response.value.status == 200
            page.go_back(wait_until="networkidle")
            with page.expect_response(
                lambda item: item.url.endswith("/api/stats/dashboard")
            ) as stats_response:
                page.locator('a[href="/api/stats/dashboard"]').click()
            assert stats_response.value.status == 200
        finally:
            context.close()
            browser.close()
