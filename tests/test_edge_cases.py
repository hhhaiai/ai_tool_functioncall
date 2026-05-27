"""Comprehensive edge case and boundary condition tests.

Tests every boundary condition for commercial-grade reliability.
"""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# gateway_context.py - Edge Cases
# ============================================================================

class TestContextEdgeCases:
    """Edge cases for context module."""

    def test_token_count_none(self):
        from src.gateway_context import _approx_token_count
        assert _approx_token_count(None) == 0

    def test_token_count_empty_string(self):
        from src.gateway_context import _approx_token_count
        result = _approx_token_count("")
        assert result >= 0

    def test_token_count_whitespace_only(self):
        from src.gateway_context import _approx_token_count
        result = _approx_token_count("   \n\t  ")
        assert result >= 0

    def test_token_count_very_long_string(self):
        from src.gateway_context import _approx_token_count
        result = _approx_token_count("a" * 1000000)
        assert result > 0

    def test_token_count_cjk_characters(self):
        from src.gateway_context import _approx_token_count
        result = _approx_token_count("你好世界" * 100)
        assert result >= 100

    def test_token_count_mixed_content(self):
        from src.gateway_context import _approx_token_count
        result = _approx_token_count("Hello 你好 World 世界 !@#$%^&*()")
        assert result > 0

    def test_token_count_dict_input(self):
        from src.gateway_context import _approx_token_count
        result = _approx_token_count({"key": "value", "nested": {"data": "test"}})
        assert result > 0

    def test_token_count_list_input(self):
        from src.gateway_context import _approx_token_count
        result = _approx_token_count(["item1", "item2", {"key": "value"}])
        assert result > 0

    def test_token_count_numeric_input(self):
        from src.gateway_context import _approx_token_count
        assert _approx_token_count(42) == 1
        assert _approx_token_count(3.14) == 1
        assert _approx_token_count(True) == 1

    def test_compact_messages_empty(self):
        from src.gateway_context import _compact_messages
        result = _compact_messages([], keep_recent=4, text_limit=1000)
        assert result == []

    def test_compact_messages_none(self):
        from src.gateway_context import _compact_messages
        result = _compact_messages(None, keep_recent=4, text_limit=1000)
        assert result is None or result == []

    def test_compact_messages_single(self):
        from src.gateway_context import _compact_messages
        messages = [{"role": "user", "content": "hello"}]
        result = _compact_messages(messages, keep_recent=4, text_limit=1000)
        assert len(result) == 1

    def test_compact_messages_keep_recent_zero(self):
        from src.gateway_context import _compact_messages
        messages = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ]
        result = _compact_messages(messages, keep_recent=0, text_limit=1000)
        assert isinstance(result, list)

    def test_compact_messages_very_large(self):
        from src.gateway_context import _compact_messages
        messages = [
            {"role": "user", "content": "x" * 100000},
            {"role": "assistant", "content": "y" * 100000},
        ] * 100
        result = _compact_messages(messages, keep_recent=4, text_limit=50000)
        assert isinstance(result, list)
        assert len(result) <= len(messages)

    def test_chunk_text_empty(self):
        from src.gateway_context import _chunk_text_by_tokens
        result = _chunk_text_by_tokens("", chunk_tokens=100, max_chunks=10)
        assert result == [] or result == [""]

    def test_chunk_text_small(self):
        from src.gateway_context import _chunk_text_by_tokens
        result = _chunk_text_by_tokens("hello", chunk_tokens=1000, max_chunks=10)
        assert len(result) == 1
        assert result[0] == "hello"

    def test_chunk_text_large(self):
        from src.gateway_context import _chunk_text_by_tokens
        text = "word " * 10000
        result = _chunk_text_by_tokens(text, chunk_tokens=500, max_chunks=50)
        assert len(result) > 1

    def test_trim_text_short(self):
        from src.gateway_context import _trim_text_for_context
        result = _trim_text_for_context("hello", limit=100)
        assert result == "hello"

    def test_trim_text_long(self):
        from src.gateway_context import _trim_text_for_context
        result = _trim_text_for_context("a" * 100000, limit=100)
        assert len(result) <= 200  # Some margin for truncation marker

    def test_body_token_estimate_empty(self):
        from src.gateway_context import _body_token_estimate
        result = _body_token_estimate({})
        assert result >= 0

    def test_body_token_estimate_with_messages(self):
        from src.gateway_context import _body_token_estimate
        body = {
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ]
        }
        result = _body_token_estimate(body)
        assert result > 0

    def test_inject_system_prompt_openai(self):
        from src.gateway_context import _inject_gateway_system_prompt
        body = {"messages": [{"role": "user", "content": "hello"}]}
        result = _inject_gateway_system_prompt("/v1/chat/completions", body, reason="test")
        assert result["messages"][0]["role"] == "system"

    def test_inject_system_prompt_existing_system(self):
        from src.gateway_context import _inject_gateway_system_prompt
        body = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hello"},
            ]
        }
        result = _inject_gateway_system_prompt("/v1/chat/completions", body, reason="test")
        assert "gateway" in result["messages"][0]["content"].lower()

    def test_memory_extract_keywords_empty(self):
        from src.gateway_context import _memory_extract_keywords
        result = _memory_extract_keywords("")
        assert result == []

    def test_memory_extract_keywords_stop_words(self):
        from src.gateway_context import _memory_extract_keywords
        result = _memory_extract_keywords("is a the and or but")
        assert len(result) == 0

    def test_memory_session_key_with_metadata(self):
        from src.gateway_context import _memory_session_key
        body = {"metadata": {"session_id": "test-123"}, "messages": []}
        result = _memory_session_key(body)
        assert result == "test-123"

    def test_memory_session_key_empty_messages(self):
        from src.gateway_context import _memory_session_key
        body = {"messages": []}
        result = _memory_session_key(body)
        assert result.startswith("session_")


