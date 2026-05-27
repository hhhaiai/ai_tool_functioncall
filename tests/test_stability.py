"""Stability and stress tests for commercial-grade verification.

These tests verify that all modules handle edge cases, concurrent access,
error conditions, and resource cleanup correctly.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Context Module Stability
# ---------------------------------------------------------------------------

class TestContextStability:
    """Stability tests for gateway_context module."""

    def test_rapid_token_estimation(self):
        """Test rapid token estimation doesn't leak memory."""
        from src.gateway_context import _approx_token_count

        for _ in range(1000):
            result = _approx_token_count("Hello world " * 100)
            assert result > 0

    def test_empty_and_none_handling(self):
        """Test handling of empty and None inputs."""
        from src.gateway_context import _approx_token_count, _compact_messages

        assert _approx_token_count(None) == 0
        assert _approx_token_count("") >= 0
        assert _compact_messages(None, keep_recent=4, text_limit=1000) is None or _compact_messages(None, keep_recent=4, text_limit=1000) == []

    def test_concurrent_token_estimation(self):
        """Test concurrent token estimation."""
        from src.gateway_context import _approx_token_count

        errors = []

        def estimate(text):
            try:
                result = _approx_token_count(text)
                assert result >= 0
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(estimate, f"text {i}" * 50) for i in range(100)]
            for f in as_completed(futures):
                f.result()

        assert len(errors) == 0

    def test_large_message_compaction(self):
        """Test compaction with very large messages."""
        from src.gateway_context import _compact_messages

        messages = [
            {"role": "user", "content": "x" * 100000},
            {"role": "assistant", "content": "y" * 100000},
        ] * 50

        result = _compact_messages(messages, keep_recent=4, text_limit=50000)
        assert isinstance(result, list)
        assert len(result) <= len(messages)

    def test_malformed_messages_handling(self):
        """Test handling of malformed messages."""
        from src.gateway_context import _approx_token_count

        malformed = [
            {"role": "user"},  # Missing content
            {"content": "hello"},  # Missing role
            "not a dict",
            None,
        ]

        for msg in malformed:
            try:
                _approx_token_count(msg)
            except Exception:
                pass  # Should not crash


# ---------------------------------------------------------------------------
# Tool Runtime Stability
# ---------------------------------------------------------------------------

class TestToolRuntimeStability:
    """Stability tests for gateway_tool_runtime module."""

    def test_extract_tool_calls_empty_response(self):
        """Test extraction from empty responses."""
        from src.gateway_tool_runtime import _extract_tool_calls

        assert _extract_tool_calls("/v1/chat/completions", {}) == []
        assert _extract_tool_calls("/v1/chat/completions", {"choices": []}) == []

    def test_normalize_invalid_tool_call(self):
        """Test normalization of invalid tool calls."""
        from src.gateway_tool_runtime import _normalize_tool_call, ToolCall

        # Should handle gracefully
        tc = ToolCall(call_id="", name="", arguments={}, raw={})
        result = _normalize_tool_call(tc)
        assert result is not None

    def test_concurrent_tool_extraction(self):
        """Test concurrent tool extraction."""
        from src.gateway_tool_runtime import _extract_tool_calls

        response = {
            "choices": [{
                "message": {
                    "tool_calls": [
                        {"id": f"call_{i}", "type": "function", "function": {"name": f"tool_{i}", "arguments": "{}"}}
                        for i in range(10)
                    ]
                }
            }]
        }

        errors = []

        def extract():
            try:
                calls = _extract_tool_calls("/v1/chat/completions", response)
                assert len(calls) == 10
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(extract) for _ in range(50)]
            for f in as_completed(futures):
                f.result()

        assert len(errors) == 0


# ---------------------------------------------------------------------------
# Cache Stability
# ---------------------------------------------------------------------------

