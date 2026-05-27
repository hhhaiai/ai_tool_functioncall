"""Tests for Q&A statistics and analytics module."""
from __future__ import annotations

import json
import time

import pytest

from src.gateway_stats import (
    CacheStat,
    DashboardData,
    QualityStat,
    RequestStat,
    StatsConfig,
    ToolStat,
    UpstreamStat,
    _stats_config,
    cleanup_old_stats,
    get_cache_stats,
    get_dashboard,
    get_dashboard_json,
    get_hourly_trends,
    get_quality_stats,
    get_request_stats,
    get_tool_stats,
    get_top_paths,
    get_top_tools,
    get_upstream_stats,
    record_cache,
    record_quality,
    record_request,
    record_tool,
    record_upstream,
    reset_stats,
    export_stats_csv,
)


@pytest.fixture(autouse=True)
def clean_db():
    """Reset database before each test."""
    reset_stats()
    yield
    reset_stats()


class TestStatsConfig:
    def test_default_config(self):
        config = StatsConfig()
        assert config.enabled is True
        assert config.track_requests is True
        assert config.track_tools is True
        assert config.track_cache is True
        assert config.track_quality is True
        assert config.retention_days == 30
        assert config.snapshot_interval == 300

    def test_custom_config(self):
        config = StatsConfig(enabled=False, retention_days=7)
        assert config.enabled is False
        assert config.retention_days == 7

    def test_parse_config_from_dict(self):
        raw = {"enabled": True, "retention_days": 14}
        config = _stats_config(raw)
        assert config.retention_days == 14

    def test_parse_config_from_none(self):
        config = _stats_config(None)
        assert config.enabled is True


class TestRequestStats:
    def test_record_request(self):
        stat = RequestStat(
            timestamp=time.time(),
            path="/v1/chat/completions",
            method="POST",
            status_code=200,
            response_time=0.5,
        )
        record_request(stat)

    def test_get_empty_stats(self):
        stats = get_request_stats()
        assert stats["total_requests"] == 0

    def test_record_and_get(self):
        now = time.time()
        for i in range(5):
            stat = RequestStat(
                timestamp=now - i,
                path="/v1/chat/completions",
                method="POST",
                status_code=200 if i < 4 else 500,
                response_time=0.1 * (i + 1),
                error="Internal Server Error" if i == 4 else None,
            )
            record_request(stat)

        stats = get_request_stats()
        assert stats["total_requests"] == 5
        assert stats["success_rate"] == 0.8
        assert stats["error_count"] == 1

    def test_filter_by_time(self):
        now = time.time()
        record_request(RequestStat(timestamp=now - 100, path="/old", method="GET"))
        record_request(RequestStat(timestamp=now, path="/new", method="GET"))

        stats = get_request_stats(start_time=now - 50)
        assert stats["total_requests"] == 1

    def test_filter_by_path(self):
        now = time.time()
        record_request(RequestStat(timestamp=now, path="/v1/chat", method="POST"))
        record_request(RequestStat(timestamp=now, path="/v1/messages", method="POST"))

        stats = get_request_stats(path="/v1/chat")
        assert stats["total_requests"] == 1


class TestToolStats:
    def test_record_tool(self):
        stat = ToolStat(
            timestamp=time.time(),
            tool_name="Read",
            success=True,
            execution_time=0.1,
        )
        record_tool(stat)

    def test_get_empty_stats(self):
        stats = get_tool_stats()
        assert stats["total_executions"] == 0

    def test_record_and_get(self):
        now = time.time()
        tools = ["Read", "Read", "Write", "Bash"]
        successes = [True, True, True, False]

        for tool, success in zip(tools, successes):
            record_tool(ToolStat(
                timestamp=now,
                tool_name=tool,
                success=success,
                execution_time=0.1,
                error_type="timeout" if not success else None,
            ))

        stats = get_tool_stats()
        assert stats["total_executions"] == 4
        assert stats["success_rate"] == 0.75
        assert "Read" in stats["tools"]
        assert stats["tools"]["Read"]["total"] == 2

    def test_filter_by_tool(self):
        now = time.time()
        record_tool(ToolStat(timestamp=now, tool_name="Read", success=True))
        record_tool(ToolStat(timestamp=now, tool_name="Write", success=True))

        stats = get_tool_stats(tool_name="Read")
        assert stats["total_executions"] == 1