# ============================================================================
# gateway_intelligence.py - Edge Cases
# ============================================================================

class TestIntelligenceEdgeCases:
    """Edge cases for intelligence module."""

    def test_analyze_empty_question(self):
        from src.gateway_intelligence import _analyze_question
        result = _analyze_question("")
        assert result.complexity == "simple"
        assert result.domain == "general"

    def test_analyze_whitespace_only(self):
        from src.gateway_intelligence import _analyze_question
        result = _analyze_question("   \n\t  ")
        assert result.complexity == "simple"

    def test_analyze_single_character(self):
        from src.gateway_intelligence import _analyze_question
        result = _analyze_question("?")
        assert result.complexity == "simple"

    def test_analyze_very_long_question(self):
        from src.gateway_intelligence import _analyze_question
        long_q = "这是一个非常长的问题 " * 1000
        result = _analyze_question(long_q)
        assert result.complexity in ["simple", "moderate", "complex"]

    def test_analyze_multiple_question_marks(self):
        from src.gateway_intelligence import _analyze_question
        result = _analyze_question("什么？为什么？怎么？")
        assert result.complexity == "complex"
        assert len(result.sub_questions) > 0

    def test_detect_complexity_boundary(self):
        from src.gateway_intelligence import _detect_complexity
        # Exactly at boundary
        result = _detect_complexity("a" * 15)
        assert result in ["simple", "moderate", "complex"]

    def test_detect_domain_all_types(self):
        from src.gateway_intelligence import _detect_domain
        assert _detect_domain("python code function") == "code"
        assert _detect_domain("calculate math formula") == "math"
        assert _detect_domain("write story poem") == "creative"
        assert _detect_domain("what is who is when") == "factual"
        assert _detect_domain("hello world") == "general"

    def test_decompose_single_question(self):
        from src.gateway_intelligence import _decompose_question
        result = _decompose_question("什么是Python？")
        assert len(result) == 1

    def test_decompose_multiple_questions(self):
        from src.gateway_intelligence import _decompose_question
        result = _decompose_question("什么是Python？它有什么特点？如何学习？")
        assert len(result) == 3

    def test_decompose_no_question_mark(self):
        from src.gateway_intelligence import _decompose_question
        result = _decompose_question("没有问号的句子")
        assert len(result) <= 1

    def test_decompose_max_limit(self):
        from src.gateway_intelligence import _decompose_question
        text = "？".join([f"问题{i}" for i in range(20)])
        result = _decompose_question(text)
        assert len(result) <= 5

    def test_assess_completeness_empty(self):
        from src.gateway_intelligence import _assess_completeness
        assert _assess_completeness("question", "") == 0.0

    def test_assess_completeness_uncertain(self):
        from src.gateway_intelligence import _assess_completeness
        result = _assess_completeness("question", "我不知道")
        assert result < 0.5

    def test_assess_completeness_good(self):
        from src.gateway_intelligence import _assess_completeness
        result = _assess_completeness("什么是Python", "Python是一种编程语言，广泛用于开发")
        assert result >= 0.4

    def test_assess_relevance_empty(self):
        from src.gateway_intelligence import _assess_relevance
        assert _assess_relevance("question", "") == 0.0

    def test_assess_relevance_relevant(self):
        from src.gateway_intelligence import _assess_relevance
        result = _assess_relevance("Python programming", "Python is a programming language")
        assert result >= 0.4

    def test_assess_relevance_irrelevant(self):
        from src.gateway_intelligence import _assess_relevance
        result = _assess_relevance("Python programming", "今天天气很好")
        assert result < 0.6

    def test_assess_clarity_empty(self):
        from src.gateway_intelligence import _assess_clarity
        assert _assess_clarity("") == 0.0

    def test_assess_clarity_structured(self):
        from src.gateway_intelligence import _assess_clarity
        result = _assess_clarity("# Title\n\nParagraph\n\n- Item 1\n- Item 2")
        assert result > 0.6

    def test_assess_accuracy_confident(self):
        from src.gateway_intelligence import _assess_accuracy
        result = _assess_accuracy("Python是编程语言，广泛用于开发")
        assert result >= 0.6

    def test_assess_accuracy_uncertain(self):
        from src.gateway_intelligence import _assess_accuracy
        result = _assess_accuracy("可能也许大概我不确定")
        assert result < 0.7

    def test_enhance_intelligence_disabled(self):
        from src.gateway_intelligence import enhance_intelligence, IntelligenceConfig
        config = IntelligenceConfig(enabled=False)
        result = enhance_intelligence([{"role": "user", "content": "hello"}], config)
        assert result.system_prompt is None

    def test_refine_answer_good(self):
        from src.gateway_intelligence import refine_answer
        answer = "Python是一种高级编程语言，广泛用于开发。它简洁易读。"
        _, assessment = refine_answer("什么是Python？", answer)
        assert assessment.overall >= 0.4

    def test_refine_answer_bad(self):
        from src.gateway_intelligence import refine_answer, IntelligenceConfig
        config = IntelligenceConfig(quality_threshold=0.9)
        _, assessment = refine_answer("详细解释Python", "不知道", config)
        assert assessment.needs_refinement is True


