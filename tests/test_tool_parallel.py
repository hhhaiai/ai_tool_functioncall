"""Tests for parallel tool execution and dependency analysis."""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from src.gateway_tool_runtime import (
    ToolCall,
    _normalize_tool_call,
    _extract_tool_calls,
    _parse_text_tool_calls,
    _response_text,
    _looks_like_context_rejection,
)


class TestToolCallNormalization:
    def test_normalize_chat_tool_call(self):
        tc = ToolCall(
            call_id="call_123",
            name="calculator",
            arguments={"expression": "2+2"},
            raw={"id": "call_123", "type": "function", "function": {"name": "calculator", "arguments": "{}"}},
        )
        result = _normalize_tool_call(tc)
        assert result is not None
        assert result.name == "calculator"
        assert result.call_id == "call_123"

    def test_normalize_tool_call_fields(self):
        tc = ToolCall(
            call_id="call_456",
            name="read_file",
            arguments={"path": "test.py"},
            raw={},
        )
        result = _normalize_tool_call(tc)
        assert result.name == "read_file"
        assert result.call_id == "call_456"
        assert result.arguments == {"path": "test.py"}


class TestToolCallExtraction:
    def test_extract_chat_tool_calls(self):
        response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "calculator", "arguments": "{}"}},
                        {"id": "call_2", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                    ],
                },
                "finish_reason": "tool_calls",
            }]
        }
        calls = _extract_tool_calls("/v1/chat/completions", response)
        assert len(calls) == 2
        assert isinstance(calls[0], ToolCall)

    def test_extract_responses_tool_calls(self):
        response = {
            "output": [
                {"type": "function_call", "name": "tool1", "call_id": "c1", "arguments": "{}"},
                {"type": "function_call", "name": "tool2", "call_id": "c2", "arguments": "{}"},
            ]
        }
        calls = _extract_tool_calls("/v1/responses", response)
        assert len(calls) == 2

    def test_extract_anthropic_tool_calls(self):
        response = {
            "content": [
                {"type": "tool_use", "id": "t1", "name": "tool1", "input": {}},
                {"type": "tool_use", "id": "t2", "name": "tool2", "input": {}},
            ],
            "stop_reason": "tool_use",
        }
        calls = _extract_tool_calls("/v1/messages", response)
        assert len(calls) == 2

    def test_extract_no_tool_calls(self):
        response = {"choices": [{"message": {"role": "assistant", "content": "Hello!"}}]}
        calls = _extract_tool_calls("/v1/chat/completions", response)
        assert len(calls) == 0

    def test_extract_empty_response(self):
        calls = _extract_tool_calls("/v1/chat/completions", {})
        assert len(calls) == 0


class TestContextRejectionDetection:
    def test_detects_too_long(self):
        assert _looks_like_context_rejection("Error: text too long, please shorten") is True

    def test_detects_send_in_parts(self):
        assert _looks_like_context_rejection("Please send it in parts") is True

    def test_detects_chinese_rejection(self):
        assert _looks_like_context_rejection("内容过长，请缩短") is True

    def test_no_false_positive(self):
        assert _looks_like_context_rejection("Here is the answer to your question") is False

    def test_empty_text(self):
        assert _looks_like_context_rejection("") is False


class TestResponseText:
    def test_chat_response_text(self):
        response = {"choices": [{"message": {"content": "Hello world"}}]}
        text = _response_text("/v1/chat/completions", response)
        assert text == "Hello world"

    def test_chat_response_list_content(self):
        response = {"choices": [{"message": {"content": [{"type": "text", "text": "Hi"}]}}]}
        text = _response_text("/v1/chat/completions", response)
        assert "Hi" in text

    def test_responses_response_text(self):
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "Hello"}]}]}
        text = _response_text("/v1/responses", response)
        assert isinstance(text, str)

    def test_anthropic_response_text(self):
        response = {"content": [{"type": "text", "text": "Hello"}]}
        text = _response_text("/v1/messages", response)
        assert isinstance(text, str)

    def test_empty_response(self):
        text = _response_text("/v1/chat/completions", {})
        assert text == "" or text is not None


