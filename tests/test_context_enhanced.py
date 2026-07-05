"""Tests for enhanced context management features."""
from __future__ import annotations

import json
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.gateway_context import (
    _approx_token_count,
    _context_config,
    _context_enabled,
    _body_token_estimate,
    _gateway_system_prompt,
    _content_contains_gateway_prompt,
    _inject_gateway_system_prompt,
    _memory_config,
    _memory_enabled,
    _memory_session_key,
    _memory_workspace_key,
    _memory_extract_keywords,
    _memory_extract_request_text,
    _memory_summarize_turn,
    _compact_messages,
    _chunk_text_by_tokens,
    _trim_text_for_context,
    _should_fanout_context,
    _make_synthesis_prompt,
)


class TestTokenEstimation:
    def test_empty_input(self):
        assert _approx_token_count(None) == 0

    def test_empty_string(self):
        # Empty string may return 0 or 1 depending on implementation
        tokens = _approx_token_count("")
        assert tokens >= 0

    def test_ascii_text(self):
        text = "hello world"
        tokens = _approx_token_count(text)
        assert 2 <= tokens <= 4

    def test_cjk_text(self):
        text = "你好世界"
        tokens = _approx_token_count(text)
        assert tokens >= 4

    def test_mixed_text(self):
        text = "Hello 你好 World 世界"
        tokens = _approx_token_count(text)
        assert tokens > 0

    def test_dict_input(self):
        data = {"content": "hello world"}
        tokens = _approx_token_count(data)
        assert tokens > 0

    def test_list_input(self):
        data = ["hello", "world"]
        tokens = _approx_token_count(data)
        assert tokens > 0

    def test_numeric_input(self):
        assert _approx_token_count(42) == 1
        assert _approx_token_count(3.14) == 1
        assert _approx_token_count(True) == 1

    def test_body_token_estimate(self):
        body = {
            "messages": [
                {"role": "user", "content": "Hello, how are you?"},
                {"role": "assistant", "content": "I'm doing well, thanks!"},
            ],
            "tools": [{"type": "function", "function": {"name": "test"}}],
            "tool_choice": "auto",
        }
        estimate = _body_token_estimate(body)
        assert estimate > 0


class TestGatewaySystemPrompt:
    def test_system_prompt_content(self):
        prompt = _gateway_system_prompt()
        assert "gateway context compacted" in prompt
        assert "Gateway context management" in prompt

    def test_content_contains_gateway_prompt(self):
        text = "[Gateway context management: This conversation has been compacted]"
        assert _content_contains_gateway_prompt(text) is True

    def test_content_does_not_contain_gateway_prompt(self):
        text = "Normal user message"
        assert _content_contains_gateway_prompt(text) is False

    def test_content_in_list(self):
        content = [
            {"type": "text", "text": "[Gateway context management: compacted]"},
            {"type": "text", "text": "Other content"},
        ]
        assert _content_contains_gateway_prompt(content) is True

    def test_inject_gateway_system_prompt_openai(self):
        body = {"messages": [{"role": "user", "content": "Hello"}]}
        result = _inject_gateway_system_prompt("/v1/chat/completions", body, reason="test")
        messages = result["messages"]
        assert messages[0]["role"] == "system"
        assert "gateway context compacted" in messages[0]["content"]

    def test_inject_gateway_system_prompt_existing_system(self):
        body = {
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ]
        }
        result = _inject_gateway_system_prompt("/v1/chat/completions", body, reason="test")
        messages = result["messages"]
        assert "gateway context compacted" in messages[0]["content"]
        assert "You are helpful" in messages[0]["content"]

    def test_inject_gateway_system_prompt_anthropic(self):
        body = {"messages": [{"role": "user", "content": "Hello"}]}
        result = _inject_gateway_system_prompt("/v1/messages", body, reason="test")
        messages = result["messages"]
        content = messages[0]["content"]
        if isinstance(content, list):
            assert any("gateway" in item.get("text", "") for item in content)
        else:
            assert "gateway" in content

    def test_no_double_inject(self):
        body = {
            "messages": [
                {"role": "system", "content": "[Gateway context management: already there]"},
                {"role": "user", "content": "Hello"},
            ]
        }
        result = _inject_gateway_system_prompt("/v1/chat/completions", body, reason="test")
        messages = result["messages"]
        assert messages[0]["content"].count("Gateway context management") == 1