# ============================================================================
# gateway_cache.py - Edge Cases
# ============================================================================

class TestCacheEdgeCases:
    """Edge cases for cache module."""

    def test_cache_get_empty(self):
        from src.gateway_cache import ToolResultCache
        cache = ToolResultCache(ttl_seconds=60)
        assert cache.get("Read", {}) is None

    def test_cache_put_get(self):
        from src.gateway_cache import ToolResultCache
        cache = ToolResultCache(ttl_seconds=60)
        cache.put("Read", {"path": "test.py"}, "content")
        assert cache.get("Read", {"path": "test.py"}) == "content"

    def test_cache_different_args(self):
        from src.gateway_cache import ToolResultCache
        cache = ToolResultCache(ttl_seconds=60)
        cache.put("Read", {"path": "a.py"}, "content_a")
        assert cache.get("Read", {"path": "b.py"}) is None

    def test_cache_different_tools(self):
        from src.gateway_cache import ToolResultCache
        cache = ToolResultCache(ttl_seconds=60)
        cache.put("Read", {"path": "test.py"}, "content")
        assert cache.get("Write", {"path": "test.py"}) is None

    def test_cache_expiration(self):
        from src.gateway_cache import ToolResultCache
        cache = ToolResultCache(ttl_seconds=1)
        cache.put("Read", {"path": "test.py"}, "content")
        time.sleep(1.1)
        assert cache.get("Read", {"path": "test.py"}) is None

    def test_cache_invalidate(self):
        from src.gateway_cache import ToolResultCache
        cache = ToolResultCache(ttl_seconds=60)
        cache.put("Read", {"path": "/test.py"}, "content")
        cache.invalidate(path="/test.py")
        assert cache.get("Read", {"path": "/test.py"}) is None

    def test_cache_invalidate_nonexistent(self):
        from src.gateway_cache import ToolResultCache
        cache = ToolResultCache(ttl_seconds=60)
        cache.invalidate(path="/nonexistent")  # Should not raise

    def test_cache_concurrent_access(self):
        from src.gateway_cache import ToolResultCache
        cache = ToolResultCache(ttl_seconds=60)
        errors = []

        def worker(tid):
            try:
                for i in range(50):
                    cache.put("Read", {"path": f"f_{tid}_{i}.py"}, f"c_{i}")
                    assert cache.get("Read", {"path": f"f_{tid}_{i}.py"}) == f"c_{i}"
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(errors) == 0

    def test_cosine_similarity_identical(self):
        from src.gateway_cache import cosine_similarity
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) > 0.99

    def test_cosine_similarity_orthogonal(self):
        from src.gateway_cache import cosine_similarity
        assert cosine_similarity([1, 0], [0, 1]) == 0.0

    def test_cosine_similarity_opposite(self):
        from src.gateway_cache import cosine_similarity
        result = cosine_similarity([1, 0], [-1, 0])
        assert result < 0

    def test_cosine_similarity_empty(self):
        from src.gateway_cache import cosine_similarity
        result = cosine_similarity([], [])
        assert result == 0.0


