"""Bounded, observable maintenance for Gateway persistent runtime data."""
from __future__ import annotations

import copy
import os
import pathlib
import shutil
import stat
import threading
import time
from typing import Any, Callable

Json = dict[str, Any]

_LOCK = threading.RLock()
_STATE: Json = {
    "runs_total": 0,
    "failures_total": 0,
    "last_started_at": 0.0,
    "last_completed_at": 0.0,
    "last_success": None,
    "last_error": "",
    "components": {},
}

_SAFE_RUNTIME_PREFIXES = (
    "agent-planner-",
    "project-scope-cli-smoke-",
    "remote-mimo-capability-",
    "codex-cli-smoke-",
    "current-skills-project-smoke-",
    "verify-",
)


def maintenance_snapshot() -> Json:
    with _LOCK:
        return copy.deepcopy(_STATE)


def record_maintenance_crash(exc: BaseException) -> Json:
    """Record an unexpected cycle-level failure that escaped component guards."""
    now = time.time()
    with _LOCK:
        _STATE["runs_total"] = int(_STATE.get("runs_total") or 0) + 1
        _STATE["failures_total"] = int(_STATE.get("failures_total") or 0) + 1
        _STATE.update({
            "last_started_at": now,
            "last_completed_at": now,
            "last_success": False,
            "last_error": f"{exc.__class__.__name__}: {exc}"[:2000],
            "components": {
                "cycle": {
                    "ok": False,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc)[:1000],
                }
            },
        })
        return copy.deepcopy(_STATE)


def _tree_size(path: pathlib.Path, limit: int) -> int:
    total = 0
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories if not pathlib.Path(root, name).is_symlink()]
        for name in files:
            candidate = pathlib.Path(root, name)
            try:
                if candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
            except FileNotFoundError:
                continue
            if total > limit:
                return total
    return total


def cleanup_stale_runtime(
    runtime_dir: pathlib.Path | str,
    *,
    retention_days: int = 7,
    max_entries: int = 20,
    max_entry_bytes: int = 256 * 1024 * 1024,
    dry_run: bool = True,
    now: float | None = None,
) -> Json:
    """Remove only known disposable runtime artifacts, never arbitrary state."""
    root = pathlib.Path(runtime_dir).expanduser()
    result: Json = {
        "enabled": True,
        "dry_run": bool(dry_run),
        "eligible": 0,
        "selected": 0,
        "would_remove": 0,
        "removed": 0,
        "removed_bytes": 0,
        "skipped_oversized": 0,
    }
    if not root.exists():
        return result
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"runtime cleanup root is not a real directory: {root}")
    cutoff = float(now if now is not None else time.time()) - max(0, int(retention_days)) * 86400
    candidates: list[pathlib.Path] = []
    anonymous_root = root / "anonymous_spaces"
    if anonymous_root.is_dir() and not anonymous_root.is_symlink():
        candidates.extend(item for item in anonymous_root.iterdir())
    candidates.extend(
        item
        for item in root.iterdir()
        if item.name.startswith(_SAFE_RUNTIME_PREFIXES)
    )
    stale: list[tuple[float, pathlib.Path]] = []
    for candidate in candidates:
        try:
            candidate_info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(candidate_info.st_mode) or candidate_info.st_mtime >= cutoff:
            continue
        if not (stat.S_ISDIR(candidate_info.st_mode) or stat.S_ISREG(candidate_info.st_mode)):
            continue
        stale.append((candidate_info.st_mtime, candidate))
    stale.sort(key=lambda item: item[0])
    result["eligible"] = len(stale)
    for _, candidate in stale[: max(0, int(max_entries))]:
        size = _tree_size(candidate, max(0, int(max_entry_bytes)))
        if size > max_entry_bytes:
            result["skipped_oversized"] += 1
            continue
        result["selected"] += 1
        if dry_run:
            result["would_remove"] += 1
        else:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            result["removed"] += 1
            result["removed_bytes"] += size
    return result


def _run_component(name: str, operation: Callable[[], Json | int]) -> tuple[Json, bool]:
    started = time.time()
    try:
        raw = operation()
        payload = raw if isinstance(raw, dict) else {"deleted": int(raw)}
        return {"ok": True, "duration_ms": round((time.time() - started) * 1000, 3), **payload}, True
    except Exception as exc:
        return {
            "ok": False,
            "duration_ms": round((time.time() - started) * 1000, 3),
            "error_type": exc.__class__.__name__,
            "error": str(exc)[:1000],
        }, False


