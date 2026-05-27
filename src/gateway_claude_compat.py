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

import json
import os
import pathlib
from typing import Any, Optional

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
) -> dict[str, Any]:
    """Execute a Claude Code tool locally in the gateway.

    Args:
        name: Tool name (e.g., "Read", "Bash")
        arguments: Tool arguments
        workspace_root: Workspace root for file operations

    Returns:
        Tool result dict with success/content fields
    """
    try:
        if name == "Read":
            return _execute_read(arguments, workspace_root)
        elif name == "Write":
            return _execute_write(arguments, workspace_root)
        elif name == "Edit":
            return _execute_edit(arguments, workspace_root)
        elif name == "Bash":
            return _execute_bash(arguments, workspace_root)
        elif name == "Glob":
            return _execute_glob(arguments, workspace_root)
        elif name == "Grep":
            return _execute_grep(arguments, workspace_root)
        elif name == "WebFetch":
            return _execute_web_fetch(arguments)
        elif name == "WebSearch":
            return _execute_web_search(arguments)
        else:
            return {"success": False, "content": f"Unknown tool: {name}"}
    except Exception as e:
        return {"success": False, "content": f"Tool execution error: {str(e)}"}


def _resolve_path(path: str, workspace_root: str) -> pathlib.Path:
    """Resolve a path, making it absolute if relative. Enforces workspace containment."""
    p = pathlib.Path(path)
    if not p.is_absolute() and workspace_root:
        p = pathlib.Path(workspace_root) / p
    resolved = p.resolve()
    if workspace_root:
        root = pathlib.Path(workspace_root).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise PermissionError(f"path escapes workspace root: {path}")
    return resolved


def _execute_read(args: dict, workspace_root: str) -> dict:
    """Execute Read tool."""
    file_path = args.get("file_path", "")
    if not file_path:
        return {"success": False, "content": "file_path is required"}

    p = _resolve_path(file_path, workspace_root)

    if not p.exists():
        return {"success": False, "content": f"File not found: {p}"}

    if not p.is_file():
        return {"success": False, "content": f"Not a file: {p}"}

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")

        offset = args.get("offset", 0)
        limit = args.get("limit", len(lines))

        selected = lines[offset:offset + limit]

        # Add line numbers (cat -n format)
        numbered = []
        for i, line in enumerate(selected, start=offset + 1):
            numbered.append(f"{i}\t{line}")

        return {"success": True, "content": "\n".join(numbered)}
    except Exception as e:
        return {"success": False, "content": f"Error reading file: {str(e)}"}


def _execute_write(args: dict, workspace_root: str) -> dict:
    """Execute Write tool."""
    file_path = args.get("file_path", "")
    content = args.get("content", "")

    if not file_path:
        return {"success": False, "content": "file_path is required"}

    p = _resolve_path(file_path, workspace_root)

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"success": True, "content": f"File written: {p}"}
    except Exception as e:
        return {"success": False, "content": f"Error writing file: {str(e)}"}


def _execute_edit(args: dict, workspace_root: str) -> dict:
    """Execute Edit tool."""
    file_path = args.get("file_path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = args.get("replace_all", False)

    if not file_path:
        return {"success": False, "content": "file_path is required"}

    p = _resolve_path(file_path, workspace_root)

    if not p.exists():
        return {"success": False, "content": f"File not found: {p}"}

    try:
        content = p.read_text(encoding="utf-8")

        if old_string not in content:
            return {"success": False, "content": f"old_string not found in {p}"}

        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        p.write_text(new_content, encoding="utf-8")
        return {"success": True, "content": f"File edited: {p}"}
    except Exception as e:
        return {"success": False, "content": f"Error editing file: {str(e)}"}


def _execute_bash(args: dict, workspace_root: str) -> dict:
    """Execute Bash tool. Requires shell_enabled in config."""
    import subprocess
    import threading

    # Safety check: require shell_enabled config
    try:
        from .gateway_config import load_config
        cfg = load_config()
        if not cfg.get("tools", {}).get("shell_enabled", False):
            return {"success": False, "content": "Shell execution is disabled in gateway config. Set tools.shell_enabled=true to enable."}
    except Exception:
        return {"success": False, "content": "Cannot verify shell_enabled config"}

    command = args.get("command", "")
    timeout = args.get("timeout", 120000) / 1000  # Convert ms to seconds

    if not command:
        return {"success": False, "content": "command is required"}

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workspace_root or None,
            capture_output=True,
            text=True,
            timeout=min(timeout, 600),  # Max 10 minutes
        )

        output = result.stdout
        if result.stderr:
            output += "\n" + result.stderr if output else result.stderr

        return {
            "success": result.returncode == 0,
            "content": output[:100000],  # Limit output size
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "content": "Command timed out"}
    except Exception as e:
        return {"success": False, "content": f"Error: {str(e)}"}