# ============================================================================
# gateway_concurrency.py - Edge Cases
# ============================================================================

class TestConcurrencyEdgeCases:
    """Edge cases for concurrency module."""

    def test_load_balancer_empty_upstreams(self):
        from src.gateway_concurrency import LoadBalancer, ConcurrencyConfig
        config = ConcurrencyConfig()
        balancer = LoadBalancer([], config)
        assert balancer.get_next() is None

    def test_load_balancer_single_upstream(self):
        from src.gateway_concurrency import LoadBalancer, ConcurrencyConfig
        config = ConcurrencyConfig()
        balancer = LoadBalancer([{"url": "http://only.com"}], config)
        for _ in range(10):
            assert balancer.get_next()["url"] == "http://only.com"

    def test_load_balancer_round_robin_distribution(self):
        from src.gateway_concurrency import LoadBalancer, ConcurrencyConfig
        config = ConcurrencyConfig(load_balance_strategy="round_robin")
        upstreams = [{"url": f"http://up{i}.com"} for i in range(3)]
        balancer = LoadBalancer(upstreams, config)

        counts = {}
        for _ in range(30):
            url = balancer.get_next()["url"]
            counts[url] = counts.get(url, 0) + 1

        # Should be evenly distributed
        for count in counts.values():
            assert count == 10

    def test_load_balancer_health_tracking(self):
        from src.gateway_concurrency import LoadBalancer, ConcurrencyConfig
        config = ConcurrencyConfig()
        balancer = LoadBalancer([{"url": "http://up.com"}], config)

        # Report failures
        for _ in range(3):
            balancer.report_failure("http://up.com")

        status = balancer.get_health_status()
        assert status["http://up.com"]["is_healthy"] is False

    def test_load_balancer_recovery(self):
        from src.gateway_concurrency import LoadBalancer, ConcurrencyConfig
        config = ConcurrencyConfig()
        balancer = LoadBalancer([{"url": "http://up.com"}], config)

        # Fail then recover
        for _ in range(3):
            balancer.report_failure("http://up.com")
        balancer.report_success("http://up.com", 0.1)

        status = balancer.get_health_status()
        assert status["http://up.com"]["is_healthy"] is True

    def test_connection_pool_limit(self):
        from src.gateway_concurrency import ConnectionPool, ConcurrencyConfig
        config = ConcurrencyConfig(max_connections_per_host=2)
        pool = ConnectionPool(config)

        conns = [pool.get_connection("http://example.com") for _ in range(2)]
        with pytest.raises(ConnectionError):
            pool.get_connection("http://example.com")

    def test_connection_pool_return(self):
        from src.gateway_concurrency import ConnectionPool, ConcurrencyConfig
        config = ConcurrencyConfig(max_connections_per_host=1)
        pool = ConnectionPool(config)

        conn = pool.get_connection("http://example.com")
        pool.return_connection("http://example.com", conn)
        conn2 = pool.get_connection("http://example.com")
        assert conn2 is not None

    def test_upstream_manager_empty(self):
        from src.gateway_concurrency import MultiUpstreamManager
        manager = MultiUpstreamManager([])
        assert manager.get_primary_upstream() is None

    def test_upstream_manager_add_remove(self):
        from src.gateway_concurrency import MultiUpstreamManager
        manager = MultiUpstreamManager([{"url": "http://a.com"}])
        manager.add_upstream({"url": "http://b.com"})
        assert len(manager.get_status()["upstreams"]) == 2
        manager.remove_upstream("http://a.com")
        assert len(manager.get_status()["upstreams"]) == 1

    def test_upstream_manager_switch_primary(self):
        from src.gateway_concurrency import MultiUpstreamManager
        upstreams = [{"url": f"http://up{i}.com"} for i in range(3)]
        manager = MultiUpstreamManager(upstreams)

        manager.switch_primary(2)
        assert manager.get_primary_upstream()["url"] == "http://up2.com"

    def test_upstream_manager_invalid_switch(self):
        from src.gateway_concurrency import MultiUpstreamManager
        manager = MultiUpstreamManager([{"url": "http://a.com"}])
        manager.switch_primary(99)  # Should not raise
        assert manager.get_primary_upstream()["url"] == "http://a.com"