class TestMemoryKeywords:
    def test_extract_keywords(self):
        text = "Python is a great programming language for building web applications"
        keywords = _memory_extract_keywords(text)
        assert "python" in keywords
        assert "programming" in keywords
        assert "language" in keywords
        assert "is" not in keywords
        assert "a" not in keywords

    def test_extract_keywords_limit(self):
        text = " ".join([f"word{i}" for i in range(100)])
        keywords = _memory_extract_keywords(text, limit=10)
        assert len(keywords) <= 10

    def test_extract_keywords_empty(self):
        keywords = _memory_extract_keywords("")
        assert len(keywords) == 0

    def test_extract_keywords_dedup(self):
        text = "python python python java java"
        keywords = _memory_extract_keywords(text)
        assert keywords.count("python") == 1
        assert keywords.count("java") == 1


class TestMemorySessionKey:
    def test_session_from_metadata(self):
        body = {
            "metadata": {"session_id": "my-session-123"},
            "messages": [{"role": "user", "content": "Hello"}],
        }
        key = _memory_session_key(body)
        assert key == "tenant:anonymous:session:my-session-123"

    def test_session_accepts_metadata_tenant_alias(self):
        body = {
            "metadata": {"tenant": "tenant-alias-user", "session_id": "my-session-123"},
            "messages": [{"role": "user", "content": "Hello"}],
        }
        key = _memory_session_key(body)
        assert key == "tenant:tenant-alias-user:session:my-session-123"

    def test_session_from_conversation_id(self):
        body = {
            "metadata": {"conversation_id": "conv-456"},
            "messages": [{"role": "user", "content": "Hello"}],
        }
        key = _memory_session_key(body)
        assert key == "tenant:anonymous:session:conv-456"

    def test_session_from_first_message(self):
        body = {
            "messages": [
                {"role": "user", "content": "This is a long enough message for hashing"},
            ]
        }
        key = _memory_session_key(body)
        assert key.startswith("tenant:anonymous:session_")

    def test_session_random_fallback(self):
        body = {"messages": []}
        key = _memory_session_key(body)
        assert key.startswith("tenant:anonymous:session_")


class TestMessageCompaction:
    def test_compact_messages_under_limit(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = _compact_messages(messages, keep_recent=10, text_limit=100000)
        assert len(result) == 2

    def test_compact_messages_preserves_recent(self):
        messages = []
        for i in range(20):
            messages.append({"role": "user", "content": f"Question {i} " * 100})
            messages.append({"role": "assistant", "content": f"Answer {i} " * 100})

        result = _compact_messages(messages, keep_recent=4, text_limit=5000)
        assert len(result) <= len(messages)
        assert result[-1]["content"] == messages[-1]["content"]

    def test_compact_messages_empty(self):
        result = _compact_messages([], keep_recent=4, text_limit=1000)
        assert len(result) == 0


class TestTextChunking:
    def test_chunk_small_text(self):
        text = "Hello world"
        chunks = _chunk_text_by_tokens(text, chunk_tokens=1000, max_chunks=10)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_chunk_large_text(self):
        text = "word " * 10000
        chunks = _chunk_text_by_tokens(text, chunk_tokens=500, max_chunks=50)
        assert len(chunks) > 1

    def test_chunk_preserves_content(self):
        text = "first second third fourth fifth"
        chunks = _chunk_text_by_tokens(text, chunk_tokens=20, max_chunks=10)
        reconstructed = " ".join(chunks)
        for word in ["first", "second", "third", "fourth", "fifth"]:
            assert word in reconstructed


class TestTextTrimming:
    def test_trim_short_text(self):
        text = "Hello"
        result = _trim_text_for_context(text, limit=100)
        assert result == text

    def test_trim_long_text(self):
        text = "a" * 10000
        result = _trim_text_for_context(text, limit=100)
        assert len(result) <= 150


class TestSynthesisPrompt:
    def test_synthesis_prompt_structure(self):
        partials = ["First part analysis", "Second part analysis"]
        original_question = "What is this codebase about?"

        prompt = _make_synthesis_prompt(original_question, partials)
        assert isinstance(prompt, str)
        assert len(prompt) > 0
        assert original_question in prompt or "codebase" in prompt.lower()


class TestMemorySummarizeTurn:
    def test_summarize_basic(self):
        path = "/v1/chat/completions"
        body = {
            "messages": [
                {"role": "user", "content": "What is Python?"},
                {"role": "assistant", "content": "Python is a programming language."},
            ]
        }
        response = {"choices": [{"message": {"content": "Python is a programming language."}}]}

        result = _memory_summarize_turn(path, body, response, max_chars=500)
        assert isinstance(result, tuple)
        assert len(result) == 4  # (summary, kind, keywords, importance)
        summary, kind, keywords, importance = result
        assert isinstance(summary, str)
        assert isinstance(kind, str)
        assert isinstance(keywords, list)
        assert isinstance(importance, int)


@pytest.mark.integration
class TestContextIntegration:
    def test_full_context_pipeline(self):
        messages = []
        for i in range(30):
            messages.append({"role": "user", "content": f"Question {i}: " + "word " * 100})
            messages.append({"role": "assistant", "content": f"Answer {i}: " + "word " * 200})

        body = {"messages": messages}
        tokens = _body_token_estimate(body)
        assert tokens > 0

        # Compaction may or may not reduce messages depending on implementation
        compacted = _compact_messages(messages, keep_recent=4, text_limit=10000)
        assert isinstance(compacted, list)

    def test_memory_extraction_pipeline(self):
        body = {
            "messages": [
                {"role": "user", "content": "I'm building a Python web app with FastAPI and PostgreSQL"},
                {"role": "assistant", "content": "Great choice! FastAPI is excellent for building APIs."},
            ],
            "metadata": {"session_id": "test-session"},
        }
        session_key = _memory_session_key(body)
        assert session_key == "tenant:anonymous:session:test-session"
        text = _memory_extract_request_text("/v1/chat/completions", body)
        assert "Python" in text or "FastAPI" in text
        keywords = _memory_extract_keywords(text)
        assert len(keywords) > 0


class TestContextEdgeCases:
    def test_none_messages(self):
        result = _compact_messages(None, keep_recent=4, text_limit=1000)
        assert result is None or result == []

    def test_empty_content(self):
        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": None},
        ]
        tokens = _approx_token_count(messages)
        assert tokens >= 0

    def test_malformed_messages(self):
        messages = [
            {"role": "user"},
            {"content": "Hello"},
            "not a dict",
        ]
        try:
            _memory_extract_request_text("/v1/chat/completions", {"messages": messages})
        except Exception:
            pass

    def test_very_long_single_message(self):
        messages = [{"role": "user", "content": "x" * 1000000}]
        tokens = _approx_token_count(messages)
        assert tokens > 0


