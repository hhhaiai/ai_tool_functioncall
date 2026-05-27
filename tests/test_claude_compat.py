"""Tests for Claude Code compatibility layer."""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
from unittest.mock import patch

import pytest

from src.gateway_claude_compat import (
    CLAUDE_CODE_TOOLS,
    get_claude_code_tool_definitions,
    is_claude_code_tool,
    execute_claude_code_tool,
    format_tool_result_for_anthropic,
    format_tool_use_for_anthropic,
    extract_tool_uses_from_response,
    build_tool_result_message,
)


class TestClaudeCodeToolDefinitions:
    """Tests for Claude Code tool definitions."""

    def test_all_tools_have_required_fields(self):
        for name, tool_def in CLAUDE_CODE_TOOLS.items():
            assert "name" in tool_def, f"{name} missing name"
            assert "description" in tool_def, f"{name} missing description"
            assert "input_schema" in tool_def, f"{name} missing input_schema"
            assert tool_def["name"] == name

    def test_input_schema_valid(self):
        for name, tool_def in CLAUDE_CODE_TOOLS.items():
            schema = tool_def["input_schema"]
            assert schema.get("type") == "object"
            assert "properties" in schema

    def test_get_tool_definitions(self):
        tools = get_claude_code_tool_definitions()
        assert len(tools) == len(CLAUDE_CODE_TOOLS)
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool

    def test_tool_definitions_json_serializable(self):
        tools = get_claude_code_tool_definitions()
        json_str = json.dumps(tools)
        assert len(json_str) > 0


class TestIsClaudeCodeTool:
    """Tests for tool name checking."""

    def test_known_tools(self):
        assert is_claude_code_tool("Read") is True
        assert is_claude_code_tool("Write") is True
        assert is_claude_code_tool("Edit") is True
        assert is_claude_code_tool("Bash") is True
        assert is_claude_code_tool("Glob") is True
        assert is_claude_code_tool("Grep") is True
        assert is_claude_code_tool("WebFetch") is True
        assert is_claude_code_tool("WebSearch") is True

    def test_unknown_tools(self):
        assert is_claude_code_tool("UnknownTool") is False
        assert is_claude_code_tool("calculator") is False
        assert is_claude_code_tool("") is False


