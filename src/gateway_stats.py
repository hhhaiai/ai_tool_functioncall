"""Q&A statistics and analytics module for the gateway.

Provides comprehensive tracking of requests, tool usage, cache performance,
and quality metrics for monitoring and optimization.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from .gateway_sqlite import secure_sqlite_artifacts, secure_sqlite_connect, set_secure_sqlite_journal_mode
except ImportError:  # pragma: no cover - legacy top-level import mode
    from gateway_sqlite import secure_sqlite_artifacts, secure_sqlite_connect, set_secure_sqlite_journal_mode

Json = dict[str, Any]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatsConfig:
    """Configuration for statistics tracking."""
    enabled: bool = True
    track_requests: bool = True
    track_tools: bool = True
    track_cache: bool = True
    track_quality: bool = True
    retention_days: int = 30
    snapshot_interval: int = 300  # seconds


def _stats_config(raw: dict | None = None) -> StatsConfig:
    """Parse stats config from raw dict."""
    if not raw:
        return StatsConfig()
    return StatsConfig(
        enabled=raw.get("enabled", True),
        track_requests=raw.get("track_requests", True),
        track_tools=raw.get("track_tools", True),
        track_cache=raw.get("track_cache", True),
        track_quality=raw.get("track_quality", True),
        retention_days=raw.get("retention_days", 30),
        snapshot_interval=raw.get("snapshot_interval", 300),
    )


# ---------------------------------------------------------------------------
# Database Connection
# ---------------------------------------------------------------------------

_db_lock = threading.RLock()
_db_conn: sqlite3.Connection | None = None


def _stats_db_path() -> Path:
    explicit = os.environ.get("GATEWAY_STATS_DB_PATH")
    if explicit:
        return Path(explicit)
    runtime_dir = Path(os.environ.get("GATEWAY_RUNTIME_DIR", ".gateway_runtime"))
    return runtime_dir / "stats.db"


def _get_db() -> sqlite3.Connection:
    """Get or create database connection."""
    global _db_conn
    if _db_conn is None:
        with _db_lock:
            if _db_conn is None:
                # Use persistent database file instead of :memory:
                db_path = _stats_db_path()
                explicit_path = bool(os.environ.get("GATEWAY_STATS_DB_PATH"))

                _db_conn = secure_sqlite_connect(
                    db_path,
                    private_parent=not explicit_path,
                    check_same_thread=False,
                )
                _db_conn.row_factory = sqlite3.Row

                # Configure for performance
                _db_conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                set_secure_sqlite_journal_mode(_db_conn, db_path, "WAL")
                _db_conn.execute("PRAGMA synchronous = NORMAL")

                _init_tables(_db_conn)
    return _db_conn


def _init_tables(conn: sqlite3.Connection):
    """Initialize database tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS request_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            path TEXT NOT NULL,
            method TEXT NOT NULL,
            status_code INTEGER,
            response_time REAL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_hit BOOLEAN DEFAULT 0,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS tool_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            tool_name TEXT NOT NULL,
            success BOOLEAN NOT NULL,
            execution_time REAL,
            error_type TEXT,
            arguments_keys TEXT
        );

        CREATE TABLE IF NOT EXISTS cache_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            cache_type TEXT NOT NULL,
            hit BOOLEAN NOT NULL,
            key_hash TEXT,
            similarity REAL
        );

        CREATE TABLE IF NOT EXISTS quality_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            request_id TEXT,
            completeness REAL,
            relevance REAL,
            clarity REAL,
            accuracy REAL,
            overall REAL,
            needs_refinement BOOLEAN
        );

        CREATE TABLE IF NOT EXISTS upstream_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            upstream_url TEXT NOT NULL,
            success BOOLEAN NOT NULL,
            response_time REAL,
            status_code INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_request_timestamp ON request_stats(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tool_timestamp ON tool_stats(timestamp);
        CREATE INDEX IF NOT EXISTS idx_cache_timestamp ON cache_stats(timestamp);
        CREATE INDEX IF NOT EXISTS idx_quality_timestamp ON quality_stats(timestamp);
        CREATE INDEX IF NOT EXISTS idx_upstream_timestamp ON upstream_stats(timestamp);
    """)


# ---------------------------------------------------------------------------
# Request Statistics
# ---------------------------------------------------------------------------

@dataclass
class RequestStat:
    """Statistics for a single request."""
    timestamp: float
    path: str
    method: str
    status_code: int | None = None
    response_time: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_hit: bool = False
    error: str | None = None


def record_request(stat: RequestStat, config: StatsConfig | None = None):
    """Record a request statistic."""
    if config is None:
        config = StatsConfig()

    if not config.enabled or not config.track_requests:
        return

    with _db_lock:
        db = _get_db()
        db.execute(
            """INSERT INTO request_stats
               (timestamp, path, method, status_code, response_time, input_tokens, output_tokens, cache_hit, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stat.timestamp, stat.path, stat.method, stat.status_code,
             stat.response_time, stat.input_tokens, stat.output_tokens,
             stat.cache_hit, stat.error),
        )
        db.commit()