class TestCacheStats:
    def test_record_cache(self):
        stat = CacheStat(
            timestamp=time.time(),
            cache_type="semantic",
            hit=True,
            similarity=0.95,
        )
        record_cache(stat)

    def test_get_empty_stats(self):
        stats = get_cache_stats()
        assert stats["total_operations"] == 0

    def test_record_and_get(self):
        now = time.time()
        for i in range(10):
            record_cache(CacheStat(
                timestamp=now,
                cache_type="semantic" if i < 7 else "tool_result",
                hit=i < 6,
                similarity=0.9 if i < 6 else None,
            ))

        stats = get_cache_stats()
        assert stats["total_operations"] == 10
        assert stats["hit_rate"] == 0.6
        assert "semantic" in stats["by_type"]

    def test_filter_by_type(self):
        now = time.time()
        record_cache(CacheStat(timestamp=now, cache_type="semantic", hit=True))
        record_cache(CacheStat(timestamp=now, cache_type="tool_result", hit=False))

        stats = get_cache_stats(cache_type="semantic")
        assert stats["total_operations"] == 1


class TestQualityStats:
    def test_record_quality(self):
        stat = QualityStat(
            timestamp=time.time(),
            completeness=0.8,
            relevance=0.9,
            clarity=0.7,
            accuracy=0.85,
            overall=0.8,
            needs_refinement=False,
        )
        record_quality(stat)

    def test_get_empty_stats(self):
        stats = get_quality_stats()
        assert stats["total_assessments"] == 0

    def test_record_and_get(self):
        now = time.time()
        for i in range(5):
            record_quality(QualityStat(
                timestamp=now,
                completeness=0.7 + i * 0.05,
                relevance=0.8,
                clarity=0.75,
                accuracy=0.85,
                overall=0.75 + i * 0.05,
                needs_refinement=i < 2,
            ))

        stats = get_quality_stats()
        assert stats["total_assessments"] == 5
        assert stats["refinement_rate"] == 0.4


class TestUpstreamStats:
    def test_record_upstream(self):
        stat = UpstreamStat(
            timestamp=time.time(),
            upstream_url="http://upstream1.com",
            success=True,
            response_time=0.5,
        )
        record_upstream(stat)

    def test_get_empty_stats(self):
        stats = get_upstream_stats()
        assert stats["total_requests"] == 0

    def test_record_and_get(self):
        now = time.time()
        urls = ["http://up1.com", "http://up1.com", "http://up2.com"]
        successes = [True, False, True]

        for url, success in zip(urls, successes):
            record_upstream(UpstreamStat(
                timestamp=now,
                upstream_url=url,
                success=success,
                response_time=0.5,
            ))

        stats = get_upstream_stats()
        assert stats["total_requests"] == 3
        assert stats["success_rate"] == 2/3
        assert "http://up1.com" in stats["by_upstream"]


class TestDashboard:
    def test_get_dashboard(self):
        now = time.time()
        record_request(RequestStat(timestamp=now, path="/test", method="GET", status_code=200))

        dashboard = get_dashboard()
        assert isinstance(dashboard, DashboardData)
        assert dashboard.requests["total_requests"] == 1

    def test_get_dashboard_json(self):
        now = time.time()
        record_request(RequestStat(timestamp=now, path="/test", method="GET", status_code=200))

        json_str = get_dashboard_json()
        data = json.loads(json_str)
        assert "requests" in data
        assert "tools" in data
        assert "cache" in data


class TestTrends:
    def test_hourly_trends(self):
        trends = get_hourly_trends(hours=24)
        assert "hours" in trends
        assert "requests" in trends
        assert "tools" in trends
        assert "cache" in trends