def run_gateway_maintenance(config: Json, *, now: float | None = None) -> Json:
    """Run one bounded maintenance cycle and retain failures for metrics/admin."""
    settings = dict(config.get("maintenance") or {})
    batch_size = int(settings.get("batch_size") or 1_000)
    max_batches = int(settings.get("max_batches_per_run") or 4)
    vacuum_pages = int(settings.get("incremental_vacuum_pages") or 256)
    dry_run = bool(settings.get("dry_run", False))
    components: Json = {}
    success = True

    if str((config.get("gateway") or {}).get("logging_backend") or "sqlite") == "sqlite":
        from .gateway_logging import cleanup_primary_sqlite
        current_dt = None
        if now is not None:
            import datetime as dt
            current_dt = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
        components["primary"] , ok = _run_component(
            "primary",
            lambda: cleanup_primary_sqlite(
                request_retention_days=int(settings.get("request_log_retention_days") or 30),
                request_max_rows=int(settings.get("request_log_max_rows") or 100_000),
                failure_retention_days=int(settings.get("tool_failure_retention_days") or 90),
                failure_max_rows=int(settings.get("tool_failure_max_rows") or 50_000),
                memory_retention_days=int(settings.get("memory_retention_days") or 90),
                memory_max_rows=int(settings.get("memory_max_rows") or 50_000),
                batch_size=batch_size,
                max_batches=max_batches,
                incremental_vacuum_pages=vacuum_pages,
                dry_run=dry_run,
                now=current_dt,
            ),
        )
        success = success and ok

    from .gateway_persistence import (
        cleanup_expired_semantic_cache,
        cleanup_expired_tool_cache,
        maintain_database,
    )
    components["persistence"], ok = _run_component(
        "persistence",
        lambda: {
            "semantic_cache_deleted": cleanup_expired_semantic_cache(
                batch_size=batch_size, max_batches=max_batches, strict=True
            ) if not dry_run else 0,
            "tool_cache_deleted": cleanup_expired_tool_cache(
                batch_size=batch_size, max_batches=max_batches, strict=True
            ) if not dry_run else 0,
            **(maintain_database(incremental_vacuum_pages=vacuum_pages) if not dry_run else {}),
            "dry_run": dry_run,
        },
    )
    success = success and ok

    from .gateway_stats import cleanup_old_stats
    components["stats"], ok = _run_component(
        "stats",
        lambda: {
            "deleted": cleanup_old_stats(
                int((config.get("stats") or {}).get("retention_days") or 30),
                batch_size=batch_size,
                max_batches=max_batches,
            ) if not dry_run else 0,
            "dry_run": dry_run,
        },
    )
    success = success and ok

    from .gateway_agent_planner import _store
    components["planner"], ok = _run_component(
        "planner",
        lambda: _store().cleanup(
            retention_days=int(settings.get("planner_session_retention_days") or 30),
            max_sessions=int(settings.get("planner_session_max_rows") or 20_000),
            max_events=int(settings.get("planner_event_max_rows") or 100_000),
            batch_size=batch_size,
            max_batches=max_batches,
            incremental_vacuum_pages=vacuum_pages,
            dry_run=dry_run,
            now=now,
        ),
    )
    success = success and ok

    if bool(settings.get("runtime_cleanup_enabled", False)):
        runtime_dir = os.environ.get("GATEWAY_RUNTIME_DIR") or ".gateway_runtime"
        components["runtime"], ok = _run_component(
            "runtime",
            lambda: cleanup_stale_runtime(
                runtime_dir,
                retention_days=int(settings.get("runtime_retention_days") or 7),
                max_entries=int(settings.get("runtime_max_entries_per_run") or 20),
                max_entry_bytes=int(settings.get("runtime_max_entry_bytes") or 256 * 1024 * 1024),
                dry_run=bool(settings.get("runtime_cleanup_dry_run", True)) or dry_run,
                now=now,
            ),
        )
        success = success and ok
    else:
        components["runtime"] = {"ok": True, "enabled": False}

    completed = time.time()
    errors = [f"{name}: {value.get('error', '')}" for name, value in components.items() if not value.get("ok")]
    with _LOCK:
        _STATE["runs_total"] = int(_STATE.get("runs_total") or 0) + 1
        if not success:
            _STATE["failures_total"] = int(_STATE.get("failures_total") or 0) + 1
        _STATE.update({
            "last_started_at": completed - sum(float(item.get("duration_ms") or 0) for item in components.values()) / 1000,
            "last_completed_at": completed,
            "last_success": success,
            "last_error": "; ".join(errors)[:2000],
            "components": components,
        })
        return copy.deepcopy(_STATE)


__all__ = [
    "cleanup_stale_runtime",
    "maintenance_snapshot",
    "record_maintenance_crash",
    "run_gateway_maintenance",
]