def get_request_stats(
    start_time: float | None = None,
    end_time: float | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """Get request statistics summary."""
    db = _get_db()

    query = "SELECT * FROM request_stats WHERE 1=1"
    params = []

    if start_time is not None:
        query += " AND timestamp >= ?"
        params.append(start_time)
    if end_time is not None:
        query += " AND timestamp <= ?"
        params.append(end_time)
    if path is not None:
        query += " AND path = ?"
        params.append(path)

    rows = db.execute(query, params).fetchall()

    if not rows:
        return {
            "total_requests": 0,
            "success_rate": 0,
            "avg_response_time": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "cache_hit_rate": 0,
            "error_count": 0,
        }

    total = len(rows)
    success_count = sum(1 for r in rows if r["status_code"] and 200 <= r["status_code"] < 400)
    response_times = [r["response_time"] for r in rows if r["response_time"] is not None]
    input_tokens = [r["input_tokens"] for r in rows if r["input_tokens"] is not None]
    output_tokens = [r["output_tokens"] for r in rows if r["output_tokens"] is not None]
    cache_hits = sum(1 for r in rows if r["cache_hit"])
    error_count = sum(1 for r in rows if r["error"])

    return {
        "total_requests": total,
        "success_rate": success_count / total if total > 0 else 0,
        "avg_response_time": sum(response_times) / len(response_times) if response_times else 0,
        "total_input_tokens": sum(input_tokens),
        "total_output_tokens": sum(output_tokens),
        "cache_hit_rate": cache_hits / total if total > 0 else 0,
        "error_count": error_count,
        "paths": _group_by_field(rows, "path"),
    }


def _group_by_field(rows: list, field: str) -> dict[str, int]:
    """Group rows by a field and count occurrences."""
    groups = {}
    for row in rows:
        value = row[field]
        groups[value] = groups.get(value, 0) + 1
    return groups


# ---------------------------------------------------------------------------
# Tool Statistics
# ---------------------------------------------------------------------------

@dataclass
class ToolStat:
    """Statistics for a tool execution."""
    timestamp: float
    tool_name: str
    success: bool
    execution_time: float | None = None
    error_type: str | None = None
    arguments_keys: list[str] | None = None


def record_tool(stat: ToolStat, config: StatsConfig | None = None):
    """Record a tool execution statistic."""
    if config is None:
        config = StatsConfig()

    if not config.enabled or not config.track_tools:
        return

    with _db_lock:
        db = _get_db()
        db.execute(
            """INSERT INTO tool_stats
               (timestamp, tool_name, success, execution_time, error_type, arguments_keys)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (stat.timestamp, stat.tool_name, stat.success,
             stat.execution_time, stat.error_type,
             json.dumps(stat.arguments_keys) if stat.arguments_keys else None),
        )
        db.commit()


def get_tool_stats(
    start_time: float | None = None,
    end_time: float | None = None,
    tool_name: str | None = None,
) -> dict[str, Any]:
    """Get tool execution statistics summary."""
    db = _get_db()

    query = "SELECT * FROM tool_stats WHERE 1=1"
    params = []

    if start_time is not None:
        query += " AND timestamp >= ?"
        params.append(start_time)
    if end_time is not None:
        query += " AND timestamp <= ?"
        params.append(end_time)
    if tool_name is not None:
        query += " AND tool_name = ?"
        params.append(tool_name)

    rows = db.execute(query, params).fetchall()

    if not rows:
        return {
            "total_executions": 0,
            "success_rate": 0,
            "avg_execution_time": 0,
            "tools": {},
        }

    total = len(rows)
    success_count = sum(1 for r in rows if r["success"])
    exec_times = [r["execution_time"] for r in rows if r["execution_time"] is not None]

    # Group by tool
    tools = {}
    for row in rows:
        name = row["tool_name"]
        if name not in tools:
            tools[name] = {"total": 0, "success": 0, "errors": []}
        tools[name]["total"] += 1
        if row["success"]:
            tools[name]["success"] += 1
        elif row["error_type"]:
            tools[name]["errors"].append(row["error_type"])

    return {
        "total_executions": total,
        "success_rate": success_count / total if total > 0 else 0,
        "avg_execution_time": sum(exec_times) / len(exec_times) if exec_times else 0,
        "tools": tools,
    }


# ---------------------------------------------------------------------------
# Cache Statistics
# ---------------------------------------------------------------------------

@dataclass
class CacheStat:
    """Statistics for a cache operation."""
    timestamp: float
    cache_type: str  # "semantic" or "tool_result"
    hit: bool
    key_hash: str | None = None
    similarity: float | None = None


def record_cache(stat: CacheStat, config: StatsConfig | None = None):
    """Record a cache operation statistic."""
    if config is None:
        config = StatsConfig()

    if not config.enabled or not config.track_cache:
        return

    with _db_lock:
        db = _get_db()
        db.execute(
            """INSERT INTO cache_stats
               (timestamp, cache_type, hit, key_hash, similarity)
               VALUES (?, ?, ?, ?, ?)""",
            (stat.timestamp, stat.cache_type, stat.hit,
             stat.key_hash, stat.similarity),
        )
        db.commit()


def get_cache_stats(
    start_time: float | None = None,
    end_time: float | None = None,
    cache_type: str | None = None,
) -> dict[str, Any]:
    """Get cache statistics summary."""
    db = _get_db()

    query = "SELECT * FROM cache_stats WHERE 1=1"
    params = []

    if start_time is not None:
        query += " AND timestamp >= ?"
        params.append(start_time)
    if end_time is not None:
        query += " AND timestamp <= ?"
        params.append(end_time)
    if cache_type is not None:
        query += " AND cache_type = ?"
        params.append(cache_type)

    rows = db.execute(query, params).fetchall()

    if not rows:
        return {
            "total_operations": 0,
            "hit_rate": 0,
            "avg_similarity": 0,
            "by_type": {},
        }

    total = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    similarities = [r["similarity"] for r in rows if r["similarity"] is not None]

    # Group by type
    by_type = {}
    for row in rows:
        ctype = row["cache_type"]
        if ctype not in by_type:
            by_type[ctype] = {"total": 0, "hits": 0}
        by_type[ctype]["total"] += 1
        if row["hit"]:
            by_type[ctype]["hits"] += 1

    return {
        "total_operations": total,
        "hit_rate": hits / total if total > 0 else 0,
        "avg_similarity": sum(similarities) / len(similarities) if similarities else 0,
        "by_type": by_type,
    }


# ---------------------------------------------------------------------------
# Quality Statistics
# ---------------------------------------------------------------------------

@dataclass
class QualityStat:
    """Statistics for answer quality."""
    timestamp: float
    request_id: str | None = None
    completeness: float = 0.0
    relevance: float = 0.0
    clarity: float = 0.0
    accuracy: float = 0.0
    overall: float = 0.0
    needs_refinement: bool = False


def record_quality(stat: QualityStat, config: StatsConfig | None = None):
    """Record a quality assessment statistic."""
    if config is None:
        config = StatsConfig()

    if not config.enabled or not config.track_quality:
        return

    with _db_lock:
        db = _get_db()
        db.execute(
            """INSERT INTO quality_stats
               (timestamp, request_id, completeness, relevance, clarity, accuracy, overall, needs_refinement)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (stat.timestamp, stat.request_id, stat.completeness,
             stat.relevance, stat.clarity, stat.accuracy,
             stat.overall, stat.needs_refinement),
        )
        db.commit()


def get_quality_stats(
    start_time: float | None = None,
    end_time: float | None = None,
) -> dict[str, Any]:
    """Get quality statistics summary."""
    db = _get_db()

    query = "SELECT * FROM quality_stats WHERE 1=1"
    params = []

    if start_time is not None:
        query += " AND timestamp >= ?"
        params.append(start_time)
    if end_time is not None:
        query += " AND timestamp <= ?"
        params.append(end_time)

    rows = db.execute(query, params).fetchall()

    if not rows:
        return {
            "total_assessments": 0,
            "avg_completeness": 0,
            "avg_relevance": 0,
            "avg_clarity": 0,
            "avg_accuracy": 0,
            "avg_overall": 0,
            "refinement_rate": 0,
        }

    total = len(rows)
    completeness = [r["completeness"] for r in rows]
    relevance = [r["relevance"] for r in rows]
    clarity = [r["clarity"] for r in rows]
    accuracy = [r["accuracy"] for r in rows]
    overall = [r["overall"] for r in rows]
    refinements = sum(1 for r in rows if r["needs_refinement"])

    return {
        "total_assessments": total,
        "avg_completeness": sum(completeness) / total,
        "avg_relevance": sum(relevance) / total,
        "avg_clarity": sum(clarity) / total,
        "avg_accuracy": sum(accuracy) / total,
        "avg_overall": sum(overall) / total,
        "refinement_rate": refinements / total,
    }


# ---------------------------------------------------------------------------
# Upstream Statistics
# ---------------------------------------------------------------------------

@dataclass
class UpstreamStat:
    """Statistics for upstream requests."""
    timestamp: float
    upstream_url: str
    success: bool
    response_time: float | None = None
    status_code: int | None = None


def record_upstream(stat: UpstreamStat, config: StatsConfig | None = None):
    """Record an upstream request statistic."""
    if config is None:
        config = StatsConfig()

    if not config.enabled:
        return

    with _db_lock:
        db = _get_db()
        db.execute(
            """INSERT INTO upstream_stats
               (timestamp, upstream_url, success, response_time, status_code)
               VALUES (?, ?, ?, ?, ?)""",
            (stat.timestamp, stat.upstream_url, stat.success,
             stat.response_time, stat.status_code),
        )
        db.commit()


def get_upstream_stats(
    start_time: float | None = None,
    end_time: float | None = None,
    upstream_url: str | None = None,
) -> dict[str, Any]:
    """Get upstream statistics summary."""
    db = _get_db()

    query = "SELECT * FROM upstream_stats WHERE 1=1"
    params = []

    if start_time is not None:
        query += " AND timestamp >= ?"
        params.append(start_time)
    if end_time is not None:
        query += " AND timestamp <= ?"
        params.append(end_time)
    if upstream_url is not None:
        query += " AND upstream_url = ?"
        params.append(upstream_url)

    rows = db.execute(query, params).fetchall()

    if not rows:
        return {
            "total_requests": 0,
            "success_rate": 0,
            "avg_response_time": 0,
            "by_upstream": {},
        }

    total = len(rows)
    success_count = sum(1 for r in rows if r["success"])
    response_times = [r["response_time"] for r in rows if r["response_time"] is not None]

    # Group by upstream
    by_upstream = {}
    for row in rows:
        url = row["upstream_url"]
        if url not in by_upstream:
            by_upstream[url] = {"total": 0, "success": 0, "avg_time": 0, "times": []}
        by_upstream[url]["total"] += 1
        if row["success"]:
            by_upstream[url]["success"] += 1
        if row["response_time"]:
            by_upstream[url]["times"].append(row["response_time"])

    # Calculate averages
    for url, data in by_upstream.items():
        if data["times"]:
            data["avg_time"] = sum(data["times"]) / len(data["times"])
        del data["times"]

    return {
        "total_requests": total,
        "success_rate": success_count / total if total > 0 else 0,
        "avg_response_time": sum(response_times) / len(response_times) if response_times else 0,
        "by_upstream": by_upstream,
    }


# ---------------------------------------------------------------------------
# Comprehensive Dashboard
# ---------------------------------------------------------------------------

@dataclass
class DashboardData:
    """Complete dashboard data."""
    timestamp: float
    requests: dict[str, Any]
    tools: dict[str, Any]
    cache: dict[str, Any]
    quality: dict[str, Any]
    upstream: dict[str, Any]


def get_dashboard(
    start_time: float | None = None,
    end_time: float | None = None,
) -> DashboardData:
    """Get complete dashboard data."""
    return DashboardData(
        timestamp=time.time(),
        requests=get_request_stats(start_time, end_time),
        tools=get_tool_stats(start_time, end_time),
        cache=get_cache_stats(start_time, end_time),
        quality=get_quality_stats(start_time, end_time),
        upstream=get_upstream_stats(start_time, end_time),
    )


def get_dashboard_json(
    start_time: float | None = None,
    end_time: float | None = None,
) -> str:
    """Get dashboard data as JSON string."""
    dashboard = get_dashboard(start_time, end_time)
    return json.dumps({
        "timestamp": dashboard.timestamp,
        "requests": dashboard.requests,
        "tools": dashboard.tools,
        "cache": dashboard.cache,
        "quality": dashboard.quality,
        "upstream": dashboard.upstream,
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Trend Analysis
# ---------------------------------------------------------------------------

def get_hourly_trends(hours: int = 24) -> dict[str, list]:
    """Get hourly trends for the last N hours."""
    db = _get_db()
    cutoff = time.time() - (hours * 3600)

    # Request trends
    request_rows = db.execute(
        """SELECT
             CAST(timestamp / 3600 AS INTEGER) * 3600 as hour,
             COUNT(*) as count,
             AVG(response_time) as avg_time,
             SUM(CASE WHEN status_code BETWEEN 200 AND 399 THEN 1 ELSE 0 END) as success
           FROM request_stats
           WHERE timestamp >= ?
           GROUP BY hour
           ORDER BY hour""",
        (cutoff,),
    ).fetchall()

    # Tool trends
    tool_rows = db.execute(
        """SELECT
             CAST(timestamp / 3600 AS INTEGER) * 3600 as hour,
             COUNT(*) as count,
             SUM(CASE WHEN success THEN 1 ELSE 0 END) as success
           FROM tool_stats
           WHERE timestamp >= ?
           GROUP BY hour
           ORDER BY hour""",
        (cutoff,),
    ).fetchall()

    # Cache trends
    cache_rows = db.execute(
        """SELECT
             CAST(timestamp / 3600 AS INTEGER) * 3600 as hour,
             COUNT(*) as count,
             SUM(CASE WHEN hit THEN 1 ELSE 0 END) as hits
           FROM cache_stats
           WHERE timestamp >= ?
           GROUP BY hour
           ORDER BY hour""",
        (cutoff,),
    ).fetchall()

    return {
        "hours": [r["hour"] for r in request_rows],
        "requests": {
            "total": [r["count"] for r in request_rows],
            "avg_time": [r["avg_time"] for r in request_rows],
            "success": [r["success"] for r in request_rows],
        },
        "tools": {
            "total": [r["count"] for r in tool_rows],
            "success": [r["success"] for r in tool_rows],
        },
        "cache": {
            "total": [r["count"] for r in cache_rows],
            "hits": [r["hits"] for r in cache_rows],
        },
    }


# ---------------------------------------------------------------------------
# Top Queries Analysis
# ---------------------------------------------------------------------------

def get_top_paths(limit: int = 10) -> list[dict[str, Any]]:
    """Get most frequently requested paths."""
    db = _get_db()
    rows = db.execute(
        """SELECT path, COUNT(*) as count,
                  AVG(response_time) as avg_time,
                  SUM(CASE WHEN status_code BETWEEN 200 AND 399 THEN 1 ELSE 0 END) as success
           FROM request_stats
           GROUP BY path
           ORDER BY count DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    return [
        {
            "path": r["path"],
            "count": r["count"],
            "avg_time": r["avg_time"],
            "success_rate": r["success"] / r["count"] if r["count"] > 0 else 0,
        }
        for r in rows
    ]


def get_top_tools(limit: int = 10) -> list[dict[str, Any]]:
    """Get most frequently used tools."""
    db = _get_db()
    rows = db.execute(
        """SELECT tool_name, COUNT(*) as count,
                  AVG(execution_time) as avg_time,
                  SUM(CASE WHEN success THEN 1 ELSE 0 END) as success
           FROM tool_stats
           GROUP BY tool_name
           ORDER BY count DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    return [
        {
            "tool": r["tool_name"],
            "count": r["count"],
            "avg_time": r["avg_time"],
            "success_rate": r["success"] / r["count"] if r["count"] > 0 else 0,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Export Functions
# ---------------------------------------------------------------------------

def export_stats_csv(stat_type: str = "requests") -> str:
    """Export statistics as CSV."""
    db = _get_db()

    if stat_type == "requests":
        rows = db.execute("SELECT * FROM request_stats ORDER BY timestamp DESC").fetchall()
        headers = "timestamp,path,method,status_code,response_time,input_tokens,output_tokens,cache_hit,error\n"
    elif stat_type == "tools":
        rows = db.execute("SELECT * FROM tool_stats ORDER BY timestamp DESC").fetchall()
        headers = "timestamp,tool_name,success,execution_time,error_type,arguments_keys\n"
    elif stat_type == "cache":
        rows = db.execute("SELECT * FROM cache_stats ORDER BY timestamp DESC").fetchall()
        headers = "timestamp,cache_type,hit,key_hash,similarity\n"
    elif stat_type == "quality":
        rows = db.execute("SELECT * FROM quality_stats ORDER BY timestamp DESC").fetchall()
        headers = "timestamp,request_id,completeness,relevance,clarity,accuracy,overall,needs_refinement\n"
    else:
        return ""

    csv = headers
    for row in rows:
        csv += ",".join(str(row[key]) for key in row.keys()) + "\n"

    return csv


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_old_stats(
    retention_days: int = 30,
    *,
    batch_size: int = 1_000,
    max_batches: int = 4,
) -> int:
    """Remove statistics older than retention period."""
    with _db_lock:
        db = _get_db()
        cutoff = time.time() - (retention_days * 86400)
        batch_size = max(1, min(int(batch_size), 100_000))
        max_batches = max(1, min(int(max_batches), 100))

        tables = ["request_stats", "tool_stats", "cache_stats", "quality_stats", "upstream_stats"]
        deleted = 0
        for table in tables:
            for _ in range(max_batches):
                result = db.execute(
                    f"DELETE FROM {table} WHERE id IN ("
                    f"SELECT id FROM {table} WHERE timestamp < ? ORDER BY id LIMIT ?)",
                    (cutoff, batch_size),
                )
                changed = max(0, int(result.rowcount))
                deleted += changed
                db.commit()
                if changed < batch_size:
                    break

        db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        secure_sqlite_artifacts(_stats_db_path())
        return deleted


def reset_stats():
    """Reset all statistics."""
    with _db_lock:
        db = _get_db()
        tables = ["request_stats", "tool_stats", "cache_stats", "quality_stats", "upstream_stats"]
        for table in tables:
            db.execute(f"DELETE FROM {table}")
        db.commit()