class TestParallelToolExecution:
    def test_parallel_read_only_tools(self):
        from src.gateway_cache import ToolResultCache

        cache = ToolResultCache(ttl_seconds=60)
        results = []
        errors = []

        def execute_tool(idx, tool_name, args):
            try:
                cached = cache.get(tool_name, args)
                if cached:
                    return cached
                time.sleep(0.01)
                result = f"result_{idx}_{tool_name}"
                cache.put(tool_name, args, result)
                return result
            except Exception as e:
                errors.append(e)
                return None

        tools = [
            ("Read", {"path": "file1.py"}),
            ("Read", {"path": "file2.py"}),
            ("Glob", {"pattern": "*.py"}),
            ("Grep", {"pattern": "def main"}),
        ]

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(execute_tool, i, name, args)
                for i, (name, args) in enumerate(tools)
            ]
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception as e:
                    errors.append(e)

        assert len(errors) == 0
        assert len(results) == 4

    def test_sequential_write_tools(self):
        execution_order = []
        lock = threading.Lock()

        def execute_write(tool_name, args):
            with lock:
                execution_order.append(tool_name)
            time.sleep(0.01)
            return f"done {tool_name}"

        tools = [
            ("Write", {"path": "file1.py", "content": "a"}),
            ("Write", {"path": "file2.py", "content": "b"}),
            ("Edit", {"path": "file3.py", "old": "a", "new": "b"}),
        ]

        for name, args in tools:
            execute_write(name, args)

        assert execution_order == ["Write", "Write", "Edit"]

    def test_mixed_tool_execution_strategy(self):
        read_results = []
        write_results = []
        errors = []

        def classify_and_execute(tool_name, args):
            try:
                read_only_tools = {"Read", "Glob", "Grep", "FileInfo", "LS", "Tree"}
                if tool_name in read_only_tools:
                    time.sleep(0.01)
                    read_results.append(tool_name)
                else:
                    time.sleep(0.01)
                    write_results.append(tool_name)
            except Exception as e:
                errors.append(e)

        tools = [
            ("Read", {"path": "a.py"}),
            ("Read", {"path": "b.py"}),
            ("Glob", {"pattern": "*.py"}),
            ("Write", {"path": "c.py", "content": "x"}),
            ("Edit", {"path": "d.py", "old": "a", "new": "b"}),
        ]

        read_tools = [(n, a) for n, a in tools if n in {"Read", "Glob", "Grep", "FileInfo", "LS", "Tree"}]
        write_tools = [(n, a) for n, a in tools if n not in {"Read", "Glob", "Grep", "FileInfo", "LS", "Tree"}]

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(classify_and_execute, n, a) for n, a in read_tools]
            for f in as_completed(futures):
                f.result()

        for n, a in write_tools:
            classify_and_execute(n, a)

        assert len(errors) == 0
        assert len(read_results) == 3
        assert len(write_results) == 2


class TestToolDependencyAnalysis:
    def test_read_tools_independent(self):
        tools = [
            {"name": "Read", "arguments": {"path": "file1.py"}},
            {"name": "Read", "arguments": {"path": "file2.py"}},
            {"name": "Glob", "arguments": {"pattern": "*.py"}},
        ]
        read_only = {"Read", "Glob", "Grep", "FileInfo", "LS", "Tree", "WebFetch", "WebSearch"}
        parallelizable = [t for t in tools if t["name"] in read_only]
        assert len(parallelizable) == 3

    def test_write_tools_dependent(self):
        tools = [
            {"name": "Write", "arguments": {"path": "file.py", "content": "a"}},
            {"name": "Edit", "arguments": {"path": "file.py", "old": "a", "new": "b"}},
        ]
        write_tools = {"Write", "Edit", "Bash"}
        sequential = [t for t in tools if t["name"] in write_tools]
        assert len(sequential) == 2

    def test_mixed_dependencies(self):
        tools = [
            {"name": "Read", "arguments": {"path": "input.py"}},
            {"name": "Glob", "arguments": {"pattern": "*.py"}},
            {"name": "Write", "arguments": {"path": "output.py", "content": "x"}},
            {"name": "Read", "arguments": {"path": "config.json"}},
        ]
        read_only = {"Read", "Glob", "Grep", "FileInfo", "LS", "Tree"}
        parallel_batch = [t for t in tools if t["name"] in read_only]
        sequential_batch = [t for t in tools if t["name"] not in read_only]
        assert len(parallel_batch) == 3
        assert len(sequential_batch) == 1


@pytest.mark.integration
class TestToolExecutionIntegration:
    def test_tool_cache_workflow(self):
        from src.gateway_cache import ToolResultCache

        cache = ToolResultCache(ttl_seconds=60)
        file_content = '{"content": "import os\\nprint(os.getcwd())"}'
        args = {"path": "src/main.py"}

        result = cache.get("Read", args)
        assert result is None

        cache.put("Read", args, file_content)
        result = cache.get("Read", args)
        assert result == file_content

        result = cache.get("Read", {"path": "src/other.py"})
        assert result is None

    def test_concurrent_tool_cache_access(self):
        from src.gateway_cache import ToolResultCache

        cache = ToolResultCache(ttl_seconds=60)
        errors = []

        def worker(thread_id):
            try:
                for i in range(10):
                    args = {"path": f"file_{thread_id}_{i}.py"}
                    cache.put("Read", args, f"content_{i}")
                    result = cache.get("Read", args)
                    assert result == f"content_{i}"
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