class TestExecuteRead:
    """Tests for Read tool execution."""

    def test_read_existing_file(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello, World!")

        result = execute_claude_code_tool(
            "Read",
            {"file_path": str(test_file)},
            str(tmp_path),
        )

        assert result["success"] is True
        assert "Hello, World!" in result["content"]

    def test_read_with_line_numbers(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3")

        result = execute_claude_code_tool(
            "Read",
            {"file_path": str(test_file)},
            str(tmp_path),
        )

        assert result["success"] is True
        assert "1\tline1" in result["content"]
        assert "2\tline2" in result["content"]

    def test_read_with_offset_and_limit(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5")

        result = execute_claude_code_tool(
            "Read",
            {"file_path": str(test_file), "offset": 1, "limit": 2},
            str(tmp_path),
        )

        assert result["success"] is True
        assert "2\tline2" in result["content"]
        assert "3\tline3" in result["content"]

    def test_read_nonexistent_file(self, tmp_path):
        result = execute_claude_code_tool(
            "Read",
            {"file_path": str(tmp_path / "nonexistent.txt")},
            str(tmp_path),
        )

        assert result["success"] is False
        assert "not found" in result["content"].lower()

    def test_read_relative_path(self, tmp_path):
        test_file = tmp_path / "subdir" / "test.txt"
        test_file.parent.mkdir()
        test_file.write_text("content")

        result = execute_claude_code_tool(
            "Read",
            {"file_path": "subdir/test.txt"},
            str(tmp_path),
        )

        assert result["success"] is True
        assert "content" in result["content"]


class TestExecuteWrite:
    """Tests for Write tool execution."""

    def test_write_new_file(self, tmp_path):
        test_file = tmp_path / "new.txt"

        result = execute_claude_code_tool(
            "Write",
            {"file_path": str(test_file), "content": "Hello!"},
            str(tmp_path),
        )

        assert result["success"] is True
        assert test_file.read_text() == "Hello!"

    def test_write_overwrite(self, tmp_path):
        test_file = tmp_path / "existing.txt"
        test_file.write_text("old content")

        result = execute_claude_code_tool(
            "Write",
            {"file_path": str(test_file), "content": "new content"},
            str(tmp_path),
        )

        assert result["success"] is True
        assert test_file.read_text() == "new content"

    def test_write_creates_directories(self, tmp_path):
        test_file = tmp_path / "a" / "b" / "c" / "file.txt"

        result = execute_claude_code_tool(
            "Write",
            {"file_path": str(test_file), "content": "nested"},
            str(tmp_path),
        )

        assert result["success"] is True
        assert test_file.read_text() == "nested"


class TestExecuteEdit:
    """Tests for Edit tool execution."""

    def test_edit_single_replacement(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello World")

        result = execute_claude_code_tool(
            "Edit",
            {
                "file_path": str(test_file),
                "old_string": "World",
                "new_string": "Python",
            },
            str(tmp_path),
        )

        assert result["success"] is True
        assert test_file.read_text() == "Hello Python"

    def test_edit_replace_all(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("aaa bbb aaa bbb")

        result = execute_claude_code_tool(
            "Edit",
            {
                "file_path": str(test_file),
                "old_string": "aaa",
                "new_string": "xxx",
                "replace_all": True,
            },
            str(tmp_path),
        )

        assert result["success"] is True
        assert test_file.read_text() == "xxx bbb xxx bbb"

    def test_edit_string_not_found(self, tmp_path):
        test_file = tmp_path / "test.txt"
        test_file.write_text("Hello World")

        result = execute_claude_code_tool(
            "Edit",
            {
                "file_path": str(test_file),
                "old_string": "NotFound",
                "new_string": "xxx",
            },
            str(tmp_path),
        )

        assert result["success"] is False


class TestExecuteBash:
    """Tests for Bash tool execution."""

    def test_bash_echo(self, tmp_path):
        with patch("src.gateway_config.load_config", return_value={"tools": {"shell_enabled": True}}):
            result = execute_claude_code_tool(
                "Bash",
                {"command": "echo hello"},
                str(tmp_path),
            )

        assert result["success"] is True
        assert "hello" in result["content"]

    def test_bash_with_description(self, tmp_path):
        with patch("src.gateway_config.load_config", return_value={"tools": {"shell_enabled": True}}):
            result = execute_claude_code_tool(
                "Bash",
                {"command": "echo test", "description": "Print test"},
                str(tmp_path),
            )

        assert result["success"] is True

    def test_bash_failure(self, tmp_path):
        with patch("src.gateway_config.load_config", return_value={"tools": {"shell_enabled": True}}):
            result = execute_claude_code_tool(
                "Bash",
                {"command": "false"},
                str(tmp_path),
            )

        assert result["success"] is False

    def test_bash_timeout(self, tmp_path):
        with patch("src.gateway_config.load_config", return_value={"tools": {"shell_enabled": True}}):
            result = execute_claude_code_tool(
                "Bash",
                {"command": "sleep 10", "timeout": 100},  # 100ms timeout
                str(tmp_path),
            )

        assert result["success"] is False


class TestExecuteGlob:
    """Tests for Glob tool execution."""

    def test_glob_pattern(self, tmp_path):
        (tmp_path / "a.py").touch()
        (tmp_path / "b.py").touch()
        (tmp_path / "c.txt").touch()

        result = execute_claude_code_tool(
            "Glob",
            {"pattern": "*.py", "path": str(tmp_path)},
            str(tmp_path),
        )

        assert result["success"] is True
        assert "a.py" in result["content"]
        assert "b.py" in result["content"]
        assert "c.txt" not in result["content"]

    def test_glob_recursive(self, tmp_path):
        (tmp_path / "a.py").touch()
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "b.py").touch()

        result = execute_claude_code_tool(
            "Glob",
            {"pattern": "**/*.py", "path": str(tmp_path)},
            str(tmp_path),
        )

        assert result["success"] is True


class TestExecuteGrep:
    """Tests for Grep tool execution."""

    def test_grep_basic(self, tmp_path):
        test_file = tmp_path / "test.py"
        test_file.write_text("def hello():\n    print('hello')\n")

        result = execute_claude_code_tool(
            "Grep",
            {"pattern": "hello", "path": str(tmp_path)},
            str(tmp_path),
        )

        assert result["success"] is True
        assert "hello" in result["content"]


class TestToolResultFormatting:
    """Tests for tool result formatting."""

    def test_format_success_result(self):
        result = format_tool_result_for_anthropic(
            "call_123",
            {"success": True, "content": "File content here"},
        )

        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "call_123"
        assert result["content"] == "File content here"
        assert result.get("is_error") is not True

    def test_format_error_result(self):
        result = format_tool_result_for_anthropic(
            "call_456",
            {"success": False, "content": "File not found"},
        )

        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "call_456"
        assert result["is_error"] is True


class TestToolUseFormatting:
    """Tests for tool use formatting."""

    def test_format_tool_use(self):
        tool_use = format_tool_use_for_anthropic(
            "Read",
            {"file_path": "/path/to/file"},
            "call_789",
        )

        assert tool_use["type"] == "tool_use"
        assert tool_use["id"] == "call_789"
        assert tool_use["name"] == "Read"
        assert tool_use["input"]["file_path"] == "/path/to/file"


class TestExtractToolUses:
    """Tests for extracting tool uses from response."""

    def test_extract_single_tool_use(self):
        response = {
            "content": [
                {"type": "text", "text": "I'll read the file."},
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "Read",
                    "input": {"file_path": "/test.py"},
                },
            ]
        }

        tool_uses = extract_tool_uses_from_response(response)
        assert len(tool_uses) == 1
        assert tool_uses[0]["name"] == "Read"
        assert tool_uses[0]["id"] == "call_1"

    def test_extract_multiple_tool_uses(self):
        response = {
            "content": [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "Read",
                    "input": {"file_path": "/a.py"},
                },
                {
                    "type": "tool_use",
                    "id": "call_2",
                    "name": "Read",
                    "input": {"file_path": "/b.py"},
                },
            ]
        }

        tool_uses = extract_tool_uses_from_response(response)
        assert len(tool_uses) == 2

    def test_extract_no_tool_uses(self):
        response = {
            "content": [
                {"type": "text", "text": "Hello!"},
            ]
        }

        tool_uses = extract_tool_uses_from_response(response)
        assert len(tool_uses) == 0


class TestBuildToolResultMessage:
    """Tests for building tool result messages."""

    def test_build_message(self):
        results = [
            format_tool_result_for_anthropic("call_1", {"success": True, "content": "result1"}),
            format_tool_result_for_anthropic("call_2", {"success": True, "content": "result2"}),
        ]

        message = build_tool_result_message(results)
        assert message["role"] == "user"
        assert len(message["content"]) == 2


@pytest.mark.integration
class TestClaudeCodeWorkflow:
    """Integration tests for Claude Code workflow."""

    def test_full_tool_cycle(self, tmp_path):
        """Test complete tool use/result cycle."""
        # 1. Create a test file
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")

        # 2. Simulate Claude Code sending tool_use
        tool_use = {
            "type": "tool_use",
            "id": "call_test_1",
            "name": "Read",
            "input": {"file_path": str(test_file)},
        }

        # 3. Execute the tool
        result = execute_claude_code_tool(
            tool_use["name"],
            tool_use["input"],
            str(tmp_path),
        )

        # 4. Format result for response back
        tool_result = format_tool_result_for_anthropic(tool_use["id"], result)

        assert result["success"] is True
        assert "print('hello')" in result["content"]
        assert tool_result["type"] == "tool_result"
        assert tool_result["tool_use_id"] == "call_test_1"

    def test_multi_tool_parallel_execution(self, tmp_path):
        """Test executing multiple tools in parallel."""
        # Create test files
        for i in range(5):
            (tmp_path / f"file{i}.txt").write_text(f"content{i}")

        # Simulate parallel tool calls
        tool_calls = [
            {"name": "Read", "id": f"call_{i}", "input": {"file_path": str(tmp_path / f"file{i}.txt")}}
            for i in range(5)
        ]

        results = []
        for call in tool_calls:
            result = execute_claude_code_tool(call["name"], call["input"], str(tmp_path))
            results.append(format_tool_result_for_anthropic(call["id"], result))

        assert len(results) == 5
        for i, r in enumerate(results):
            assert r["tool_use_id"] == f"call_{i}"
            assert f"content{i}" in r["content"]