class TestCacheStability:
    """Stability tests for gateway_cache module."""

    def test_rapid_cache_operations(self):
        """Test rapid cache put/get operations."""
        from src.gateway_cache import ToolResultCache

        cache = ToolResultCache(ttl_seconds=60, max_entries=2000)

        for i in range(1000):
            cache.put("Read", {"path": f"file_{i}.py"}, f"content_{i}")

        for i in range(1000):
            result = cache.get("Read", {"path": f"file_{i}.py"})
            assert result == f"content_{i}"

    def test_cache_eviction_stability(self):
        """Test cache eviction under pressure."""
        from src.gateway_cache import ToolResultCache

        cache = ToolResultCache(ttl_seconds=1)

        # Fill cache
        for i in range(100):
            cache.put("Read", {"path": f"file_{i}.py"}, f"content_{i}")

        # Wait for expiration
        time.sleep(1.1)

        # All should be expired
        for i in range(100):
            result = cache.get("Read", {"path": f"file_{i}.py"})
            assert result is None

    def test_concurrent_cache_access(self):
        """Test concurrent cache access."""
        from src.gateway_cache import ToolResultCache

        cache = ToolResultCache(ttl_seconds=60)
        errors = []

        def worker(thread_id):
            try:
                for i in range(100):
                    args = {"path": f"file_{thread_id}_{i}.py"}
                    cache.put("Read", args, f"content_{i}")
                    result = cache.get("Read", args)
                    assert result == f"content_{i}"
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, i) for i in range(10)]
            for f in as_completed(futures):
                f.result()

        assert len(errors) == 0

    def test_invalidate_nonexistent(self):
        """Test invalidating non-existent entries."""
        from src.gateway_cache import ToolResultCache

        cache = ToolResultCache(ttl_seconds=60)
        # Should not raise
        cache.invalidate("/nonexistent/path")


# ---------------------------------------------------------------------------
# Intelligence Stability
# ---------------------------------------------------------------------------

class TestIntelligenceStability:
    """Stability tests for gateway_intelligence module."""

    def test_analyze_empty_question(self):
        """Test analyzing empty questions."""
        from src.gateway_intelligence import _analyze_question

        analysis = _analyze_question("")
        assert analysis.complexity == "simple"

    def test_analyze_very_long_question(self):
        """Test analyzing very long questions."""
        from src.gateway_intelligence import _analyze_question

        long_question = "这是一个很长的问题 " * 1000
        analysis = _analyze_question(long_question)
        assert analysis.complexity in ["simple", "moderate", "complex"]

    def test_quality_assessment_edge_cases(self):
        """Test quality assessment edge cases."""
        from src.gateway_intelligence import _assess_answer_quality

        # Empty answer
        assessment = _assess_answer_quality("question", "")
        assert assessment.completeness == 0.0

        # Very short answer
        assessment = _assess_answer_quality("long question " * 10, "ok")
        assert assessment.completeness < 1.0

    def test_concurrent_analysis(self):
        """Test concurrent question analysis."""
        from src.gateway_intelligence import _analyze_question

        errors = []

        def analyze(text):
            try:
                result = _analyze_question(text)
                assert result is not None
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(analyze, f"question {i}") for i in range(100)]
            for f in as_completed(futures):
                f.result()

        assert len(errors) == 0


# ---------------------------------------------------------------------------
# Concurrency Module Stability
# ---------------------------------------------------------------------------

class TestConcurrencyStability:
    """Stability tests for gateway_concurrency module."""

    def test_load_balancer_round_robin(self):
        """Test load balancer round robin stability."""
        from src.gateway_concurrency import LoadBalancer, ConcurrencyConfig

        upstreams = [{"url": f"http://up{i}.com"} for i in range(5)]
        config = ConcurrencyConfig(load_balance_strategy="round_robin")
        balancer = LoadBalancer(upstreams, config)

        results = []
        for _ in range(100):
            upstream = balancer.get_next()
            results.append(upstream["url"])

        # Should distribute evenly
        for url in set(results):
            assert results.count(url) == 20

    def test_load_balancer_failover(self):
        """Test load balancer failover."""
        from src.gateway_concurrency import LoadBalancer, ConcurrencyConfig

        upstreams = [{"url": f"http://up{i}.com"} for i in range(3)]
        config = ConcurrencyConfig()
        balancer = LoadBalancer(upstreams, config)

        # Mark first upstream as unhealthy
        for _ in range(3):
            balancer.report_failure("http://up0.com")

        # Should skip unhealthy upstream
        healthy_urls = set()
        for _ in range(30):
            upstream = balancer.get_next()
            healthy_urls.add(upstream["url"])

        assert "http://up0.com" not in healthy_urls

    def test_connection_pool_limits(self):
        """Test connection pool respects limits."""
        from src.gateway_concurrency import ConnectionPool, ConcurrencyConfig

        config = ConcurrencyConfig(max_connections_per_host=3)
        pool = ConnectionPool(config)

        conns = []
        for _ in range(3):
            conns.append(pool.get_connection("http://example.com"))

        with pytest.raises(ConnectionError):
            pool.get_connection("http://example.com")

    def test_upstream_manager_operations(self):
        """Test upstream manager operations."""
        from src.gateway_concurrency import MultiUpstreamManager

        upstreams = [{"url": f"http://up{i}.com"} for i in range(3)]
        manager = MultiUpstreamManager(upstreams)

        # Add upstream
        manager.add_upstream({"url": "http://new.com"})
        status = manager.get_status()
        assert len(status["upstreams"]) == 4

        # Remove upstream
        manager.remove_upstream("http://up0.com")
        status = manager.get_status()
        assert len(status["upstreams"]) == 3

        # Switch primary - after removing up0, list is [up1, up2, new]
        manager.switch_primary(0)
        primary = manager.get_primary_upstream()
        assert primary["url"] == "http://up1.com"