class TestTopQueries:
    def test_top_paths(self):
        now = time.time()
        for _ in range(5):
            record_request(RequestStat(timestamp=now, path="/v1/chat", method="POST"))
        for _ in range(3):
            record_request(RequestStat(timestamp=now, path="/v1/messages", method="POST"))

        top = get_top_paths(limit=10)
        assert len(top) == 2
        assert top[0]["path"] == "/v1/chat"
        assert top[0]["count"] == 5

    def test_top_tools(self):
        now = time.time()
        for _ in range(10):
            record_tool(ToolStat(timestamp=now, tool_name="Read", success=True))
        for _ in range(5):
            record_tool(ToolStat(timestamp=now, tool_name="Write", success=True))

        top = get_top_tools(limit=10)
        assert len(top) == 2
        assert top[0]["tool"] == "Read"
        assert top[0]["count"] == 10


class TestExport:
    def test_export_requests_csv(self):
        now = time.time()
        record_request(RequestStat(timestamp=now, path="/test", method="GET", status_code=200))

        csv = export_stats_csv("requests")
        assert "timestamp" in csv
        assert "/test" in csv

    def test_export_tools_csv(self):
        now = time.time()
        record_tool(ToolStat(timestamp=now, tool_name="Read", success=True))

        csv = export_stats_csv("tools")
        assert "tool_name" in csv
        assert "Read" in csv

    def test_export_cache_csv(self):
        now = time.time()
        record_cache(CacheStat(timestamp=now, cache_type="semantic", hit=True))

        csv = export_stats_csv("cache")
        assert "cache_type" in csv
        assert "semantic" in csv


class TestCleanup:
    def test_cleanup_old_stats(self):
        now = time.time()
        # Old record
        record_request(RequestStat(timestamp=now - 40 * 86400, path="/old", method="GET"))
        # New record
        record_request(RequestStat(timestamp=now, path="/new", method="GET"))

        cleanup_old_stats(retention_days=30)

        stats = get_request_stats()
        assert stats["total_requests"] == 1

    def test_reset_stats(self):
        now = time.time()
        record_request(RequestStat(timestamp=now, path="/test", method="GET"))
        record_tool(ToolStat(timestamp=now, tool_name="Read", success=True))

        reset_stats()

        assert get_request_stats()["total_requests"] == 0
        assert get_tool_stats()["total_executions"] == 0


@pytest.mark.integration
class TestStatsIntegration:
    def test_full_lifecycle(self):
        """Test complete statistics lifecycle."""
        now = time.time()

        # Record various stats
        record_request(RequestStat(
            timestamp=now,
            path="/v1/chat/completions",
            method="POST",
            status_code=200,
            response_time=0.5,
            input_tokens=100,
            output_tokens=50,
            cache_hit=False,
        ))

        record_tool(ToolStat(
            timestamp=now,
            tool_name="Read",
            success=True,
            execution_time=0.1,
        ))

        record_cache(CacheStat(
            timestamp=now,
            cache_type="semantic",
            hit=True,
            similarity=0.95,
        ))

        record_quality(QualityStat(
            timestamp=now,
            completeness=0.8,
            relevance=0.9,
            overall=0.85,
        ))

        record_upstream(UpstreamStat(
            timestamp=now,
            upstream_url="http://upstream.com",
            success=True,
            response_time=0.3,
        ))

        # Verify all stats
        assert get_request_stats()["total_requests"] == 1
        assert get_tool_stats()["total_executions"] == 1
        assert get_cache_stats()["total_operations"] == 1
        assert get_quality_stats()["total_assessments"] == 1
        assert get_upstream_stats()["total_requests"] == 1

        # Get dashboard
        dashboard = get_dashboard()
        assert dashboard.requests["total_requests"] == 1
        assert dashboard.tools["total_executions"] == 1

    def test_concurrent_recording(self):
        """Test concurrent stat recording."""
        import threading

        now = time.time()
        errors = []
        lock = threading.Lock()

        def record_stats(thread_id):
            try:
                for i in range(10):
                    with lock:
                        record_request(RequestStat(
                            timestamp=now,
                            path=f"/test/{thread_id}",
                            method="GET",
                            status_code=200,
                        ))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_stats, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert get_request_stats()["total_requests"] == 50
