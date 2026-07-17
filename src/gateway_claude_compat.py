"""Claude Code compatibility layer for the gateway.

This module provides compatibility with Claude Code's tool calling format,
mapping Claude Code tools to gateway's internal tool execution while
avoiding naming conflicts with downstream user tools.

Key design principle:
- Gateway's own tools use `gw_` prefix internally
- Claude Code tools are mapped transparently
- No naming conflicts with downstream tools
"""
from __future__ import annotations

import pathlib
import uuid
from typing import Any

Json = dict[str, Any]

# Claude Code tool definitions that the gateway can execute
# These map Claude Code tool names to gateway internal handlers
CLAUDE_CODE_TOOLS = {
    "Read": {
        "name": "Read",
        "description": "Reads a file from the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The absolute path to the file to read"},
                "offset": {"type": "integer", "description": "Line number to start reading from"},
                "limit": {"type": "integer", "description": "Number of lines to read"},
            },
            "required": ["file_path"],
        },
    },
    "Write": {
        "name": "Write",
        "description": "Writes a file to the local filesystem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The absolute path to the file"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["file_path", "content"],
        },
    },
    "Edit": {
        "name": "Edit",
        "description": "Performs exact string replacements in files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean", "default": False},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "Bash": {
        "name": "Bash",
        "description": "Executes a bash command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
                "description": {"type": "string"},
            },
            "required": ["command"],
        },
    },
    "Glob": {
        "name": "Glob",
        "description": "Finds files by glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
    "Grep": {
        "name": "Grep",
        "description": "Searches file contents using regex.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "include": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
    "WebFetch": {
        "name": "WebFetch",
        "description": "Fetches content from a URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["url"],
        },
    },
    "WebSearch": {
        "name": "WebSearch",
        "description": "Searches the web.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
}


def get_claude_code_tool_definitions() -> list[Json]:
    """Get Claude Code compatible tool definitions for upstream.

    Returns tool definitions in Anthropic format that the gateway
    can execute locally.
    """
    tools = []
    for tool_def in CLAUDE_CODE_TOOLS.values():
        tools.append({
            "name": tool_def["name"],
            "description": tool_def["description"],
            "input_schema": tool_def["input_schema"],
        })
    return tools


def is_claude_code_tool(name: str) -> bool:
    """Check if a tool name is a Claude Code tool that gateway can execute."""
    return name in CLAUDE_CODE_TOOLS


def execute_claude_code_tool(
    name: str,
    arguments: dict[str, Any],
    workspace_root: str = "",
    *,
    client_id: str | None = None,
) -> dict[str, Any]:
    """Execute a Claude Code tool locally in the gateway.

    Args:
        name: Tool name (e.g., "Read", "Bash")
        arguments: Tool arguments
        workspace_root: Workspace root for file operations

    Returns:
        Tool result dict with success/content fields
    """
    # This module is a protocol-compatibility facade only. Execution is routed
    # through the complete canonical runtime so permission checks, workspace
    # confinement, cache scoping, retries, auditing, and write/shell/network
    # policy cannot drift from the main Gateway path.
    try:
        from .gateway_builtin_tools import ToolCall
        from .gateway_tool_runtime import _execute_tool_call, _workspace_scope

        if not workspace_root:
            return {"success": False, "content": "workspace_root is required", "failure_type": "invalid_input"}

        canonical_args = dict(arguments or {})
        # Claude Code's compatibility schema uses a zero-based Read offset and
        # millisecond Bash timeout; canonical built-ins use one-based lines and
        # seconds respectively.
        if name == "Read" and "offset" in canonical_args:
            canonical_args["offset"] = max(1, int(canonical_args["offset"]) + 1)
        if name == "Bash" and "timeout" in canonical_args:
            canonical_args["timeout"] = max(0.001, float(canonical_args["timeout"]) / 1000.0)

        call = ToolCall(
            call_id=f"claude_compat_{uuid.uuid4().hex}",
            name=name,
            arguments=canonical_args,
            raw={"name": name, "input": canonical_args},
        )
        scope_body = {"client_id": client_id} if client_id else {}
        with _workspace_scope(pathlib.Path(workspace_root), scope_body):
            result = _execute_tool_call(call, provider="claude_compat", client_id=client_id)

        success = bool(result.success)
        failure_type = result.failure_type
        if name == "Bash" and success and str(result.content).startswith("exit_code="):
            first_line = str(result.content).splitlines()[0]
            if first_line != "exit_code=0":
                success = False
                failure_type = "execution_failed"
        payload = {"success": success, "content": str(result.content)}
        if not success:
            payload["failure_type"] = failure_type or "execution_failed"
        return payload
    except Exception as exc:
        return {"success": False, "content": f"Tool execution error: {exc}", "failure_type": "execution_failed"}


def format_tool_result_for_anthropic(
    tool_use_id: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Format a tool result for Anthropic Messages API response.

    Args:
        tool_use_id: The ID from the tool_use block
        result: Tool execution result

    Returns:
        Formatted tool_result block
    """
    content = result.get("content", "")
    is_error = not result.get("success", True)

    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


def format_tool_use_for_anthropic(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_id: str,
) -> dict[str, Any]:
    """Format a tool use block for Anthropic Messages API.

    Args:
        tool_name: Name of the tool
        tool_input: Tool input parameters
        tool_id: Unique tool call ID

    Returns:
        Formatted tool_use block
    """
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": tool_name,
        "input": tool_input,
    }


def extract_tool_uses_from_response(response: dict) -> list[dict]:
    """Extract tool_use blocks from an Anthropic Messages response.

    Args:
        response: Anthropic Messages API response

    Returns:
        List of tool use dicts with name, id, input
    """
    tool_uses = []
    content = response.get("content", [])

    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tool_uses.append({
                "name": block["name"],
                "id": block["id"],
                "input": block.get("input", {}),
            })

    return tool_uses


def build_tool_result_message(
    tool_results: list[dict],
) -> dict[str, Any]:
    """Build a user message containing tool results.

    Args:
        tool_results: List of formatted tool_result blocks

    Returns:
        User message with tool results
    """
    return {
        "role": "user",
        "content": tool_results,
    }
