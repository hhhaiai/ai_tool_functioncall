"""Authenticated read-only operations endpoints for the Gateway Admin API."""
from __future__ import annotations

import urllib.parse
from typing import Any, Callable

Json = dict[str, Any]

_OPERATIONS_PATHS = {
    "/admin/metrics",
    "/admin/stats.json",
    "/admin/storage.json",
    "/admin/traces.json",
    "/admin/requests.json",
    "/admin/failures.json",
}


def prometheus_metrics_text(*, ready: bool) -> str:
    """Build the low-cardinality Gateway Prometheus exposition exactly once."""
    from .gateway_admission import ADMISSION_SERVICE
    from .gateway_config import _gateway_config
    from .gateway_logging import _stats_snapshot
    from .gateway_maintenance import maintenance_snapshot
    from .gateway_observability import OBSERVABILITY
    from .gateway_rate_limit import RATE_LIMIT_SERVICE

    config = _gateway_config()
    stats = _stats_snapshot()
    maintenance = maintenance_snapshot()
    admission = ADMISSION_SERVICE.snapshot(config)
    rate = RATE_LIMIT_SERVICE.snapshot(config)
    total = int(stats.get("total_requests") or (stats.get("requests") or {}).get("total") or 0)
    primary = (maintenance.get("components") or {}).get("primary") or {}
    primary_rows = sum(int(value or 0) for value in (primary.get("rows") or {}).values())
    rate_backend = str(rate.get("backend") or "unknown")
    rate_configured = str(rate.get("configured_backend") or "unknown")
    admission_backend = str(admission.get("backend") or "unknown")
    admission_configured = str(admission.get("configured_backend") or "unknown")
    return (
        "# TYPE gateway_requests_total counter\n"
        f"gateway_requests_total {total}\n"
        "# TYPE gateway_rate_limit_rejections_total counter\n"
        f"gateway_rate_limit_rejections_total {int(rate.get('rejections') or 0)}\n"
        "# TYPE gateway_rate_limit_identities gauge\n"
        f"gateway_rate_limit_identities {int(rate.get('active_identities') or 0)}\n"
        "# TYPE gateway_rate_limit_backend_info gauge\n"
        f'gateway_rate_limit_backend_info{{backend="{rate_backend}",configured="{rate_configured}"}} 1\n'
        "# TYPE gateway_rate_limit_degraded gauge\n"
        f"gateway_rate_limit_degraded {1 if rate.get('degraded') else 0}\n"
        "# TYPE gateway_ready gauge\n"
        f"gateway_ready {1 if ready else 0}\n"
        "# TYPE gateway_maintenance_runs_total counter\n"
        f"gateway_maintenance_runs_total {int(maintenance.get('runs_total') or 0)}\n"
        "# TYPE gateway_maintenance_failures_total counter\n"
        f"gateway_maintenance_failures_total {int(maintenance.get('failures_total') or 0)}\n"
        "# TYPE gateway_maintenance_last_success gauge\n"
        f"gateway_maintenance_last_success {1 if maintenance.get('last_success') is True else 0}\n"
        "# TYPE gateway_persistent_primary_bytes gauge\n"
        f"gateway_persistent_primary_bytes {int(primary.get('space_bytes') or 0)}\n"
        "# TYPE gateway_persistent_primary_rows gauge\n"
        f"gateway_persistent_primary_rows {primary_rows}\n"
        "# TYPE gateway_request_admission_active gauge\n"
        f"gateway_request_admission_active {int(admission.get('active') or 0)}\n"
        "# TYPE gateway_request_admission_limit gauge\n"
        f"gateway_request_admission_limit {int(admission.get('effective_limit') or 0)}\n"
        "# TYPE gateway_request_admission_rejections_total counter\n"
        f"gateway_request_admission_rejections_total {int(admission.get('rejections') or 0)}\n"
        "# TYPE gateway_request_admission_expired_reaped_total counter\n"
        f"gateway_request_admission_expired_reaped_total {int(admission.get('expired_reaped') or 0)}\n"
        "# TYPE gateway_request_admission_backend_info gauge\n"
        f'gateway_request_admission_backend_info{{backend="{admission_backend}",configured="{admission_configured}"}} 1\n'
        "# TYPE gateway_request_admission_degraded gauge\n"
        f"gateway_request_admission_degraded {1 if admission.get('degraded') else 0}\n"
        + OBSERVABILITY.prometheus()
    )


def handle_admin_operations_get(
    handler: Any,
    path: str,
    *,
    check_admin: Callable[[Any], bool],
    json_response: Callable[[Any, int, Json], None],
    text_response: Callable[[Any, int, str, str], None],
    ready: Callable[[], bool],
) -> bool:
    """Handle one read-only operations endpoint and return whether it matched."""
    if path not in _OPERATIONS_PATHS:
        return False
    if not check_admin(handler):
        return True
    if path == "/admin/metrics":
        text_response(
            handler,
            200,
            prometheus_metrics_text(ready=ready()),
            "text/plain; version=0.0.4; charset=utf-8",
        )
        return True
    if path == "/admin/stats.json":
        from .gateway_logging import _stats_snapshot
        json_response(handler, 200, {"stats": _stats_snapshot()})
        return True
    if path == "/admin/storage.json":
        from .gateway_config import load_config
        from .gateway_sqlite_compact import inspect_gateway_databases
        json_response(
            handler,
            200,
            {
                "databases": inspect_gateway_databases(load_config()),
                "compaction": {
                    "execution": "offline_cli_only",
                    "command": "python3 -m src.gateway_sqlite_compact --database <path> --execute --confirm-gateway-stopped",
                    "online_execution_supported": False,
                },
            },
        )
        return True
    if path == "/admin/traces.json":
        from .gateway_observability import OBSERVABILITY
        query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        try:
            limit = int(query.get("limit", ["100"])[0])
        except (TypeError, ValueError):
            limit = 100
        bounded = max(1, min(limit, 1000))
        json_response(handler, 200, {"traces": OBSERVABILITY.traces(bounded), "limit": bounded})
        return True
    if path == "/admin/requests.json":
        from .gateway_logging import _tail_requests
        json_response(handler, 200, {"requests": _tail_requests(200)})
        return True
    if path == "/admin/failures.json":
        from .gateway_logging import _tail_failures
        json_response(handler, 200, {"failures": _tail_failures(200)})
        return True
    return False


__all__ = ["handle_admin_operations_get", "prometheus_metrics_text"]