# ============================================================================
# gateway_stats.py - Edge Cases
# ============================================================================

class TestStatsEdgeCases:
    """Edge cases for stats module."""

    @pytest.fixture(autouse=True)
    def clean_db(self):
        from src.gateway_stats import reset_stats
        reset_stats()
        yield
        reset_stats()

    def test_empty_stats(self):
        from src.gateway_stats import get_request_stats, get_tool_stats, get_cache_stats
        assert get_request_stats()["total_requests"] == 0
        assert get_tool_stats()["total_executions"] == 0
        assert get_cache_stats()["total_operations"] == 0

    def test_record_request_minimal(self):
        from src.gateway_stats import record_request, get_request_stats, RequestStat
        record_request(RequestStat(timestamp=time.time(), path="/test", method="GET"))
        assert get_request_stats()["total_requests"] == 1

    def test_record_request_full(self):
        from src.gateway_stats import record_request, get_request_stats, RequestStat
        record_request(RequestStat(
            timestamp=time.time(), path="/test", method="POST",
            status_code=200, response_time=0.5,
            input_tokens=100, output_tokens=50,
            cache_hit=True,
        ))
        stats = get_request_stats()
        assert stats["total_requests"] == 1
        assert stats["cache_hit_rate"] == 1.0

    def test_record_tool_success(self):
        from src.gateway_stats import record_tool, get_tool_stats, ToolStat
        record_tool(ToolStat(timestamp=time.time(), tool_name="Read", success=True))
        assert get_tool_stats()["success_rate"] == 1.0

    def test_record_tool_failure(self):
        from src.gateway_stats import record_tool, get_tool_stats, ToolStat
        record_tool(ToolStat(
            timestamp=time.time(), tool_name="Bash", success=False,
            error_type="timeout",
        ))
        stats = get_tool_stats()
        assert stats["success_rate"] == 0.0
        assert "timeout" in stats["tools"]["Bash"]["errors"]

    def test_record_cache_hit(self):
        from src.gateway_stats import record_cache, get_cache_stats, CacheStat
        record_cache(CacheStat(timestamp=time.time(), cache_type="semantic", hit=True))
        assert get_cache_stats()["hit_rate"] == 1.0

    def test_record_cache_miss(self):
        from src.gateway_stats import record_cache, get_cache_stats, CacheStat
        record_cache(CacheStat(timestamp=time.time(), cache_type="semantic", hit=False))
        assert get_cache_stats()["hit_rate"] == 0.0

    def test_quality_stats(self):
        from src.gateway_stats import record_quality, get_quality_stats, QualityStat
        record_quality(QualityStat(
            timestamp=time.time(), completeness=0.8, relevance=0.9,
            clarity=0.7, accuracy=0.85, overall=0.8, needs_refinement=False,
        ))
        stats = get_quality_stats()
        assert stats["total_assessments"] == 1

    def test_dashboard_empty(self):
        from src.gateway_stats import get_dashboard
        dashboard = get_dashboard()
        assert dashboard.requests["total_requests"] == 0

    def test_dashboard_with_data(self):
        from src.gateway_stats import record_request, get_dashboard, RequestStat, reset_stats
        record_request(RequestStat(timestamp=time.time(), path="/test", method="GET", status_code=200))
        dashboard = get_dashboard()
        assert dashboard.requests["total_requests"] == 1

    def test_cleanup_old_stats(self):
        from src.gateway_stats import record_request, get_request_stats, cleanup_old_stats, RequestStat
        record_request(RequestStat(timestamp=time.time() - 40*86400, path="/old", method="GET"))
        record_request(RequestStat(timestamp=time.time(), path="/new", method="GET"))
        cleanup_old_stats(retention_days=30)
        assert get_request_stats()["total_requests"] == 1

    def test_export_csv(self):
        from src.gateway_stats import record_request, export_stats_csv, RequestStat
        record_request(RequestStat(timestamp=time.time(), path="/test", method="GET", status_code=200))
        csv = export_stats_csv("requests")
        assert "timestamp" in csv
        assert "/test" in csv

    def test_top_paths(self):
        from src.gateway_stats import record_request, get_top_paths, RequestStat
        for _ in range(5):
            record_request(RequestStat(timestamp=time.time(), path="/v1/chat", method="POST"))
        record_request(RequestStat(timestamp=time.time(), path="/v1/messages", method="POST"))
        top = get_top_paths(limit=10)
        assert top[0]["path"] == "/v1/chat"
        assert top[0]["count"] == 5


