from __future__ import annotations

from typing import Any

import pytest

from src import gateway_admin_operations as operations


class _Handler:
    def __init__(self, path: str) -> None:
        self.path = path


class _Responses:
    def __init__(self) -> None:
        self.json: list[tuple[int, dict[str, Any]]] = []
        self.text: list[tuple[int, str, str]] = []

    def json_response(self, _handler: Any, status: int, payload: dict[str, Any]) -> None:
        self.json.append((status, payload))

    def text_response(
        self,
        _handler: Any,
        status: int,
        payload: str,
        content_type: str,
    ) -> None:
        self.text.append((status, payload, content_type))


def _handle(
    path: str,
    responses: _Responses,
    *,
    check_admin=lambda _handler: True,
) -> bool:
    return operations.handle_admin_operations_get(
        _Handler(path),
        path.split("?", 1)[0],
        check_admin=check_admin,
        json_response=responses.json_response,
        text_response=responses.text_response,
        ready=lambda: True,
    )


def test_unmatched_admin_operations_path_is_not_claimed() -> None:
    responses = _Responses()
    auth_calls: list[Any] = []
    matched = _handle(
        "/admin/not-an-operation",
        responses,
        check_admin=lambda handler: auth_calls.append(handler) or True,
    )
    assert matched is False
    assert auth_calls == []
    assert responses.json == []
    assert responses.text == []


@pytest.mark.parametrize("path", sorted(operations._OPERATIONS_PATHS))
def test_every_admin_operations_route_requires_authentication(path: str) -> None:
    responses = _Responses()
    auth_calls: list[Any] = []
    matched = _handle(
        path,
        responses,
        check_admin=lambda handler: auth_calls.append(handler) or False,
    )
    assert matched is True
    assert len(auth_calls) == 1
    assert responses.json == []
    assert responses.text == []


def test_metrics_route_uses_ready_state_and_prometheus_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []
    monkeypatch.setattr(
        operations,
        "prometheus_metrics_text",
        lambda *, ready: seen.append(ready) or "gateway_ready 1\n",
    )
    responses = _Responses()
    assert _handle("/admin/metrics", responses) is True
    assert seen == [True]
    assert responses.text == [
        (200, "gateway_ready 1\n", "text/plain; version=0.0.4; charset=utf-8")
    ]


def test_stats_requests_and_failures_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_logging

    monkeypatch.setattr(gateway_logging, "_stats_snapshot", lambda: {"total_requests": 7})
    monkeypatch.setattr(gateway_logging, "_tail_requests", lambda limit: [{"limit": limit}])
    monkeypatch.setattr(gateway_logging, "_tail_failures", lambda limit: [{"limit": limit}])

    responses = _Responses()
    assert _handle("/admin/stats.json", responses) is True
    assert _handle("/admin/requests.json", responses) is True
    assert _handle("/admin/failures.json", responses) is True
    assert responses.json == [
        (200, {"stats": {"total_requests": 7}}),
        (200, {"requests": [{"limit": 200}]}),
        (200, {"failures": [{"limit": 200}]}),
    ]


def test_storage_route_is_read_only_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_config, gateway_sqlite_compact

    config = {"gateway": {"marker": "config"}}
    monkeypatch.setattr(gateway_config, "load_config", lambda: config)
    monkeypatch.setattr(
        gateway_sqlite_compact,
        "inspect_gateway_databases",
        lambda received: [{"same_config": received is config}],
    )

    responses = _Responses()
    assert _handle("/admin/storage.json", responses) is True
    status, payload = responses.json[0]
    assert status == 200
    assert payload["databases"] == [{"same_config": True}]
    assert payload["compaction"]["execution"] == "offline_cli_only"
    assert payload["compaction"]["online_execution_supported"] is False
    assert "--confirm-gateway-stopped" in payload["compaction"]["command"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", 100),
        ("?limit=20", 20),
        ("?limit=invalid", 100),
        ("?limit=0", 1),
        ("?limit=-8", 1),
        ("?limit=5000", 1000),
    ],
)
def test_trace_limit_is_normalized(
    query: str,
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.gateway_observability import OBSERVABILITY

    seen: list[int] = []
    monkeypatch.setattr(
        OBSERVABILITY,
        "traces",
        lambda limit: seen.append(limit) or [{"limit": limit}],
    )
    responses = _Responses()
    assert _handle(f"/admin/traces.json{query}", responses) is True
    assert seen == [expected]
    assert responses.json == [
        (200, {"traces": [{"limit": expected}], "limit": expected})
    ]


def test_prometheus_renderer_emits_each_maintenance_failure_metric_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import (
        gateway_admission,
        gateway_config,
        gateway_logging,
        gateway_maintenance,
        gateway_observability,
        gateway_rate_limit,
    )

    monkeypatch.setattr(gateway_config, "_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_logging, "_stats_snapshot", lambda: {"total_requests": 3})
    monkeypatch.setattr(
        gateway_maintenance,
        "maintenance_snapshot",
        lambda: {
            "runs_total": 4,
            "failures_total": 2,
            "last_success": False,
            "components": {"primary": {"rows": {"requests": 5}, "space_bytes": 99}},
        },
    )
    monkeypatch.setattr(
        gateway_admission.ADMISSION_SERVICE,
        "snapshot",
        lambda _config: {
            "backend": "sqlite",
            "configured_backend": "sqlite",
            "active": 0,
            "effective_limit": 8,
            "rejections": 1,
            "expired_reaped": 2,
            "degraded": False,
        },
    )
    monkeypatch.setattr(
        gateway_rate_limit.RATE_LIMIT_SERVICE,
        "snapshot",
        lambda _config: {
            "backend": "sqlite",
            "configured_backend": "sqlite",
            "rejections": 1,
            "active_identities": 2,
            "degraded": False,
        },
    )
    monkeypatch.setattr(gateway_observability.OBSERVABILITY, "prometheus", lambda: "")

    lines = operations.prometheus_metrics_text(ready=True).splitlines()
    assert lines.count("# TYPE gateway_maintenance_failures_total counter") == 1
    assert lines.count("gateway_maintenance_failures_total 2") == 1