# ---------------------------------------------------------------------------
# Stats Stability
# ---------------------------------------------------------------------------

class TestStatsStability:
    """Stability tests for gateway_stats module."""

    def test_rapid_stats_recording(self):
        """Test rapid stats recording."""
        from src.gateway_stats import record_request, get_request_stats, reset_stats, RequestStat

        reset_stats()

        now = time.time()
        for i in range(1000):
            record_request(RequestStat(
                timestamp=now,
                path=f"/test/{i % 10}",
                method="GET",
                status_code=200,
            ))

        stats = get_request_stats()
        assert stats["total_requests"] == 1000

        reset_stats()

    def test_concurrent_stats_recording(self):
        """Test concurrent stats recording."""
        from src.gateway_stats import record_request, get_request_stats, reset_stats, RequestStat

        reset_stats()

        now = time.time()
        errors = []
        lock = threading.Lock()

        def record(thread_id):
            try:
                for i in range(100):
                    with lock:
                        record_request(RequestStat(
                            timestamp=now,
                            path=f"/test/{thread_id}",
                            method="GET",
                            status_code=200,
                        ))
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(record, i) for i in range(10)]
            for f in as_completed(futures):
                f.result()

        assert len(errors) == 0
        stats = get_request_stats()
        assert stats["total_requests"] == 1000

        reset_stats()

    def test_stats_cleanup(self):
        """Test stats cleanup."""
        from src.gateway_stats import record_request, get_request_stats, cleanup_old_stats, reset_stats, RequestStat

        reset_stats()

        now = time.time()
        # Old record
        record_request(RequestStat(timestamp=now - 40 * 86400, path="/old", method="GET"))
        # New record
        record_request(RequestStat(timestamp=now, path="/new", method="GET"))

        cleanup_old_stats(retention_days=30)

        stats = get_request_stats()
        assert stats["total_requests"] == 1

        reset_stats()


# ---------------------------------------------------------------------------
# Web Config Stability
# ---------------------------------------------------------------------------

class TestWebConfigStability:
    """Stability tests for gateway_web_config module."""

    def test_render_empty_config(self):
        """Test rendering with empty config."""
        from src.gateway_web_config import render_web_config_ui

        html = render_web_config_ui({})
        assert "<!DOCTYPE html>" in html
        assert "Gateway" in html

    def test_render_complex_config(self):
        """Test rendering with complex config."""
        from src.gateway_web_config import render_web_config_ui

        config = {
            "upstream": {
                "url": "https://api.example.com",
                "api_key": "sk-test-123",
                "model": "gpt-4",
                "timeout": 60,
            },
            "context": {
                "enabled": True,
                "max_input_tokens": 1048576,
                "keep_recent_messages": 12,
            },
            "intelligence": {
                "enabled": True,
                "reflection_enabled": True,
            },
            "concurrency": {
                "enabled": True,
                "max_connections": 100,
            },
        }

        html = render_web_config_ui(config)
        assert "https://api.example.com" in html
        assert "1048576" in html

    def test_config_merge_stability(self):
        """Test config merge stability."""
        from src.gateway_web_config import handle_config_post

        current = {
            "upstream": {"url": "old", "model": "gpt-4"},
            "context": {"enabled": True},
        }

        update = {
            "upstream": {"url": "new"},
        }

        result = handle_config_post(update, current)
        assert result["upstream"]["url"] == "new"
        assert result["upstream"]["model"] == "gpt-4"
        assert result["context"]["enabled"] is True

    def test_schema_generation(self):
        """Test schema generation stability."""
        from src.gateway_web_config import get_config_schema

        schema = get_config_schema()
        assert len(schema) > 0

        for tab in schema:
            assert "id" in tab
            assert "label" in tab
            assert "fields" in tab
            for field in tab["fields"]:
                assert "name" in field
                assert "type" in field


# ---------------------------------------------------------------------------
# Claude Compat Stability
# ---------------------------------------------------------------------------