def _execute_glob(args: dict, workspace_root: str) -> dict:
    """Execute Glob tool."""
    import glob as glob_module

    pattern = args.get("pattern", "")
    path = args.get("path", workspace_root or ".")

    if not pattern:
        return {"success": False, "content": "pattern is required"}

    try:
        full_pattern = os.path.join(path, pattern)
        matches = glob_module.glob(full_pattern, recursive=True)

        # Limit results
        total = len(matches)
        if total > 1000:
            matches = matches[:1000]
            matches.append(f"... (truncated, showing 1000 of {total} matches)")

        return {"success": True, "content": "\n".join(matches)}
    except Exception as e:
        return {"success": False, "content": f"Error: {str(e)}"}


def _execute_grep(args: dict, workspace_root: str) -> dict:
    """Execute Grep tool."""
    import subprocess

    pattern = args.get("pattern", "")
    include = args.get("include", "")
    path = args.get("path", workspace_root or ".")

    if not pattern:
        return {"success": False, "content": "pattern is required"}

    try:
        cmd = ["grep", "-r", "-n", pattern]
        if include:
            cmd.extend(["--include", include])
        cmd.append(path)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

        output = result.stdout[:100000]  # Limit output
        return {"success": True, "content": output}
    except subprocess.TimeoutExpired:
        return {"success": False, "content": "Grep timed out"}
    except Exception as e:
        return {"success": False, "content": f"Error: {str(e)}"}


def _validate_url_not_private(url: str) -> None:
    """Block SSRF attacks by rejecting private/loopback/link-local IPs."""
    import ipaddress
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            raise ValueError(f"URL targets a private/loopback address: {hostname}")
    except ValueError as e:
        if "private/loopback" in str(e):
            raise
        # hostname is a domain name, not an IP -- allow


def _execute_web_fetch(args: dict) -> dict:
    """Execute WebFetch tool."""
    import urllib.request
    import urllib.error
    import re

    url = args.get("url", "")
    if not url:
        return {"success": False, "content": "url is required"}

    # SSRF protection
    try:
        _validate_url_not_private(url)
    except ValueError as e:
        return {"success": False, "content": f"URL blocked: {e}"}

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Gateway/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")

            # Simple HTML to text conversion
            content = re.sub(r"<script[^>]*>.*?</script>", "", content, flags=re.DOTALL)
            content = re.sub(r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL)
            content = re.sub(r"<[^>]+>", " ", content)
            content = re.sub(r"\s+", " ", content).strip()

            return {"success": True, "content": content[:50000]}
    except urllib.error.HTTPError as e:
        return {"success": False, "content": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"success": False, "content": f"Error: {str(e)}"}


def _execute_web_search(args: dict) -> dict:
    """Execute WebSearch tool via DuckDuckGo HTML."""
    import urllib.parse
    import html as html_module

    query = args.get("query", "")
    if not query:
        return {"success": False, "content": "query is required"}

    max_results = max(1, min(int(args.get("max_results") or 5), 10))
    base_url = os.environ.get("GATEWAY_SEARCH_URL", "https://duckduckgo.com/html/")
    separator = "&" if "?" in base_url else "?"
    url = base_url + separator + urllib.parse.urlencode({"q": query})

    try:
        req = urllib.request.Request(
            url,
            headers={
                "user-agent": "Gateway/1.0",
                "accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html_text = resp.read(500_000).decode("utf-8", errors="replace")
    except Exception as e:
        return {"success": False, "content": f"Web search failed: {str(e)}"}

    results = []
    for match in re.finditer(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html_text,
        flags=re.I | re.S,
    ):
        href = html_module.unescape(match.group(1))
        if "uddg=" in href:
            parsed = urllib.parse.urlparse(href)
            query_params = urllib.parse.parse_qs(parsed.query)
            href = query_params.get("uddg", [href])[0]
        title = re.sub(r"<[^>]+>", "", match.group(2))
        title = html_module.unescape(re.sub(r"\s+", " ", title)).strip()
        tail = html_text[match.end():match.end() + 1200]
        snippet_match = re.search(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', tail, flags=re.I | re.S)
        snippet = ""
        if snippet_match:
            snippet = html_module.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", snippet_match.group(1)))).strip()
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break

    if not results:
        return {"success": True, "content": json.dumps({"query": query, "results": [], "detail": "no parseable results"})}
    return {"success": True, "content": json.dumps({"query": query, "results": results}, ensure_ascii=False)}


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