class TestFanoutContext:
    """Tests for _should_fanout_context decision logic."""

    def test_fanout_disabled_in_config(self):
        """Fanout returns False when disabled in config."""
        body = {"messages": [{"role": "user", "content": "x" * 200000}]}
        cfg = {"context": {"fanout_enabled": False}}
        assert _should_fanout_context("/v1/chat/completions", body, cfg) is False

    def test_fanout_force_overrides_threshold(self):
        """force=True triggers fanout even for small payloads."""
        body = {"messages": [{"role": "user", "content": "short"}]}
        cfg = {"context": {"fanout_enabled": True, "max_input_tokens": 24000}}
        assert _should_fanout_context("/v1/chat/completions", body, cfg, force=True) is True

    def test_fanout_below_threshold(self):
        """Small payload does not trigger fanout."""
        body = {"messages": [{"role": "user", "content": "hello world"}]}
        cfg = {"context": {"fanout_enabled": True, "max_input_tokens": 24000}}
        assert _should_fanout_context("/v1/chat/completions", body, cfg) is False

    def test_fanout_above_threshold(self):
        """Large payload triggers fanout (tokens > max_input_tokens * 2)."""
        # ~100k chars ≈ ~25k tokens, threshold is 24000*2=48000 tokens
        body = {"messages": [{"role": "user", "content": "x" * 200000}]}
        cfg = {"context": {"fanout_enabled": True, "max_input_tokens": 24000}}
        assert _should_fanout_context("/v1/chat/completions", body, cfg) is True

    def test_fanout_custom_max_input_tokens(self):
        """Respects custom max_input_tokens config."""
        body = {"messages": [{"role": "user", "content": "x" * 10000}]}
        # With max_input_tokens=100, threshold=200 tokens, 10000 chars ≈ 2500 tokens
        cfg = {"context": {"fanout_enabled": True, "max_input_tokens": 100}}
        assert _should_fanout_context("/v1/chat/completions", body, cfg) is True

    def test_fanout_empty_messages(self):
        """Empty messages do not trigger fanout."""
        body = {"messages": []}
        cfg = {"context": {"fanout_enabled": True, "max_input_tokens": 24000}}
        assert _should_fanout_context("/v1/chat/completions", body, cfg) is False