# ============================================================================
# gateway_web_config.py - Edge Cases
# ============================================================================

class TestWebConfigEdgeCases:
    """Edge cases for web config module."""

    def test_render_empty_config(self):
        from src.gateway_web_config import render_web_config_ui
        html = render_web_config_ui({})
        assert "<!DOCTYPE html>" in html

    def test_render_none_config(self):
        from src.gateway_web_config import render_web_config_ui
        html = render_web_config_ui(None)
        assert "<!DOCTYPE html>" in html

    def test_render_complex_config(self):
        from src.gateway_web_config import render_web_config_ui
        config = {
            "upstream": {"url": "https://api.test.com", "api_key": "sk-test"},
            "context": {"enabled": True, "max_input_tokens": 1048576},
            "intelligence": {"enabled": True},
        }
        html = render_web_config_ui(config)
        assert "https://api.test.com" in html

    def test_get_nested_value_simple(self):
        from src.gateway_web_config import _get_nested_value
        assert _get_nested_value({"a": 1}, "a") == 1

    def test_get_nested_value_deep(self):
        from src.gateway_web_config import _get_nested_value
        assert _get_nested_value({"a": {"b": {"c": 3}}}, "a.b.c") == 3

    def test_get_nested_value_missing(self):
        from src.gateway_web_config import _get_nested_value
        assert _get_nested_value({"a": 1}, "b") is None

    def test_set_nested_value_simple(self):
        from src.gateway_web_config import _set_nested_value
        result = _set_nested_value({}, "a", 1)
        assert result["a"] == 1

    def test_set_nested_value_deep(self):
        from src.gateway_web_config import _set_nested_value
        result = _set_nested_value({}, "a.b.c", 3)
        assert result["a"]["b"]["c"] == 3

    def test_deep_merge(self):
        from src.gateway_web_config import _deep_merge
        base = {"a": 1, "b": {"x": 1}}
        update = {"b": {"y": 2}, "c": 3}
        result = _deep_merge(base, update)
        assert result["a"] == 1
        assert result["b"]["x"] == 1
        assert result["b"]["y"] == 2
        assert result["c"] == 3

    def test_config_schema(self):
        from src.gateway_web_config import get_config_schema
        schema = get_config_schema()
        assert len(schema) > 0
        for tab in schema:
            assert "id" in tab
            assert "fields" in tab

    def test_handle_config_post(self):
        from src.gateway_web_config import handle_config_post
        current = {"a": 1, "b": 2}
        update = {"b": 3, "c": 4}
        result = handle_config_post(update, current)
        assert result["a"] == 1
        assert result["b"] == 3
        assert result["c"] == 4


# ============================================================================
# gateway_claude_compat.py - Edge Cases
# ============================================================================