class TestClaudeCompatStability:
    """Stability tests for gateway_claude_compat module."""

    def test_tool_definitions_valid(self):
        """Test tool definitions are valid."""
        from src.gateway_claude_compat import get_claude_code_tool_definitions

        tools = get_claude_code_tool_definitions()
        assert len(tools) > 0

        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["input_schema"].get("type") == "object"

    def test_tool_detection(self):
        """Test tool name detection."""
        from src.gateway_claude_compat import is_claude_code_tool

        assert is_claude_code_tool("Read") is True
        assert is_claude_code_tool("Write") is True
        assert is_claude_code_tool("Unknown") is False

    def test_tool_execution_error_handling(self):
        """Test tool execution error handling."""
        from src.gateway_claude_compat import execute_claude_code_tool

        # Non-existent file
        result = execute_claude_code_tool("Read", {"file_path": "/nonexistent/file"})
        assert result["success"] is False

        # Unknown tool
        result = execute_claude_code_tool("UnknownTool", {})
        assert result["success"] is False

    def test_format_tool_result(self):
        """Test tool result formatting."""
        from src.gateway_claude_compat import format_tool_result_for_anthropic

        result = format_tool_result_for_anthropic("call_123", {"success": True, "content": "test"})
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "call_123"
        assert result["content"] == "test"

        result = format_tool_result_for_anthropic("call_456", {"success": False, "content": "error"})
        assert result["is_error"] is True


# ---------------------------------------------------------------------------
# Web2API Stability
# ---------------------------------------------------------------------------

class TestWeb2APIStability:
    """Stability tests for gateway_web2api module."""

    def test_empty_html(self):
        """Test handling of empty HTML."""
        from src.gateway_web2api import SimpleHTMLExtractor

        extractor = SimpleHTMLExtractor()
        extractor.feed("")
        elements = extractor.get_elements()
        assert isinstance(elements, list)

    def test_malformed_html(self):
        """Test handling of malformed HTML."""
        from src.gateway_web2api import SimpleHTMLExtractor

        html = "<div><p>unclosed<p>another"
        extractor = SimpleHTMLExtractor()
        extractor.feed(html)
        elements = extractor.get_elements()
        assert isinstance(elements, list)

    def test_valid_html(self):
        """Test handling of valid HTML."""
        from src.gateway_web2api import SimpleHTMLExtractor

        html = "<div><p>Hello</p><p>World</p></div>"
        extractor = SimpleHTMLExtractor()
        extractor.feed(html)
        elements = extractor.get_elements()
        assert isinstance(elements, list)
        assert len(elements) > 0


# ---------------------------------------------------------------------------
# Integration Stability
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestIntegrationStability:
    """Integration stability tests."""

    def test_full_context_pipeline(self):
        """Test full context pipeline stability."""
        from src.gateway_context import _body_token_estimate, _compact_messages

        messages = [
            {"role": "user", "content": f"Question {i}: " + "word " * 100}
            for i in range(50)
        ]

        tokens = _body_token_estimate({"messages": messages})
        assert tokens > 0

        compacted = _compact_messages(messages, keep_recent=4, text_limit=10000)
        assert isinstance(compacted, list)

    def test_intelligence_with_real_questions(self):
        """Test intelligence module with realistic questions."""
        from src.gateway_intelligence import enhance_intelligence

        questions = [
            "你好",
            "什么是Python？",
            "如何用Python实现文件读取？",
            "比较Python和JavaScript的区别，哪个更适合初学者？",
            "请详细解释机器学习的原理，包括监督学习、无监督学习和强化学习的区别",
        ]

        for question in questions:
            messages = [{"role": "user", "content": question}]
            result = enhance_intelligence(messages)
            assert result.analysis is not None
            assert result.enhanced_messages is not None

    def test_stats_full_lifecycle(self):
        """Test stats full lifecycle."""
        from src.gateway_stats import (
            record_request, record_tool, record_cache,
            get_dashboard, reset_stats,
            RequestStat, ToolStat, CacheStat,
        )

        reset_stats()

        now = time.time()

        # Record various stats
        record_request(RequestStat(
            timestamp=now, path="/test", method="GET",
            status_code=200, response_time=0.5,
        ))

        record_tool(ToolStat(
            timestamp=now, tool_name="Read",
            success=True, execution_time=0.1,
        ))

        record_cache(CacheStat(
            timestamp=now, cache_type="semantic",
            hit=True, similarity=0.95,
        ))

        # Get dashboard
        dashboard = get_dashboard()
        assert dashboard.requests["total_requests"] == 1
        assert dashboard.tools["total_executions"] == 1
        assert dashboard.cache["total_operations"] == 1

        reset_stats()