class TestClaudeCompatEdgeCases:
    """Edge cases for Claude compatibility module."""

    def test_tool_definitions_valid(self):
        from src.gateway_claude_compat import get_claude_code_tool_definitions
        tools = get_claude_code_tool_definitions()
        assert len(tools) > 0
        for tool in tools:
            assert "name" in tool
            assert "input_schema" in tool

    def test_is_claude_code_tool_known(self):
        from src.gateway_claude_compat import is_claude_code_tool
        assert is_claude_code_tool("Read") is True
        assert is_claude_code_tool("Write") is True
        assert is_claude_code_tool("Bash") is True

    def test_is_claude_code_tool_unknown(self):
        from src.gateway_claude_compat import is_claude_code_tool
        assert is_claude_code_tool("Unknown") is False
        assert is_claude_code_tool("") is False

    def test_execute_read_nonexistent(self):
        from src.gateway_claude_compat import execute_claude_code_tool
        result = execute_claude_code_tool("Read", {"file_path": "/nonexistent"})
        assert result["success"] is False

    def test_execute_read_no_path(self):
        from src.gateway_claude_compat import execute_claude_code_tool
        result = execute_claude_code_tool("Read", {})
        assert result["success"] is False

    def test_execute_unknown_tool(self):
        from src.gateway_claude_compat import execute_claude_code_tool
        result = execute_claude_code_tool("UnknownTool", {})
        assert result["success"] is False

    def test_format_result_success(self):
        from src.gateway_claude_compat import format_tool_result_for_anthropic
        result = format_tool_result_for_anthropic("id1", {"success": True, "content": "ok"})
        assert result["type"] == "tool_result"
        assert result["is_error"] is False

    def test_format_result_error(self):
        from src.gateway_claude_compat import format_tool_result_for_anthropic
        result = format_tool_result_for_anthropic("id1", {"success": False, "content": "err"})
        assert result["is_error"] is True

    def test_extract_tool_uses(self):
        from src.gateway_claude_compat import extract_tool_uses_from_response
        response = {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "c1", "name": "Read", "input": {}},
        ]}
        uses = extract_tool_uses_from_response(response)
        assert len(uses) == 1

    def test_extract_no_tool_uses(self):
        from src.gateway_claude_compat import extract_tool_uses_from_response
        response = {"content": [{"type": "text", "text": "hello"}]}
        uses = extract_tool_uses_from_response(response)
        assert len(uses) == 0

    def test_build_tool_result_message(self):
        from src.gateway_claude_compat import build_tool_result_message, format_tool_result_for_anthropic
        results = [format_tool_result_for_anthropic("c1", {"success": True, "content": "ok"})]
        msg = build_tool_result_message(results)
        assert msg["role"] == "user"
        assert len(msg["content"]) == 1


# ============================================================================
# gateway_tool_runtime.py - Edge Cases
# ============================================================================

class TestToolRuntimeEdgeCases:
    """Edge cases for tool runtime module."""

    def test_extract_empty_response(self):
        from src.gateway_tool_runtime import _extract_tool_calls
        assert _extract_tool_calls("/v1/chat/completions", {}) == []

    def test_extract_no_choices(self):
        from src.gateway_tool_runtime import _extract_tool_calls
        assert _extract_tool_calls("/v1/chat/completions", {"choices": []}) == []

    def test_extract_no_tool_calls(self):
        from src.gateway_tool_runtime import _extract_tool_calls
        response = {"choices": [{"message": {"content": "hello"}}]}
        assert _extract_tool_calls("/v1/chat/completions", response) == []

    def test_extract_multiple_tools(self):
        from src.gateway_tool_runtime import _extract_tool_calls
        response = {
            "choices": [{
                "message": {
                    "tool_calls": [
                        {"id": f"c{i}", "type": "function", "function": {"name": f"t{i}", "arguments": "{}"}}
                        for i in range(5)
                    ]
                }
            }]
        }
        calls = _extract_tool_calls("/v1/chat/completions", response)
        assert len(calls) == 5

    def test_normalize_tool_call(self):
        from src.gateway_tool_runtime import _normalize_tool_call, ToolCall
        tc = ToolCall(call_id="c1", name="Read", arguments={"path": "test"}, raw={})
        result = _normalize_tool_call(tc)
        assert result is not None
        assert result.name == "Read"

    def test_response_text_empty(self):
        from src.gateway_tool_runtime import _response_text
        result = _response_text("/v1/chat/completions", {})
        assert result == "" or result is not None

    def test_response_text_with_content(self):
        from src.gateway_tool_runtime import _response_text
        response = {"choices": [{"message": {"content": "hello"}}]}
        assert _response_text("/v1/chat/completions", response) == "hello"

    def test_looks_like_context_rejection(self):
        from src.gateway_tool_runtime import _looks_like_context_rejection
        assert _looks_like_context_rejection("text too long") is True
        assert _looks_like_context_rejection("send it in parts") is True
        assert _looks_like_context_rejection("内容过长") is True
        assert _looks_like_context_rejection("normal message") is False
        assert _looks_like_context_rejection("") is False


# ============================================================================
# gateway_web2api.py - Edge Cases
# ============================================================================

class TestWeb2APIEdgeCases:
    """Edge cases for web2api module."""

    def test_empty_html(self):
        from src.gateway_web2api import SimpleHTMLExtractor
        extractor = SimpleHTMLExtractor()
        extractor.feed("")
        assert isinstance(extractor.get_elements(), list)

    def test_malformed_html(self):
        from src.gateway_web2api import SimpleHTMLExtractor
        extractor = SimpleHTMLExtractor()
        extractor.feed("<div><p>unclosed")
        assert isinstance(extractor.get_elements(), list)

    def test_valid_html(self):
        from src.gateway_web2api import SimpleHTMLExtractor
        extractor = SimpleHTMLExtractor()
        extractor.feed("<html><body><p>Hello</p></body></html>")
        elements = extractor.get_elements()
        assert len(elements) > 0

    def test_extract_title(self):
        from src.gateway_web2api import _extract_title
        html = "<html><head><title>Test</title></head></html>"
        assert _extract_title(html) == "Test"

    def test_extract_title_missing(self):
        from src.gateway_web2api import _extract_title
        assert _extract_title("<html></html>") is None or _extract_title("<html></html>") == ""

    def test_regex_extract(self):
        from src.gateway_web2api import _regex_extract
        result = _regex_extract("abc 123 def 456", r'\d+')
        assert "123" in result
        assert "456" in result


# ============================================================================
# Integration Edge Cases
# ============================================================================

@pytest.mark.integration
class TestIntegrationEdgeCases:
    """Integration edge case tests."""

    def test_context_to_intelligence_pipeline(self):
        """Test full pipeline from context to intelligence."""
        from src.gateway_context import _body_token_estimate, _compact_messages
        from src.gateway_intelligence import enhance_intelligence

        messages = [
            {"role": "user", "content": f"Question {i}: " + "word " * 50}
            for i in range(20)
        ]

        # Context processing
        tokens = _body_token_estimate({"messages": messages})
        compacted = _compact_messages(messages, keep_recent=4, text_limit=10000)

        # Intelligence enhancement
        result = enhance_intelligence(compacted)
        assert result.analysis is not None

    def test_stats_with_cache(self):
        """Test stats recording with cache operations."""
        from src.gateway_stats import record_cache, get_cache_stats, reset_stats, CacheStat
        from src.gateway_cache import ToolResultCache

        reset_stats()
        cache = ToolResultCache(ttl_seconds=60)

        # Simulate cache operations with stats
        for i in range(10):
            cache.put("Read", {"path": f"f{i}.py"}, f"c{i}")
            record_cache(CacheStat(
                timestamp=time.time(), cache_type="tool_result", hit=False,
            ))

        for i in range(10):
            cache.get("Read", {"path": f"f{i}.py"})
            record_cache(CacheStat(
                timestamp=time.time(), cache_type="tool_result", hit=True,
            ))

        stats = get_cache_stats()
        assert stats["total_operations"] == 20
        assert stats["hit_rate"] == 0.5

        reset_stats()

    def test_concurrent_all_modules(self):
        """Test all modules under concurrent load."""
        from src.gateway_context import _approx_token_count
        from src.gateway_intelligence import _analyze_question
        from src.gateway_cache import ToolResultCache
        from src.gateway_stats import record_request, get_request_stats, reset_stats, RequestStat

        reset_stats()
        cache = ToolResultCache(ttl_seconds=60, max_entries=5000)
        errors = []

        def worker(tid):
            try:
                for i in range(20):
                    _approx_token_count(f"text {i} thread {tid}")
                    _analyze_question(f"question {i} from thread {tid}")
                    cache.put("Read", {"path": f"f_{tid}_{i}.py"}, f"content_{tid}_{i}")
                    record_request(RequestStat(
                        timestamp=time.time(), path=f"/t/{tid}", method="GET", status_code=200,
                    ))
            except Exception as e:
                errors.append(repr(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent errors: {errors}"
        assert get_request_stats()["total_requests"] == 100

        reset_stats()
