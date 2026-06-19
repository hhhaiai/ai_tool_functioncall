#!/usr/bin/env python3
"""Tool runtime for the gateway.

Handles tool call parsing, normalization, execution, and orchestration.
"""
from __future__ import annotations

import json
import copy
import logging
import os

_logger = logging.getLogger(__name__)
import pathlib
import re
import subprocess
import threading
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from typing import Any

from .gateway_builtin_tools import (
    BUILTIN_TOOLS,
    ToolCall,
    ToolResult,
    _parse_json_arguments,
    _resolve_workspace_path,
    _response_text,
    _workspace_root,
)
from .gateway_config import (
    SUPPORTED_PATHS,
    _config_env,
    _configured_max_tool_rounds,
    _gateway_config,
    _upstream_config,
    load_config,
)
from .gateway_context import (
    _approx_token_count,
    _body_token_estimate,
    _context_config,
    _inject_recalled_memories,
    _maybe_compact_request_for_upstream,
    _remember_conversation_turn,
    _run_context_fanout,
)
from .gateway_errors import GatewayError, ToolExecutionError, UpstreamHTTPError
from .gateway_http_actions import _call_http_action, _http_action_by_name
from .gateway_logging import _record_tool_failure, _record_tool_stat
from .gateway_mcp import _mcp_call_tool, _mcp_parse_public_name, _mcp_server_by_name
from .gateway_protocol import (
    _convert_request_to_upstream,
    _convert_response_to_downstream,
    _forced_tool_name_from_choice,
    _from_openai_chat_response,
    _last_user_text,
    _replace_last_user_text,
    _without_tools,
)
from .gateway_proxy import NativeProxyClient
from .gateway_streaming import _merge_builtin_tools

Json = dict[str, Any]

DEFAULT_MAX_TOOL_ROUNDS = 5

# Concurrency control globals
_REQUEST_SEMAPHORE_LOCK = threading.Lock()
_REQUEST_SEMAPHORE: threading.BoundedSemaphore | None = None
_REQUEST_SEMAPHORE_SIZE: int = 0

# Path-like regex for cleaning text tool paths
_PATHISH_RE = re.compile(
    r"@?(?P<path>"
    r"(?:~?/|/|\.{1,2}/)[^\s<>'\"`|]+"
    r"|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.@%+=:,/-]+"
    r"|[A-Za-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|json|jsonl|toml|yaml|yml|md|txt|sh|bash|zsh|env|ini|cfg|conf|html|css|sql|go|rs|java|kt|swift|c|cc|cpp|h|hpp)"
    r")"
)


# =============================================================================
# Tool-runtime parsing and normalization utilities
# =============================================================================

def _first_present(args: Json, names: tuple[str, ...]) -> Any:
    """Return the first present (non-None) value from args for the given names."""
    for name in names:
        if name in args and args[name] is not None:
            return args[name]
    return None


def _clean_tool_string(value: Any) -> Any:
    """Clean tool string by stripping whitespace and XML-like tags."""
    if not isinstance(value, str):
        return value
    cleaned = value.strip()
    cdata = re.fullmatch(r"<!\[CDATA\[(.*)\]\]>", cleaned, flags=re.S)
    if cdata:
        cleaned = cdata.group(1).strip()
    cleaned = re.sub(r"</?(?:parameter|function|tool|tool_call|invoke)>", "", cleaned, flags=re.I).strip()
    return cleaned


def _clean_text_tool_path(value: Any) -> Any:
    """Extract a single path from noisy text-tool fallback parameters.

    Weak upstreams sometimes put prose after a path, e.g.
    ``README.md\n<tool_call>`` or ``src/app.py\n\n--- report``. Passing the
    whole blob to filesystem tools causes false not_found/File name too long
    failures, so path-like parameters are reduced to the first path token.
    """
    cleaned = _clean_tool_string(value)
    if not isinstance(cleaned, str):
        return cleaned
    text = cleaned.strip()
    if not text:
        return text
    for line in text.splitlines():
        candidate = line.strip().strip("`'\"")
        if not candidate:
            continue
        match = _PATHISH_RE.search(candidate)
        if match:
            return match.group("path").rstrip(".,;:)")
        if not re.match(r"^(?:[-*_]{3,}|#{1,6}\s|[*>]|\*\*)", candidate):
            return candidate.rstrip(".,;:)")
    return text


def _normalize_relative_pattern(value: Any) -> Any:
    """Normalize relative glob patterns to workspace-root relative."""
    value = _clean_tool_string(value)
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        return value.lstrip("/") or "*"
    return value


def _copy_model_override(body: Json) -> Json:
    """Copy body and override model with configured upstream model."""
    copied = dict(body)
    model = _config_env("UPSTREAM_MODEL", "")
    if model:
        copied["model"] = model
    return copied


def _has_requested_tools(body: Json) -> bool:
    """Check if the request body has tools defined."""
    tools = body.get("tools")
    return isinstance(tools, list) and bool(tools)


def _response_has_tool_calls(path: str, response: Json) -> bool:
    """Check if a response contains tool_calls in any protocol format."""
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message") or {}
            if isinstance(msg, dict) and (msg.get("tool_calls") or msg.get("function_call")):
                return True
        return False
    if path == "/v1/messages":
        for block in response.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        if response.get("stop_reason") == "tool_use":
            return True
        return False
    if path == "/v1/responses":
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") == "function_call":
                return True
        return False
    return False


def _extract_openai_tool_calls_for_stream(response: Json) -> list[dict]:
    """Extract tool_calls from an OpenAI response formatted for SSE streaming chunks.
    Returns list of delta-style tool_call objects with index field."""
    result: list[dict] = []
    choice = (response.get("choices") or [{}])[0] if isinstance(response.get("choices"), list) else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        return result
    tc_list = message.get("tool_calls")
    if isinstance(tc_list, list):
        for idx, tc in enumerate(tc_list):
            if not isinstance(tc, dict):
                continue
            func = tc.get("function") or {}
            result.append({
                "index": idx,
                "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": func.get("name") or "",
                    "arguments": func.get("arguments") or "{}",
                },
            })
    return result


def _fallback_response(path: str, text: str, *, status_note: str = "gateway_fallback") -> Json:
    """Generate a fallback response when upstream is unavailable."""
    model = _config_env("UPSTREAM_MODEL", "")
    output_tokens = _approx_token_count(text)
    usage = {"input_tokens": 0, "output_tokens": output_tokens, "total_tokens": output_tokens}
    if path == "/v1/messages":
        return {
            "id": f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": model,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
            "gateway_context": {"strategy": status_note},
        }
    if path == "/v1/responses":
        return {
            "id": f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "model": model,
            "status": "completed",
            "usage": usage,
            "gateway_context": {"strategy": status_note},
        }
    return {
        "id": f"chatcmpl_gateway_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": usage,
        "gateway_context": {"strategy": status_note},
    }


def _acquire_request_slot() -> threading.BoundedSemaphore | None:
    """Acquire a concurrency slot for request processing."""
    global _REQUEST_SEMAPHORE, _REQUEST_SEMAPHORE_SIZE
    gateway = _gateway_config()
    try:
        limit = int(gateway.get("max_concurrent_requests") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        return None
    with _REQUEST_SEMAPHORE_LOCK:
        if _REQUEST_SEMAPHORE is None or _REQUEST_SEMAPHORE_SIZE != limit:
            _REQUEST_SEMAPHORE = threading.BoundedSemaphore(limit)
            _REQUEST_SEMAPHORE_SIZE = limit
        sem = _REQUEST_SEMAPHORE
    try:
        timeout = float(gateway.get("concurrency_queue_timeout_seconds") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    ok = sem.acquire(timeout=timeout) if timeout > 0 else sem.acquire(blocking=False)
    if not ok:
        from .gateway_errors import GatewayBusyError
        raise GatewayBusyError(f"gateway concurrency limit reached ({limit})")
    return sem


@contextmanager
def _request_slot_scope():
    """Acquire and always release the configured HTTP request concurrency slot."""
    sem = _acquire_request_slot()
    try:
        yield
    finally:
        if sem is not None:
            sem.release()


def _get_marketplace():
    """Lazy import for marketplace to avoid circular imports."""
    if not hasattr(_get_marketplace, '_cache'):
        try:
            from marketplace import list_mcp_marketplace
            _get_marketplace._cache = list_mcp_marketplace
        except Exception:
            _get_marketplace._cache = lambda: []
    return _get_marketplace._cache


def _extract_client_project_dir(body: Json) -> pathlib.Path | None:
    """Detect the downstream client's project directory from request metadata.

    Claude Code injects session context into user/system blocks; Codex sends
    ``<environment_context><cwd>`` through Responses input.  Explicit metadata
    fields win first.  For natural-language/system-reminder text, prefer the
    latest matching path in the request because compacted summaries can contain
    stale ``Worktree:`` values from an older Gateway/service repo.
    """
    candidates: list[str] = []
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in (
            "gateway_workspace",
            "workspace_root",
            "project_dir",
            "projectDir",
            "cwd",
            "working_directory",
            "primary_working_directory",
            "worktree",
        ):
            value = metadata.get(key)
            if isinstance(value, str):
                candidates.append(value)
        user_meta = metadata.get("user_id")
        if isinstance(user_meta, str):
            candidates.append(user_meta)
    elif isinstance(metadata, str):
        candidates.append(metadata)

    for key in (
        "project_dir",
        "projectDir",
        "cwd",
        "working_directory",
        "primary_working_directory",
        "worktree",
    ):
        value = body.get(key)
        if isinstance(value, str):
            candidates.append(value)

    system = body.get("system")
    if isinstance(system, str):
        candidates.append(system)
    elif isinstance(system, list):
        for item in system:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    candidates.append(text)
            elif isinstance(item, str):
                candidates.append(item)

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        candidates.append(raw_input)
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                candidates.append(item)
            elif isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    candidates.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict):
                            text = part.get("text") or part.get("input_text")
                            if isinstance(text, str):
                                candidates.append(text)
                        elif isinstance(part, str):
                            candidates.append(part)
                text = item.get("text") or item.get("input") or item.get("input_text")
                if isinstance(text, str):
                    candidates.append(text)

    messages = body.get("messages") or []
    message_texts: list[str] = []
    # Ordered by specificity - more specific patterns first
    patterns = [
        # Claude Code worktree pattern (highest priority - explicit isolation)
        re.compile(r"Worktree:\*?\*?\s*(/.+?)(?:\s*(?:\n|$))"),
        # Claude Code primary working directory pattern
        re.compile(r"Primary working directory:\*?\*?\s*(/.+?)(?:\s*(?:\n|$))"),
        # JSON projectDir pattern (handles both "projectDir": "/path" and projectDir: /path)
        # Also handles trailing quotes from JSON strings
        re.compile(r"""projectDir["']?\s*[:=]\s*["']?(/\S+?)["']?(?:\s|,|$|})"""),
        re.compile(r"""project[_-]?dir["']?\s*[:=]\s*["']?(/\S+?)["']?(?:\s|,|$|})""", re.I),
        re.compile(r"""workspace[_-]?root["']?\s*[:=]\s*["']?(/\S+?)["']?(?:\s|,|$|})""", re.I),
        re.compile(r"""gateway[_-]?workspace["']?\s*[:=]\s*["']?(/\S+?)["']?(?:\s|,|$|})""", re.I),
        # Codex CLI environment context.
        re.compile(r"<cwd>\s*(/.+?)\s*</cwd>", re.I | re.S),
        # Generic working directory pattern (lower priority)
        re.compile(r"Working directory:\*?\*?\s*(/.+?)(?:\s*(?:\n|$))"),
        # CWD pattern (last resort)
        re.compile(r"(?:^|\s)CWD:\s*(/\S+)"),
    ]

    def path_from_text(text: str) -> pathlib.Path | None:
        matches: list[tuple[int, int, str]] = []
        for priority, pat in enumerate(patterns):
            for match in pat.finditer(text):
                matches.append((match.start(1), -priority, match.group(1)))
        # In Claude Code prompts the live environment block is appended after
        # older summaries, so the later path is the safest source of truth.
        for _pos, _priority, raw_path in sorted(matches, reverse=True):
            cleaned = raw_path.strip().rstrip("\"'.,;:")
            # SECURITY FIX: Do NOT validate path existence on Gateway server
            # The path is on the CLIENT machine, not the Gateway server
            # Just validate it looks like a valid absolute path
            if cleaned.startswith('/') or cleaned.startswith('~'):
                try:
                    candidate = pathlib.Path(cleaned).expanduser()
                    # Return the path - it exists on client machine, not here
                    return candidate
                except (OSError, ValueError):
                    continue
        return None

    for raw in candidates:
        try:
            # SECURITY FIX: Do NOT validate path existence on Gateway server
            # The path is on the CLIENT machine, not the Gateway server
            # Just validate it looks like a valid path and return it
            cleaned = raw.strip().rstrip("\"'.,;:")
            if cleaned.startswith('/') or cleaned.startswith('~'):
                candidate = pathlib.Path(cleaned).expanduser()
                return candidate
        except (OSError, ValueError):
            pass
        path = path_from_text(raw)
        if path is not None:
            return path

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            message_texts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    message_texts.append(str(item.get("text") or ""))

    # Claude Code appends the live environment later in the prompt.  Previous
    # compacted summaries can contain stale Worktree values from another repo,
    # so scan user messages from newest text block to oldest and within each
    # block prefer the last match.
    for text in reversed(message_texts):
        path = path_from_text(text)
        if path is not None:
            return path
    return None


def _create_anonymous_workspace(body: Json) -> pathlib.Path:
    """Create an isolated anonymous workspace for a session.

    SECURITY: Each session gets its own isolated temporary directory.
    This prevents cross-session contamination and protects the Gateway server.

    The workspace is identified by:
    1. session_id from metadata
    2. Request hash (model + first message)
    3. Random UUID as fallback
    """
    import hashlib
    import tempfile

    # Try to extract session_id from metadata
    session_id = None
    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        session_id = metadata.get("session_id") or metadata.get("conversation_id")
        if not session_id:
            try:
                user_meta = json.loads(metadata.get("user_id") or "{}")
            except Exception:
                user_meta = {}
            if isinstance(user_meta, dict):
                session_id = user_meta.get("session_id") or user_meta.get("conversation_id")

    # If no session_id, generate from request content
    if not session_id:
        # Hash: model + first user message (stable across same conversation)
        model = body.get("model", "")
        messages = body.get("messages", [])
        first_msg = ""
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    first_msg = content[:100]  # First 100 chars
                break

        hash_input = f"{model}:{first_msg}".encode("utf-8")
        session_id = hashlib.sha256(hash_input).hexdigest()[:16]

    # Create isolated workspace under .gateway_runtime/anonymous_spaces/
    base_dir = pathlib.Path.home() / ".gateway_runtime" / "anonymous_spaces"
    base_dir.mkdir(parents=True, exist_ok=True)

    workspace_dir = base_dir / session_id
    workspace_dir.mkdir(exist_ok=True)

    return workspace_dir.resolve()


def _log_workspace_resolution(source: str, path: pathlib.Path) -> None:
    """Log workspace resolution decision for debugging."""
    import logging
    import sys
    logger = logging.getLogger("gateway.workspace")
    # Always log workspace resolution for security auditing
    msg = f"✓ Workspace resolved via [{source}]: {path}"
    logger.info(msg)
    # Also print to stderr to ensure visibility
    print(msg, file=sys.stderr, flush=True)


def _request_workspace_root(body: Json) -> pathlib.Path:
    """Extract workspace root from request body.

    SECURITY: This function must NEVER return the Gateway server's working directory.
    All workspace paths MUST come from the client OR use an isolated anonymous space.

    Priority chain:
    1. Explicit body field (workspace_root or gateway_workspace)
    2. Auto-detected downstream project dir from session metadata
    3. Explicit env var (GATEWAY_WORKSPACE_ROOT) - for testing only
    4. Anonymous isolated space - per session/request temporary directory

    Returns a safe workspace path - never fails.
    """
    custom_root = body.get("workspace_root") or body.get("gateway_workspace")
    if custom_root:
        path = pathlib.Path(custom_root).expanduser().resolve()
        _log_workspace_resolution("explicit_body", path)
        return path
    # Auto-detect from Claude Code session metadata (Worktree / Primary working directory)
    detected = _extract_client_project_dir(body)
    if detected is not None:
        _log_workspace_resolution("session_metadata", detected)
        return detected
    # Only allow explicit env var for testing - not cwd
    env_root = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    if env_root:
        path = pathlib.Path(env_root).expanduser().resolve()
        _log_workspace_resolution("env_var", path)
        return path

    configured_root = str(_gateway_config().get("workspace_root") or "").strip()
    if configured_root:
        path = pathlib.Path(configured_root).expanduser().resolve(strict=False)
        _log_workspace_resolution("configured_root", path)
        return path

    # SECURITY: Create isolated anonymous space for this session
    # This allows users to chat even without providing workspace
    anonymous_space = _create_anonymous_workspace(body)
    _log_workspace_resolution("anonymous_space", anonymous_space)
    return anonymous_space


@contextmanager
def _workspace_scope(root: pathlib.Path):
    """Context manager that temporarily changes the workspace root.

    SECURITY: root is always a valid path (client-provided or anonymous space).
    """
    from . import gateway_builtin_tools as _bt

    # Ensure the path is absolute and resolved
    resolved_root = pathlib.Path(root).resolve()

    _logger.debug("_workspace_scope: setting workspace to %s", resolved_root)

    token = _bt._WORKSPACE_ROOT_OVERRIDE.set(resolved_root)
    try:
        yield resolved_root
    finally:
        _bt._WORKSPACE_ROOT_OVERRIDE.reset(token)
        _logger.debug("_workspace_scope: reset workspace")

def _normalize_tool_name(name: str) -> str:
    """Normalize tool name to match builtin registry."""
    if not name:
        return name
    # Direct match
    if name in BUILTIN_TOOLS:
        return name
    # Case-insensitive match
    lower = name.lower()
    for key in BUILTIN_TOOLS:
        if key.lower() == lower:
            return key
    # Strip common prefixes
    for prefix in ("gateway_", "gw_", "tool_"):
        if lower.startswith(prefix):
            stripped = name[len(prefix):]
            if stripped in BUILTIN_TOOLS:
                return stripped
    return name


def _normalize_tool_args(name: str, arguments: Json) -> Json:
    """Normalize tool arguments to match expected schema."""
    if not isinstance(arguments, dict):
        return arguments
    tool = BUILTIN_TOOLS.get(name)
    if not tool or not tool.parameters:
        return arguments
    props = tool.parameters.get("properties", {})
    if not props:
        return arguments
    # Map common aliases - only apply if the target property exists in schema
    alias_map = {
        "cmd": "command",
        "file": "path",
        "file_path": "path",
        "filepath": "path",
        "dir": "path",
        "directory": "path",
        "folder": "path",
        "input": "content",
        "text": "content",
        "data": "content",
        "value": "expression",
        "expr": "expression",
    }
    result = {}
    for key, value in arguments.items():
        mapped_key = alias_map.get(key, key)
        if mapped_key in props:
            result[mapped_key] = value
        else:
            result[key] = value
    if name == "Bash" and "command" in result and "cmd" in props and "cmd" not in result:
        result["cmd"] = result["command"]
    return result


def _normalize_tool_call(call: ToolCall) -> ToolCall:
    name = _normalize_tool_name(call.name)
    arguments = _normalize_tool_args(name, call.arguments)
    if name == "Git" and not arguments.get("action"):
        compact = re.sub(r"[^a-z0-9]+", "_", call.name.lower()).strip("_")
        for action in ("status", "diff", "log", "show", "branch"):
            if action in compact:
                arguments["action"] = action
                break
    return ToolCall(
        call_id=call.call_id,
        name=name,
        arguments=arguments,
        raw=call.raw,
    )


def _direct_tool_call_from_body(body: Json) -> ToolCall:
    raw: Json = body
    call_id = str(body.get("id") or body.get("call_id") or body.get("tool_call_id") or f"call_{uuid.uuid4().hex}")
    name: Any = body.get("name") or body.get("tool") or body.get("tool_name") or body.get("function_name") or body.get("recipient_name")
    if isinstance(name, str) and "." in name:
        name = name.rsplit(".", 1)[-1]
    raw_args: Any = body.get("arguments")
    if raw_args is None:
        raw_args = body.get("args")
    if raw_args is None:
        raw_args = body.get("input")
    if raw_args is None:
        raw_args = body.get("parameters")

    function = body.get("function")
    if isinstance(function, dict):
        name = name or function.get("name")
        raw_args = function.get("arguments") if raw_args is None else raw_args
        raw = function

    tool_call = body.get("tool_call")
    if isinstance(tool_call, dict):
        return _direct_tool_call_from_body(tool_call)

    if body.get("type") == "function" and isinstance(body.get("function"), dict):
        function = body["function"]
        name = function.get("name")
        raw_args = function.get("arguments")
        raw = body

    if body.get("type") == "tool_use":
        name = name or body.get("name")
        raw_args = body.get("input") if raw_args is None else raw_args
        raw = body

    if not name:
        raise ToolExecutionError("missing tool/function name", failure_type="invalid_input")
    return ToolCall(
        call_id=call_id,
        name=str(name),
        arguments=_parse_json_arguments(raw_args, allow_text=True),
        raw=raw,
    )


def _direct_tool_calls_from_body(body: Json) -> list[ToolCall]:
    if isinstance(body.get("tool_uses"), list):
        return [
            ToolCall(
                call_id=str(body.get("call_id") or body.get("id") or f"call_{uuid.uuid4().hex}"),
                name="multi_tool_use.parallel",
                arguments={"tool_uses": body.get("tool_uses"), "max_workers": body.get("max_workers")},
                raw=body,
            )
        ]
    raw_calls = body.get("tool_calls") or body.get("calls") or body.get("function_calls")
    if isinstance(raw_calls, list):
        return [_direct_tool_call_from_body(call) for call in raw_calls if isinstance(call, dict)]
    return [_direct_tool_call_from_body(body)]


def _response_tool_call_from_item(item: Json) -> ToolCall | None:
    item_type = item.get("type")
    if item_type not in {"function_call", "tool_call", "custom_tool_call"}:
        return None
    name = item.get("name")
    if not name:
        return None
    raw_args = item.get("arguments")
    allow_text = item_type == "custom_tool_call"
    custom_string_input = False
    if raw_args is None and item_type == "custom_tool_call":
        raw_args = item.get("input")
        custom_string_input = raw_args is not None and not isinstance(raw_args, dict)
    if raw_args is None:
        raw_args = item.get("input") if isinstance(item.get("input"), dict) else item.get("action")
    arguments = {"input": raw_args} if custom_string_input else _parse_json_arguments(raw_args, allow_text=allow_text)
    return ToolCall(
        call_id=str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}"),
        name=str(name),
        arguments=arguments,
        raw=item,
    )


def _strip_xmlish_closing_tags(value: str) -> str:
    return re.sub(r"</(?:parameter|function|tool|invoke)>", "", value, flags=re.I).strip()



def _parse_parameter_blocks(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    # Stop at parameter, function, or tool_call tags
    parameter_re = re.compile(r"<parameter=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<parameter=[A-Za-z0-9_.:-]+>|<function=[A-Za-z0-9_.:-]+>|<tool_call>|\Z)", re.S)
    for param in parameter_re.finditer(text or ""):
        key = param.group(1).strip()
        value = _strip_xmlish_closing_tags(param.group(2))
        if key:
            blocks.append((key, value))
    return blocks


def _inline_text_before_parameter_blocks(text: str) -> str:
    raw = re.sub(r"<parameter=[A-Za-z0-9_.:-]+>.*", "", text or "", flags=re.S).strip()
    # Strip trailing junk after first blank line or markdown header
    raw = re.sub(r"\n\n.*", "", raw, flags=re.S).strip()
    # Strip trailing markdown headers
    raw = re.sub(r"\s*---.*", "", raw, flags=re.S).strip()
    return raw


def _repair_shell_command_spacing(command: str) -> str:
    """Repair common spacing loss from weak text-tool markup."""
    cmd = str(command or "").strip()
    if not cmd:
        return cmd
    cmd = re.sub(r"^(find)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"^(grep)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"^(ls|cat|head|tail|wc|python3?|bash|sh)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"\b(ls\s+-[A-Za-z]+)(/[^\s]+)", r"\1 \2", cmd)
    cmd = re.sub(r"\s-type\s*([fdl])(?=\s|-|$)", r" -type \1", cmd)
    cmd = re.sub(r"(-type\s+[fdl])-name", r"\1 -name", cmd)
    cmd = re.sub(r"\s-name(')", r" -name \1", cmd)
    cmd = re.sub(r'\s-name(")', r' -name \1', cmd)
    cmd = re.sub(r"(?<!\s)-name(')", r" -name \1", cmd)
    cmd = re.sub(r'(?<!\s)-name(")', r' -name \1', cmd)
    cmd = re.sub(r'\s-name([^\s\'"]+)', r" -name \1", cmd)
    cmd = re.sub(r"\b(head|tail)-([0-9]+)\b", r"\1 -\2", cmd)
    cmd = re.sub(r"\b(wc\s+-[A-Za-z]+)\{\}", r"\1 {}", cmd)
    cmd = re.sub(r"\s-l\{\}", r" -l {}", cmd)
    cmd = re.sub(r"([^\s])\{\}(?=\s|$)", r"\1 {}", cmd)
    cmd = re.sub(r"\s+", " ", cmd).strip()
    return cmd


def _parse_json_tool_calls_from_text(text: str) -> list[ToolCall]:
    """Parse JSON-formatted tool calls from text responses.

    Supports: {"name": "X", "arguments": {...}}, {"tool": "X", "args": {...}},
    {"function": {"name": "X", "arguments": {...}}}, arrays thereof,
    and JSON inside ```json / ```functioncall code blocks.
    """
    if not text:
        return []
    calls: list[ToolCall] = []

    def _try(obj: dict) -> ToolCall | None:
        if not isinstance(obj, dict):
            return None
        name = obj.get("name") or obj.get("tool") or obj.get("function_name")
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or obj.get("input")
        if not name and isinstance(obj.get("function"), dict):
            fn = obj["function"]
            name = fn.get("name")
            args = args or fn.get("arguments")
        if not name and isinstance(obj.get("tool_calls"), list):
            for tc in obj["tool_calls"]:
                if isinstance(tc, dict):
                    fn = tc.get("function") or tc
                    if fn.get("name"):
                        name = fn["name"]
                        args = args or fn.get("arguments")
                        break
        if not name:
            return None
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"raw": args}
        if not isinstance(args, dict):
            args = {"value": args}
        n = _normalize_tool_name(str(name))
        return ToolCall(
            call_id=f"textjson_{uuid.uuid4().hex}", name=n,
            arguments=_normalize_tool_args(n, args),
            raw={"gateway_text_tool_call_fallback": True, "format": "json", "text": text[:2000]},
        )

    # JSON in code blocks first
    code_block_re = re.compile(r"```(?:json|functioncall|tool_call|toolcall)?\s*\n(.*?)```", re.S | re.I)
    for m in code_block_re.finditer(text):
        block = m.group(1).strip()
        try:
            parsed = json.loads(block)
            items = parsed if isinstance(parsed, list) else [parsed]
            for item in items:
                c = _try(item)
                if c:
                    calls.append(c)
            if calls:
                return calls
        except json.JSONDecodeError:
            for line in block.splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "//")):
                    continue
                try:
                    c = _try(json.loads(line))
                    if c:
                        calls.append(c)
                except json.JSONDecodeError:
                    pass
    if calls:
        return calls

    # Raw JSON objects in text
    if '"name"' in text or '"tool"' in text:
        for m in re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text):
            try:
                parsed = json.loads(m.group(0))
                if isinstance(parsed, dict) and (parsed.get("name") or parsed.get("tool") or parsed.get("function")):
                    c = _try(parsed)
                    if c:
                        calls.append(c)
            except json.JSONDecodeError:
                pass
    return calls


def _parse_markdown_tool_calls(text: str) -> list[ToolCall]:
    """Parse ```tool ...``` blocks and Python-style ToolName(key="value") calls."""
    if not text:
        return []
    calls: list[ToolCall] = []

    # ```tool ... ``` blocks
    for m in re.finditer(r"```(?:tool|tools|tool_call|toolcall)\s*\n(.*?)```", text, re.S | re.I):
        for line in m.group(1).strip().splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "//")):
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            name = parts[0]
            args: Json = {}
            if len(parts) > 1:
                for kv in re.finditer(r'(\w+)=(?:"([^"]*)"|\'([^\']*)\'|(\S+))', parts[1]):
                    args[kv.group(1)] = kv.group(2) or kv.group(3) or kv.group(4) or ""
                if not args:
                    args = {"input": parts[1]}
            n = _normalize_tool_name(name)
            calls.append(ToolCall(
                call_id=f"textmd_{uuid.uuid4().hex}", name=n,
                arguments=_normalize_tool_args(n, args),
                raw={"gateway_text_tool_call_fallback": True, "format": "markdown", "text": text[:2000]},
            ))
    if calls:
        return calls

    # Python-style: ToolName(key="value")
    for m in re.finditer(r'([A-Z][A-Za-z0-9_]*)\s*\(([^)]*)\)', text):
        name = m.group(1)
        n = _normalize_tool_name(name)
        if n not in BUILTIN_TOOLS and name not in BUILTIN_TOOLS:
            continue
        args: Json = {}
        for kv in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))', m.group(2)):
            args[kv.group(1)] = kv.group(2) or kv.group(3) or kv.group(4) or ""
        calls.append(ToolCall(
            call_id=f"textpy_{uuid.uuid4().hex}", name=n,
            arguments=_normalize_tool_args(n, args),
            raw={"gateway_text_tool_call_fallback": True, "format": "python_call", "text": text[:2000]},
        ))
    return calls


def _parse_xml_tool_calls(text: str) -> list[ToolCall]:
    """Parse XML-format tool calls with function/parameter tags."""
    if "<function=" not in text and "<parameter=" not in text:
        return []
    calls: list[ToolCall] = []
    function_re = re.compile(r"<function=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<function=[A-Za-z0-9_.:-]+>|\Z)", re.S)

    def append_call(name: str, args: Json, raw_text: str) -> None:
        if not name:
            return
        n = _normalize_tool_name(name)
        calls.append(
            ToolCall(
                call_id=f"textcall_{uuid.uuid4().hex}",
                name=n,
                arguments=_normalize_tool_args(n, args),
                raw={"gateway_text_tool_call_fallback": True, "format": "xml", "text": raw_text[:2000]},
            )
        )

    matched_function = False
    for match in function_re.finditer(text):
        matched_function = True
        name = match.group(1).strip()
        body = match.group(2).strip()
        if body.startswith("{"):
            try:
                parsed = json.loads(_strip_xmlish_closing_tags(body))
                if isinstance(parsed, dict):
                    append_call(name, parsed, match.group(0))
                    continue
            except Exception:
                pass
        blocks = _parse_parameter_blocks(body)
        if name in {"Bash", "bash", "exec_command", "shell", "shell_command"}:
            inline_command = _inline_text_before_parameter_blocks(body)
            if inline_command:
                append_call(name, {"command": _repair_shell_command_spacing(inline_command)}, match.group(0))
            current: Json | None = None
            for key, value in blocks:
                if key in {"command", "cmd", "shell"}:
                    if current and current.get("command"):
                        append_call(name, current, match.group(0))
                    current = {"command": _repair_shell_command_spacing(value)}
                elif current is not None:
                    current[key] = value
            if current and current.get("command"):
                append_call(name, current, match.group(0))
            continue
        args: Json = {}
        for key, value in blocks:
            args[key] = value
        if not args:
            inline_value = _inline_text_before_parameter_blocks(body)
            normalized_name = _normalize_tool_name(name)
            if inline_value and normalized_name in {"Read", "FileInfo", "LS", "Tree", "Glob", "PythonSymbols", "JsonQuery"}:
                if normalized_name == "Glob":
                    args["pattern"] = inline_value
                else:
                    args["path"] = inline_value
        append_call(name, args, match.group(0))

    if not matched_function:
        current: Json | None = None
        for key, value in _parse_parameter_blocks(text):
            if key in {"command", "cmd", "shell"}:
                if current and current.get("command"):
                    append_call("Bash", current, text)
                current = {"command": _repair_shell_command_spacing(value)}
            elif current is not None:
                current[key] = value
        if current and current.get("command"):
            append_call("Bash", current, text)
    return calls


def _parse_text_tool_calls(text: str) -> list[ToolCall]:
    """Parse text-based tool-call fallbacks from weak upstream models.

    Supports multiple formats (tried in order):
    1. XML: function/parameter tags
    2. JSON: {"name": "ToolName", "arguments": {...}}
    3. Markdown: ```tool blocks or Python-style calls
    4. Bare parameter blocks
    """
    if not text:
        return []

    # Try XML format first
    calls = _parse_xml_tool_calls(text)
    if calls:
        return calls

    # Try JSON format
    calls = _parse_json_tool_calls_from_text(text)
    if calls:
        return calls

    # Try markdown/python-style format
    calls = _parse_markdown_tool_calls(text)
    if calls:
        return calls

    # Fallback: bare parameter blocks
    if "<parameter=" in text:
        current: Json | None = None
        for key, value in _parse_parameter_blocks(text):
            if key in {"command", "cmd", "shell"}:
                if current and current.get("command"):
                    calls.append(ToolCall(
                        call_id=f"textcall_{uuid.uuid4().hex}",
                        name="Bash",
                        arguments=_normalize_tool_args("Bash", current),
                        raw={"gateway_text_tool_call_fallback": True, "format": "bare_param", "text": text[:2000]},
                    ))
                current = {"command": _repair_shell_command_spacing(value)}
            elif current is not None:
                current[key] = value
        if current and current.get("command"):
            calls.append(ToolCall(
                call_id=f"textcall_{uuid.uuid4().hex}",
                name="Bash",
                arguments=_normalize_tool_args("Bash", current),
                raw={"gateway_text_tool_call_fallback": True, "format": "bare_param", "text": text[:2000]},
            ))

    return calls


def _extract_tool_calls(path: str, response: Json) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if path == "/v1/chat/completions":
        for choice in response.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or {}
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                if not isinstance(fn, dict) or not fn.get("name"):
                    continue
                calls.append(
                    ToolCall(
                        call_id=str(call.get("id") or f"call_{uuid.uuid4().hex}"),
                        name=str(fn["name"]),
                        arguments=_parse_json_arguments(fn.get("arguments")),
                        raw=call,
                    )
                )
        return calls

    if path == "/v1/responses":
        for item in response.get("output") or []:
            if not isinstance(item, dict):
                continue
            call = _response_tool_call_from_item(item)
            if call:
                calls.append(call)
            for block in item.get("content") or []:
                if isinstance(block, dict):
                    call = _response_tool_call_from_item(block)
                    if call:
                        calls.append(call)
        return calls

    if path == "/v1/messages":
        for block in response.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name"):
                calls.append(
                    ToolCall(
                        call_id=str(block.get("id") or f"toolu_{uuid.uuid4().hex}"),
                        name=str(block["name"]),
                        arguments=_parse_json_arguments(block.get("input") or {}),
                        raw=block,
                    )
                )
        return calls

    return calls


def _text_tool_call_fallback_enabled() -> bool:
    gateway = _gateway_config()
    upstream = _upstream_config()
    tools_enabled = str(upstream.get("tools_enabled", "auto") or "auto").strip().lower()
    if tools_enabled in {"off", "disabled", "false", "0", "none"}:
        return False
    capabilities = upstream.get("capabilities") if isinstance(upstream.get("capabilities"), dict) else {}
    native_capable = bool(capabilities.get("supports_tools", False)) and bool(capabilities.get("supports_function_calls", False))
    if tools_enabled in {"adapter", "text_only", "prompt"}:
        return True
    if tools_enabled == "auto" and not native_capable:
        return True
    return bool(gateway.get("text_tool_call_fallback_enabled", True))


def _extract_text_tool_calls(path: str, response: Json) -> list[ToolCall]:
    if not _text_tool_call_fallback_enabled():
        return []
    return _parse_text_tool_calls(_response_text(path, response))


def _convert_text_calls_to_downstream_response(
    path: str,
    text_calls: list["ToolCall"],
    original_response: Json,
    upstream_protocol: str,
) -> Json:
    """Convert parsed text-based tool calls into native downstream protocol format.

    Instead of executing tools locally, this creates a proper tool_use / function_call
    response that the downstream client (Claude Code / Codex) will execute.
    """
    if not text_calls:
        return original_response

    # Build Anthropic Messages format (tool_use blocks)
    if "/messages" in path:
        content_parts: list[dict] = []
        for call in text_calls:
            content_parts.append({
                "type": "tool_use",
                "id": call.call_id,
                "name": call.name,
                "input": call.arguments,
            })
        return {
            "id": f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": original_response.get("model") or "",
            "content": content_parts,
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": original_response.get("usage") or {"input_tokens": 0, "output_tokens": 0},
        }

    # Build OpenAI Chat format (tool_calls)
    if "/chat/completions" in path:
        tool_calls = []
        for call in text_calls:
            tool_calls.append({
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            })
        choices = original_response.get("choices") or [{}]
        choice = dict(choices[0]) if choices else {}
        message = dict(choice.get("message") or {})
        # Do not leak text-adapter markup to clients as visible assistant text.
        if isinstance(message.get("content"), str) and re.search(r"<function=|<tool_call>|```tool_code|\"tool_calls\"", message["content"]):
            message["content"] = None
        message["tool_calls"] = tool_calls
        choice["message"] = message
        choice["finish_reason"] = "tool_calls"
        return {
            "id": original_response.get("id") or f"chatcmpl_gateway_{uuid.uuid4().hex}",
            "object": "chat.completion",
            "model": original_response.get("model") or "",
            "choices": [choice],
            "usage": original_response.get("usage") or {},
        }

    # Build OpenAI Responses format
    if "/responses" in path:
        output_items = []
        for call in text_calls:
            output_items.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            })
        return {
            "id": original_response.get("id") or f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "model": original_response.get("model") or "",
            "output": output_items,
            "usage": original_response.get("usage") or {},
        }

    return original_response


def _detect_intent_tool_calls(path: str, response: Json, body: Json) -> list[ToolCall]:
    """Detect tool usage intent from model response text for weak models.

    When a model can't generate proper tool calls but expresses intent to use
    tools (e.g., "I'll read the file", "Let me check the directory"), this
    function detects that intent and returns appropriate tool calls.

    This is a fallback for models that can't follow text-based tool call
    instructions.
    """
    _logger.debug("_detect_intent_tool_calls called")
    # Check if intent detection is enabled
    from .gateway_config import _gateway_config, _upstream_config
    gateway_cfg = _gateway_config()
    if not gateway_cfg.get("intent_detection_enabled", True):
        return []

    # Only enable for weak upstream models that can't generate tool calls.
    # Capabilities are stored under upstream.capabilities in the modern config;
    # keep the legacy top-level fallback for older local config files.
    upstream_cfg = _upstream_config()
    capabilities = upstream_cfg.get("capabilities") if isinstance(upstream_cfg.get("capabilities"), dict) else {}
    native_capable = (
        bool(capabilities.get("supports_tools", upstream_cfg.get("supports_tools", False)))
        and bool(capabilities.get("supports_function_calls", upstream_cfg.get("supports_function_calls", False)))
    )
    tools_enabled = str(upstream_cfg.get("tools_enabled", "auto") or "auto").strip().lower()
    if tools_enabled in {"off", "disabled", "false", "0", "none"}:
        return []
    if native_capable and tools_enabled not in {"off", "disabled", "false", "0", "none", "text_only", "adapter"}:
        return []  # Native tools supported, no need for intent detection

    text = _response_text(path, response)
    if not text:
        _logger.debug("Intent detection: no text in response")
        return []

    # Allow shorter responses for bare commands like "ls -la"
    text = text.strip()
    _logger.debug("Intent detection: response text = '%s'", text[:100])
    if len(text) < 3:
        _logger.debug("Intent detection: text too short (%d chars)", len(text))
        return []

    # Extract the last user message/input to understand context.  This supports
    # both Claude Code Anthropic Messages and Codex Responses payloads.
    _last_user_text(path, body)

    calls: list[ToolCall] = []

    # Pattern 0: Bare shell commands (highest priority)
    # Match standalone commands like "ls -la", "tree", "pwd", "find .", etc.
    # Strip trailing punctuation like . , ! ?
    clean_text = text.strip().rstrip('.,!?;:')
    bare_cmd_pattern = r"^(ls|tree|pwd|find|grep|cat|head|tail|wc|du|df)(\s+.*)?$"
    bare_match = re.match(bare_cmd_pattern, clean_text, re.IGNORECASE)
    _logger.debug("Bare command pattern match on '%s': %s", clean_text, bare_match)
    if bare_match:
        cmd = clean_text
        _logger.debug("Detected bare command: '%s'", cmd)
        calls.append(ToolCall(
            call_id=f"intent_{uuid.uuid4().hex}",
            name="Bash",
            arguments={"command": cmd, "description": f"Execute command: {cmd}"},
            raw={"gateway_intent_detection": True, "bare_command": True, "text": text[:500]},
        ))
        _logger.debug("Returning %d intent-detected tool calls", len(calls))
        return calls

    # Pattern 1: Model says it will read a file but doesn't output tool tags
    # Look for file paths mentioned in the response
    file_path_patterns = [
        # "read the file src/main.py" or "read src/main.py"
        r"(?:read|check|examine|look at|open|view)\s+(?:the\s+)?(?:file\s+)?(?:`([^`]+)`|(\S+\.\w+))",
        # "file at src/main.py" or "file: src/main.py"
        r"file\s+(?:at|:)\s*(?:`([^`]+)`|(\S+\.\w+))",
        # Backtick-quoted paths
        r"`([^`]+\.\w+)`",
    ]

    for pattern in file_path_patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            file_path = match.group(1) or match.group(2) or match.group(3)
            if file_path and not file_path.startswith(("http://", "https://", "ftp://")):
                # Avoid duplicates
                if not any(c.arguments.get("path") == file_path for c in calls):
                    calls.append(ToolCall(
                        call_id=f"intent_{uuid.uuid4().hex}",
                        name="Read",
                        arguments={"path": file_path},
                        raw={"gateway_intent_detection": True, "text": text[:500]},
                    ))
                    break  # Only one Read per response
        if calls:
            break

    # Pattern 2: Model says it will list/check directory
    if not calls:
        dir_patterns = [
            r"(?:list|check|examine|look at|explore)\s+(?:the\s+)?(?:directory|folder|contents)\s+(?:of\s+)?(?:`([^`]+)`|(\S+))",
            r"(?:ls|dir)\s+(?:`([^`]+)`|(\S+))",
        ]
        for pattern in dir_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                dir_path = match.group(1) or match.group(2) or match.group(3) or "."
                calls.append(ToolCall(
                    call_id=f"intent_{uuid.uuid4().hex}",
                    name="LS",
                    arguments={"path": dir_path},
                    raw={"gateway_intent_detection": True, "text": text[:500]},
                ))
                break
            if calls:
                break

    # Pattern 3: Model says it will run a command
    if not calls:
        cmd_patterns = [
            r"(?:run|execute|use)\s+(?:the\s+)?(?:command|shell)\s*:?\s*`([^`]+)`",
            r"(?:run|execute)\s*:?\s*`([^`]+)`",
        ]
        for pattern in cmd_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                cmd = match.group(1)
                if cmd:
                    calls.append(ToolCall(
                        call_id=f"intent_{uuid.uuid4().hex}",
                        name="Bash",
                        arguments={"command": cmd},
                        raw={"gateway_intent_detection": True, "text": text[:500]},
                    ))
                    break
            if calls:
                break

    # Pattern 4: Model says it will search/glob for files
    if not calls:
        glob_patterns = [
            r"(?:search|find|look for|glob)\s+(?:for\s+)?(?:files?\s+)?(?:matching\s+)?(?:`([^`]+)`|(\S+\.\w+))",
        ]
        for pattern in glob_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                glob_pattern = match.group(1) or match.group(2)
                if glob_pattern:
                    calls.append(ToolCall(
                        call_id=f"intent_{uuid.uuid4().hex}",
                        name="Glob",
                        arguments={"pattern": glob_pattern},
                        raw={"gateway_intent_detection": True, "text": text[:500]},
                    ))
                    break
            if calls:
                break

    return calls


def _assistant_message_from_chat_response(response: Json) -> Json:
    choices = response.get("choices") or []
    if choices and isinstance(choices[0], dict) and isinstance(choices[0].get("message"), dict):
        return dict(choices[0]["message"])
    return {"role": "assistant", "content": None}


def _append_tool_results(path: str, body: Json, response: Json, results: list[ToolResult]) -> Json:
    updated = dict(body)
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        messages.append(_assistant_message_from_chat_response(response))
        for result in results:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "content": result.content,
                }
            )
        updated["messages"] = messages
        return updated

    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        custom_call_ids: set[str] = set()
        for item in response.get("output") or []:
            if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                input_items.append(item)
                if item.get("type") == "custom_tool_call" and item.get("call_id"):
                    custom_call_ids.add(str(item["call_id"]))
            if isinstance(item, dict):
                for block in item.get("content") or []:
                    if isinstance(block, dict) and block.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                        input_items.append(block)
                        if block.get("type") == "custom_tool_call" and block.get("call_id"):
                            custom_call_ids.add(str(block["call_id"]))
        for result in results:
            output_type = "custom_tool_call_output" if result.call_id in custom_call_ids else "function_call_output"
            output_item = {
                "type": output_type,
                "call_id": result.call_id,
                "output": result.content,
            }
            if output_type == "custom_tool_call_output":
                output_item["name"] = result.name
            input_items.append(output_item)
        updated["input"] = input_items
        return updated

    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        content = response.get("content") or []
        messages.append({"role": "assistant", "content": content})
        result_blocks = [
            {
                "type": "tool_result",
                "tool_use_id": result.call_id,
                "content": result.content,
                "is_error": not result.success,
            }
            for result in results
        ]
        messages.append({"role": "user", "content": result_blocks})
        updated["messages"] = messages
        return updated

    return updated


def _append_text_tool_results(path: str, body: Json, response: Json, calls: list[ToolCall], results: list[ToolResult]) -> Json:
    updated = dict(body)
    tool_report = {
        "gateway_local_tool_fallback": True,
        "reason": "upstream returned text-only <function=...> tool call markup without native protocol tool_calls/tool_use",
        "calls": [
            {
                "id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
                "success": result.success,
                "failure_type": result.failure_type,
                "content": result.content,
            }
            for call, result in zip(calls, results)
        ],
    }
    report_text = (
        "Gateway executed Gateway-owned or explicitly opted-in text-based tool calls. Real results below.\n"
        "Continue your analysis. If you need MORE tools, output them as:\n"
        "<function=ToolName><parameter=param>value</parameter></function>\n\n"
        + json.dumps(tool_report, ensure_ascii=False, indent=2)
    )
    if path == "/v1/chat/completions":
        messages = list(updated.get("messages") or [])
        messages.append(_assistant_message_from_chat_response(response))
        messages.append({"role": "user", "content": report_text})
        updated["messages"] = messages
        return updated
    if path == "/v1/messages":
        messages = list(updated.get("messages") or [])
        text = _response_text(path, response)
        if text:
            messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": report_text})
        updated["messages"] = messages
        return updated
    if path == "/v1/responses":
        existing = updated.get("input")
        if isinstance(existing, list):
            input_items = list(existing)
        elif existing is None:
            input_items = []
        else:
            input_items = [{"role": "user", "content": existing}]
        input_items.append({"role": "assistant", "content": _response_text(path, response)})
        input_items.append({"role": "user", "content": report_text})
        updated["input"] = input_items
        return updated
    return updated


def _extract_mentioned_paths(text: str) -> list[str]:
    candidates = re.findall(
        r"@?("
        r"/[^\s<>'\"`|]+"
        r"|(?:~|\.|\.\.)/[^\s<>'\"`|]+"
        r"|(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.@%+=:,/-]+"
        r"|[A-Za-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|json|jsonl|toml|yaml|yml|md|txt|sh|bash|zsh|env|ini|cfg|conf|html|css|sql|go|rs|java|kt|swift|c|cc|cpp|h|hpp)"
        r")",
        text,
    )
    out: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().strip(".,;:，。；：）)]}\"'")
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def _should_build_local_planner_context(path: str, body: Json) -> bool:
    gateway = _gateway_config()
    if not gateway.get("local_planner_enabled", True):
        return False
    if path not in SUPPORTED_PATHS:
        return False
    text = _last_user_text(path, body)
    if not text:
        return False
    lowered = text.lower()
    read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开"))
    has_path = bool(_extract_mentioned_paths(text))
    if read_intent and has_path:
        return True
    analyze_intent = any(token in lowered for token in ("分析代码", "分析项目", "analyze code", "analyze project", "code review", "代码审查", "代码分析", "项目分析", "梳理代码", "梳理架构"))
    return analyze_intent and has_path


def _extract_value_after_marker_request(text: str) -> str:
    """Return an explicit marker from "answer only the value after <marker>" prompts."""
    patterns = (
        r"(?:value|content|text)\s+after\s+[`'\"“”]?([A-Za-z0-9_.:-]+)",
        r"after\s+[`'\"“”]?([A-Za-z0-9_.:-]+)",
        r"[`'\"“”]([^`'\"“”\s]{3,})[`'\"“”]\s*(?:之后|后的值|后面的值)",
        r"([A-Za-z0-9_.:-]{3,})\s*(?:之后|后的值|后面的值)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            marker = match.group(1).strip().strip(".,;:，。；：）)]}\"'")
            if marker:
                return marker
    return ""


def _direct_local_file_read_response(path: str, body: Json) -> Json | None:
    """Satisfy narrow deterministic local-file read extraction prompts locally.

    Claude Code smoke tests and weak upstreams can ask to read a local file and
    output only the value after a marker.  If the active upstream does not emit a
    tool call, sending the prompt upstream can produce "I will read it" instead
    of the file value.  For explicit "value after <marker>" requests, the
    gateway can safely execute the read itself and return the exact value.
    """
    if not _gateway_executes_user_side_tools_locally():
        return None
    user_text = _last_user_text(path, body)
    if not user_text:
        return None
    lowered = user_text.lower()
    read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开"))
    marker = _extract_value_after_marker_request(user_text)
    paths = _extract_mentioned_paths(user_text)
    if not (read_intent and marker and paths):
        return None
    for raw_path in reversed(paths):
        try:
            resolved = _resolve_workspace_path(raw_path)
            if not resolved.is_file():
                continue
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        index = text.find(marker)
        if index < 0:
            continue
        value = text[index + len(marker):].lstrip(" \t:=：-")
        value = value.splitlines()[0].strip()
        if value and re.search(r"[A-Za-z0-9\u4e00-\u9fff]", value):
            return _fallback_response(path, value, status_note="gateway_local_file_read")
    return None


def _extract_explicit_skill_request(text: str) -> tuple[str, str]:
    """Return (action, skill_name) for explicit local Skill requests.

    This covers Claude Code/Codex prompts such as "list skills" or
    "read skill tdd" when the active upstream cannot reliably emit a structured
    Skill tool call.  The actual work still goes through the real Gateway Skill
    executor, so this is a deterministic local runtime shortcut, not a fake
    protocol-level tool result.
    """
    if not text:
        return "", ""
    lowered = text.lower()
    if not any(token in lowered for token in ("skill", "skills", "技能")):
        return "", ""
    if any(token in lowered for token in ("list skills", "show skills", "available skills", "列出", "有哪些", "技能列表", "所有技能", "可用技能")):
        return "list", ""
    read_patterns = (
        r"(?:read|show|open|view)\s+(?:the\s+)?skill\s+[`'\"“”]?([A-Za-z0-9_.-]+)",
        r"skill\s+[`'\"“”]?([A-Za-z0-9_.-]+)[`'\"“”]?\s*(?:内容|说明|指南|怎么用|是什么)",
        r"(?:读取|查看|打开|展示)\s*(?:skill|技能)\s*[`'\"“”]?([A-Za-z0-9_.-]+)",
    )
    for pattern in read_patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            name = match.group(1).strip().strip("`'\"“”.,;:，。；：")
            if name:
                return "read", name
    return "", ""


def _direct_local_skill_response(path: str, body: Json) -> Json | None:
    """Satisfy explicit local Skill list/read prompts for weak upstreams."""
    if not _gateway_executes_user_side_tools_locally():
        return None
    action, name = _extract_explicit_skill_request(_last_user_text(path, body))
    if not action:
        return None
    arguments = {"name": name} if action == "read" else {}
    result = _execute_tool_call(ToolCall(f"direct_skill_{uuid.uuid4().hex}", "Skill", arguments, {}), provider="direct_intent")
    if not result.success:
        return _fallback_response(path, result.content, status_note=f"gateway_local_skill_{result.failure_type or 'error'}")
    return _fallback_response(path, result.content, status_note=f"gateway_local_skill_{action}")


def _extract_explicit_shell_command_request(text: str) -> str:
    """Return a command only when the user explicitly asks Gateway to run one."""
    if not text:
        return ""
    lowered = text.lower()
    if not any(token in lowered for token in ("bash", "shell", "command", "run", "execute", "terminal", "命令", "运行", "执行")):
        return ""
    patterns = (
        r"(?:bash|shell|command|run|execute|terminal)[^`'\"]{0,120}`([^`\n]+)`",
        r"`([^`\n]+)`[^`]{0,120}(?:bash|shell|command|run|execute|terminal)",
        r"(?:命令|运行|执行)[^`'\"“”]{0,120}[`'\"“”]([^`'\"“”\n]+)[`'\"“”]",
        r"[`'\"“”]([^`'\"“”\n]+)[`'\"“”][^`'\"“”]{0,120}(?:命令|运行|执行)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I | re.S)
        if match:
            command = match.group(1).strip()
            if command:
                return command
    return ""


def _stdout_from_shell_tool_content(content: str) -> str:
    marker = "stdout:\n"
    if marker not in content:
        return ""
    stdout = content.split(marker, 1)[1]
    if "\nstderr:\n" in stdout:
        stdout = stdout.split("\nstderr:\n", 1)[0]
    return stdout.strip()


def _direct_local_bash_response(path: str, body: Json) -> Json | None:
    """Satisfy narrow explicit Bash/shell prompts locally for weak upstreams.

    This is not fake tool support: it only runs through the same permission-
    gated ``Bash`` runtime used by direct tool calls and text tool orchestration.
    It protects Claude Code/Codex adapter mode when a no-native-tools upstream
    merely says "I will run the command" instead of emitting adapter tags.
    """
    if not _gateway_executes_user_side_tools_locally():
        return None
    user_text = _last_user_text(path, body)
    command = _extract_explicit_shell_command_request(user_text)
    if not command:
        return None
    result = _execute_tool_call(ToolCall(f"direct_bash_{uuid.uuid4().hex}", "Bash", {"command": command}, {}), provider="direct_intent")
    if not result.success:
        return _fallback_response(path, result.content, status_note=f"gateway_local_bash_{result.failure_type or 'error'}")
    lowered = user_text.lower()
    stdout = _stdout_from_shell_tool_content(result.content)
    if stdout and any(token in lowered for token in ("stdout", "output only", "reply only", "answer only", "只输出", "仅输出")):
        return _fallback_response(path, stdout, status_note="gateway_local_bash")
    return _fallback_response(path, result.content, status_note="gateway_local_bash")


def _direct_downstream_tool_request_response(path: str, body: Json) -> Json | None:
    """Surface obvious user-machine tool requests without touching Gateway FS/shell."""
    if _gateway_executes_user_side_tools_locally() or _has_tool_result_in_messages(path, body):
        return None
    user_text = _last_user_text(path, body)
    if not user_text:
        return None
    calls: list[ToolCall] = []
    skill_action, skill_name = _extract_explicit_skill_request(user_text)
    if skill_action:
        arguments = {"name": skill_name} if skill_action == "read" else {}
        calls.append(ToolCall(f"client_required_{uuid.uuid4().hex}", "Skill", arguments, {"gateway_downstream_tool_request": True}))
    command = "" if calls else _extract_explicit_shell_command_request(user_text)
    if command:
        calls.append(ToolCall(f"client_required_{uuid.uuid4().hex}", "Bash", {"command": command}, {"gateway_downstream_tool_request": True}))
    else:
        lowered = user_text.lower()
        paths = _extract_mentioned_paths(user_text)
        read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开"))
        list_intent = any(token in lowered for token in ("current directory", "list files", "list directory", "当前目录", "列出文件", "目录下", "ls "))
        if read_intent and paths:
            # Prefer the path closest to the explicit read request. Claude Code
            # prompts often include stale Worktree/System-reminder paths before
            # the actual user request; asking the client to read all extracted
            # paths could leak or touch the wrong project.
            calls.append(ToolCall(f"client_required_{uuid.uuid4().hex}", "Read", {"path": paths[-1]}, {"gateway_downstream_tool_request": True}))
        elif list_intent:
            target = paths[-1] if paths else "."
            calls.append(ToolCall(f"client_required_{uuid.uuid4().hex}", "LS", {"path": target}, {"gateway_downstream_tool_request": True}))
    if not calls:
        return None
    normalized_calls = [_normalize_tool_call(call) for call in calls]
    return _build_tool_round_response(
        path,
        normalized_calls,
        [],
        {"model": str(body.get("model") or _config_env("UPSTREAM_MODEL", "")), "usage": {"input_tokens": 0, "output_tokens": 0}},
    )


def _weak_upstream_text_tools_active(gateway_mode: str) -> bool:
    """Return True when the gateway must compensate for non-native tool support."""
    if gateway_mode in {"passthrough", "native_passthrough", "proxy"}:
        return False
    upstream = _upstream_config()
    tools_enabled = str(upstream.get("tools_enabled", "auto") or "auto").strip().lower()
    capabilities = upstream.get("capabilities") if isinstance(upstream.get("capabilities"), dict) else {}
    native_capable = bool(capabilities.get("supports_tools", False)) and bool(capabilities.get("supports_function_calls", False))
    if tools_enabled in {"text_only", "adapter", "prompt"}:
        return True
    if tools_enabled == "auto" and not native_capable:
        return True
    return False


USER_SIDE_TOOL_RISKS = {"read_local", "write_local", "execute_code", "gui", "ai_agent"}


def _gateway_executes_user_side_tools_locally() -> bool:
    """Return True only for explicit legacy/local-proxy execution mode.

    The production default for Codex/Claude Code clients is that tools touching
    the user's filesystem, shell, GUI, or local agent runtime execute on the
    downstream client.  Gateway-side execution is kept only as an explicit
    compatibility/local-proxy opt-in.
    """
    gateway = _gateway_config()
    env_value = os.environ.get("GATEWAY_EXECUTE_USER_SIDE_TOOLS")
    if env_value is not None:
        return env_value.strip().lower() in {"1", "true", "yes", "on"}
    if gateway.get("execute_user_side_tools_in_gateway") is True:
        return True
    # Backward-compatible escape hatch for old tests/deployments that
    # intentionally opted out of downstream delegation.
    if gateway.get("delegate_tools_to_downstream") is False:
        return True
    return False


def _declared_tool_names_from_body(body: Json) -> set[str]:
    names: set[str] = set()
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        candidates: list[Any] = [tool.get("name")]
        function = tool.get("function")
        if isinstance(function, dict):
            candidates.append(function.get("name"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                names.add(candidate.strip())
                names.add(_normalize_tool_name(candidate.strip()))
    return {name for name in names if name}


def _tool_call_requires_downstream_execution(call: ToolCall, body: Json | None = None) -> bool:
    """Return True when a tool call must be surfaced to the downstream client.

    User-machine tools (filesystem, shell, GUI, local subagents) must not run in
    the Gateway service by default. Gateway-owned tools such as HTTP Actions,
    MCP server tools, network tools, pure utilities, and Gateway state tools can
    still execute in the service.
    """
    if _gateway_executes_user_side_tools_locally():
        return False
    normalized = _normalize_tool_call(call)
    tool = BUILTIN_TOOLS.get(normalized.name)

    if normalized.name == "multi_tool_use.parallel":
        tool_uses = normalized.arguments.get("tool_uses")
        if isinstance(tool_uses, list):
            for item in tool_uses:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("tool") or item.get("tool_name") or item.get("recipient_name")
                args = item.get("arguments") or item.get("input") or item.get("parameters") or {}
                if isinstance(name, str) and "." in name:
                    name = name.rsplit(".", 1)[-1]
                if isinstance(name, str) and _tool_call_requires_downstream_execution(
                    ToolCall(f"{normalized.call_id}_nested", name, args if isinstance(args, dict) else {}, item),
                    body,
                ):
                    return True

    if tool is not None:
        return tool.risk in USER_SIDE_TOOL_RISKS

    # Gateway-owned extension points.
    if _mcp_parse_public_name(normalized.name) or _http_action_by_name(normalized.name):
        return False

    # Caller-private/custom functions are owned by the downstream client when
    # the request declared their schema. Do not fake or fail them in Gateway.
    if body is not None:
        declared = _declared_tool_names_from_body(body)
        if normalized.name in declared or call.name in declared:
            return True
    return False


def _calls_require_downstream_execution(calls: list[ToolCall], body: Json | None = None) -> bool:
    return any(_tool_call_requires_downstream_execution(call, body) for call in calls)


def _select_local_planner_files(user_text: str, max_files: int) -> list[str]:
    roots = _extract_mentioned_paths(user_text)
    if not roots:
        roots = ["src", "README.md", "docs"]
    files: list[str] = []
    patterns_by_root: list[tuple[str, str]] = []
    for root in roots:
        normalized = root.rstrip("/") or "."
        try:
            resolved = _resolve_workspace_path(normalized)
        except Exception:
            continue
        if resolved.is_file():
            try:
                rel = str(resolved.relative_to(_workspace_root()))
                files.append(rel)
            except Exception:
                pass
        elif resolved.is_dir():
            if normalized.lower().endswith("docs"):
                patterns_by_root.append((normalized, "**/*.md"))
            elif normalized.lower().endswith("src") or "src" in normalized.lower():
                patterns_by_root.append((normalized, "**/*.py"))
            else:
                patterns_by_root.extend([(normalized, "**/*.py"), (normalized, "**/*.md")])
    for root, pattern in patterns_by_root:
        result = _execute_tool_call(ToolCall(f"planner_glob_{uuid.uuid4().hex}", "Glob", {"path": root, "pattern": pattern, "limit": max_files}, {}))
        if result.success:
            for line in result.content.splitlines():
                item = line.rstrip("/")
                if item and item not in files:
                    files.append(item)
                if len(files) >= max_files:
                    break
        if len(files) >= max_files:
            break
    return files[:max_files]


def _build_local_planner_context(user_text: str) -> str:
    gateway = _gateway_config()
    max_files = max(1, min(int(gateway.get("local_planner_max_files") or 24), 80))
    max_bytes = max(1000, min(int(gateway.get("local_planner_max_bytes_per_file") or 24000), 200000))
    sections: list[str] = []
    tree = _execute_tool_call(ToolCall(f"planner_tree_{uuid.uuid4().hex}", "Tree", {"path": ".", "max_depth": 3, "max_entries": 300}, {}))
    if tree.success:
        sections.append("## 本地工具结果：项目结构 Tree\n" + tree.content)
    files = _select_local_planner_files(user_text, max_files)
    if files:
        sections.append("## 本地工具结果：命中文件列表\n" + "\n".join(files))
    symbol_sections: list[str] = []
    for file_path in [f for f in files if f.endswith(".py")][:max_files]:
        symbols = _execute_tool_call(ToolCall(f"planner_symbols_{uuid.uuid4().hex}", "PythonSymbols", {"file_path": file_path}, {}))
        if symbols.success:
            symbol_sections.append(f"### {file_path}\n{symbols.content[:12000]}")
    if symbol_sections:
        sections.append("## 本地工具结果：Python 符号/类/函数\n" + "\n\n".join(symbol_sections))
    if files:
        read_many = _execute_tool_call(
            ToolCall(
                f"planner_read_{uuid.uuid4().hex}",
                "ReadManyFiles",
                {"paths": files, "max_files": max_files, "max_bytes_per_file": max_bytes},
                {},
            )
        )
        if read_many.success:
            sections.append("## 本地工具结果：关键文件内容\n" + read_many.content)
    return "\n\n".join(sections)


def _apply_local_planner_context(path: str, body: Json) -> Json:
    if not _gateway_executes_user_side_tools_locally():
        return body
    if not _should_build_local_planner_context(path, body):
        return body
    user_text = _last_user_text(path, body)
    lowered = user_text.lower()
    direct_read = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开")) and bool(_extract_mentioned_paths(user_text))
    if isinstance(body.get("gateway_context"), dict) and body["gateway_context"].get("compacted") and not direct_read:
        return body
    context = _build_local_planner_context(user_text)
    if not context.strip():
        return body
    if direct_read:
        prompt = (
            "Gateway 已经在本地真实读取用户点名的文件/路径。"
            "下面的工具结果是事实证据，不是提示词伪造的 tool call。"
            "请直接基于这些证据回答用户原始请求；如果用户要求只输出某个值，就只输出该值，不要再说需要读取文件。\n\n"
            "# 用户原始请求\n"
            f"{user_text}\n\n"
            "# Gateway 本地真实工具证据\n"
            f"{context}"
        )
        # Direct file-read prompts from Claude Code often arrive with a huge
        # harness (system reminders, skill lists, transcript summaries).  If we
        # keep that harness, weak upstreams may ignore the injected evidence and
        # answer "I will read the file" instead of using the already-read local
        # result.  For this branch the gateway has already executed the local
        # read, so preserve only generation knobs plus a minimal evidenced user
        # request.
        preserve_keys = {"model", "max_tokens", "max_output_tokens", "temperature", "top_p", "stream"}
        updated = {key: copy.deepcopy(value) for key, value in body.items() if key in preserve_keys}
        if "/responses" in path:
            updated["input"] = prompt
        elif "/messages" in path:
            updated["messages"] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        else:
            updated["messages"] = [{"role": "user", "content": prompt}]
    else:
        prompt = (
            "Gateway 已经在本地真实执行文件/符号/目录工具完成预分析。"
            "下面的工具结果是事实证据，不是提示词伪造的 tool call。"
            "请基于这些证据完成用户请求；如果证据不足，说明还需要哪些文件/工具。\n\n"
            "# 用户原始请求\n"
            f"{user_text}\n\n"
            "# Gateway 本地真实工具证据\n"
            f"{context}\n\n"
            "# 输出要求\n"
            "按 语义分析 / 逐个类或文件分析 / 调用与证据检查 / 反思调整 / 最终结论 输出。"
        )
        updated = _replace_last_user_text(path, body, prompt)
    updated.setdefault("gateway_context", {})
    updated["gateway_context"].update({"local_planner": True, "planner_evidence_chars": len(context)})
    return updated




def _execute_tool_call(call: ToolCall, provider: str | None = None, client_id: str | None = None) -> ToolResult:
    import time as _time
    _start = _time.time()
    original_name = call.name
    call = _normalize_tool_call(call)

    # Permission check: verify tool execution is allowed for this client
    try:
        from .gateway_permissions import check_tool_permission
        allowed, reason = check_tool_permission(call.name, client_id, log=True)
        if not allowed:
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=f"Permission denied: {reason}",
                success=False,
                failure_type="permission_denied",
            )
    except ImportError:
        _logger.debug("Permission module unavailable, allowing execution")
    except Exception as exc:
        _logger.warning(f"Permission check failed for {call.name}: {exc}")
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=f"Permission check error: {exc}",
            success=False,
            failure_type="permission_denied",
        )

    tool = BUILTIN_TOOLS.get(call.name)
    mcp_target = None if tool else _mcp_parse_public_name(call.name)
    http_action = None if tool or mcp_target else (_http_action_by_name(call.name) or _http_action_by_name(original_name))
    cfg = _gateway_config() if callable(_gateway_config) else _gateway_config
    max_retries = cfg.get("tool_max_retries", 1) if isinstance(cfg, dict) else 1
    if http_action:
        try:
            max_retries = int(http_action.get("max_retries", 0) or 0)
        except (TypeError, ValueError):
            max_retries = 0
        max_retries = max(0, max_retries)
    provider = provider or "unknown"

    # Check tool result cache for cacheable read-only tools
    _tool_cache = None
    try:
        from .gateway_cache import get_tool_result_cache
        _tool_cache = get_tool_result_cache()
        if tool and _tool_cache.is_cacheable(call.name):
            cached = _tool_cache.get(call.name, call.arguments)
            if cached is not None:
                return ToolResult(call_id=call.call_id, name=call.name, content=cached, success=True)
    except Exception:
        _tool_cache = None

    last_exc: Exception | None = None
    last_result: ToolResult | None = None
    for attempt in range(max_retries + 1):
        try:
            if mcp_target:
                server_name, mcp_tool_name = mcp_target
                server = _mcp_server_by_name(server_name)
                if not server:
                    result = ToolResult(
                        call_id=call.call_id, name=call.name,
                        content=f"connector_required: MCP server {server_name} is not configured or enabled",
                        success=False, failure_type="connector_required",
                    )
                    _record_tool_failure(
                        tool_name=call.name,
                        call_id=call.call_id,
                        failure_type="connector_required",
                        arguments_keys=sorted(call.arguments.keys()),
                        content=result.content if result.content else "",
                        execution_ms=_time.time()-_start,
                        retry_count=attempt,
                        provider=provider,
                    )
                    _record_tool_stat(call.name, False, "connector_required")
                    return result
                content = _mcp_call_tool(server, mcp_tool_name, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            if http_action:
                content = _call_http_action(http_action, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            if not tool:
                result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"ToolNotFound: {call.name} is not implemented or installed in Gateway runtime",
                    success=False, failure_type="tool_not_found",
                )
                _record_tool_failure(
                    tool_name=call.name,
                    call_id=call.call_id,
                    failure_type="tool_not_found",
                    arguments_keys=sorted(call.arguments.keys()),
                    content=result.content if result.content else "",
                    execution_ms=_time.time()-_start,
                    retry_count=attempt,
                    provider=provider,
                )
                _record_tool_stat(call.name, False, "tool_not_found")
                return result
            content = tool.handler(call.arguments)
            _record_tool_stat(call.name, True)
            # Store in tool result cache for cacheable tools
            try:
                if _tool_cache and _tool_cache.is_cacheable(call.name):
                    _tool_cache.put(call.name, call.arguments, content)
            except Exception:
                pass
            return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
        except (ToolExecutionError, subprocess.TimeoutExpired) as exc:
            last_exc = exc
            if isinstance(exc, subprocess.TimeoutExpired):
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"timeout: tool execution exceeded {exc.timeout}s",
                    success=False, failure_type="timeout",
                )
            elif isinstance(exc, ToolExecutionError):
                last_result = ToolResult(
                    call_id=call.call_id, name=call.name,
                    content=f"{exc.failure_type}: {exc}",
                    success=False, failure_type=exc.failure_type,
                )
        except Exception as exc:
            # Non-transient error — do not retry
            _logger.warning("Tool %s failed with non-transient error: %s", call.name, exc)
            return ToolResult(
                call_id=call.call_id, name=call.name,
                content=f"execution_failed: {exc}",
                success=False, failure_type="execution_failed",
            )
            # transient failure — retry if attempts remain
    # All attempts exhausted
    failure_type = getattr(last_exc, "failure_type", "execution_failed") if last_exc and isinstance(last_exc, ToolExecutionError) else getattr(last_result, "failure_type", "execution_failed") if last_result else "execution_failed"
    _record_tool_failure(
        tool_name=call.name,
        call_id=call.call_id,
        failure_type=failure_type,
        arguments_keys=sorted(call.arguments.keys()),
        content=last_result.content if last_result and last_result.content else "",
        execution_ms=_time.time()-_start,
        retry_count=max_retries,
        provider=provider,
    )
    _record_tool_stat(call.name, False, failure_type)
    return last_result


def _direct_tool_result_payload(result: ToolResult) -> Json:
    payload: Json = {
        "id": result.call_id,
        "object": "gateway.tool_result",
        "name": result.name,
        "success": result.success,
        "failure_type": result.failure_type,
        "content": result.content,
        "fake_prompt_tools": False,
        "openai_chat": {
            "role": "tool",
            "tool_call_id": result.call_id,
            "content": result.content,
        },
        "openai_responses": {
            "type": "function_call_output",
            "call_id": result.call_id,
            "output": result.content,
        },
        "anthropic": {
            "type": "tool_result",
            "tool_use_id": result.call_id,
            "content": result.content,
            "is_error": not result.success,
        },
    }
    return payload


def execute_direct_tool_call(body: Json) -> Json:
    with _workspace_scope(_request_workspace_root(body)):
        calls = _direct_tool_calls_from_body(body)
        results = [_execute_tool_call(call, provider="direct") for call in calls]
    payloads = [_direct_tool_result_payload(result) for result in results]
    if len(payloads) == 1:
        return payloads[0]
    return {
        "object": "gateway.tool_results",
        "success": all(result.success for result in results),
        "results": payloads,
        "fake_prompt_tools": False,
    }



def _looks_like_context_rejection(text: str) -> bool:
    lowered = (text or "").lower()
    needles = (
        "text you sent is too long",
        "too long",
        "context length",
        "maximum context",
        "input is too large",
        "send it in parts",
        "simplify the content",
        "文本太长",
        "内容过长",
        "上下文",
        "分段发送",
    )
    return any(needle in lowered for needle in needles)

def token_count_response(body: Json) -> Json:
    return {"input_tokens": _body_token_estimate(body)}



def _build_tool_round_response(path: str, calls: list[ToolCall], results: list[ToolResult], fallback_response: Json) -> Json:
    model = fallback_response.get("model") or _config_env("UPSTREAM_MODEL", "")
    usage = fallback_response.get("usage") or {"input_tokens": 0, "output_tokens": 0}
    strategy = "gateway_local_planner_tool_round" if results else "gateway_downstream_tool_request"
    if "/messages" in path:
        content: list[dict] = []
        for call in calls:
            content.append({"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments})
        text = _response_text(path, fallback_response)
        if text:
            content.append({"type": "text", "text": text})
        # Match native tool path: assistant contains tool_use blocks only,
        # stop_reason "tool_use" signals the client to send tool_result back.
        # tool_result blocks belong in the user message, not the assistant message.
        has_tool_use = any(b.get("type") == "tool_use" for b in content)
        return {
            "id": fallback_response.get("id") or f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": "tool_use" if has_tool_use else "end_turn",
            "stop_sequence": None,
            "usage": usage,
            "gateway_context": {"strategy": strategy},
        }
    if "/responses" in path:
        output_items: list[dict] = []
        for call in calls:
            output_items.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            })
        for result in results:
            output_items.append({"type": "function_call_output", "call_id": result.call_id, "output": result.content})
        text = _response_text(path, fallback_response)
        if text:
            output_items.append({"type": "message", "content": [{"type": "output_text", "text": text}]})
        return {
            "id": fallback_response.get("id") or f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "model": model,
            "output": output_items,
            "status": "completed",
            "usage": usage,
            "gateway_context": {"strategy": strategy},
        }
    tool_calls = []
    for call in calls:
        tool_calls.append({
            "id": call.call_id,
            "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
        })
    choice = {"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": tool_calls}, "finish_reason": "tool_calls"}
    return {
        "id": fallback_response.get("id") or f"chatcmpl_gateway_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [choice],
        "usage": usage,
        "gateway_context": {"strategy": strategy},
    }


def _collect_synthetic_upstream_calls(path: str, response: Json) -> tuple[list[ToolCall], list[ToolResult]]:
    calls = _extract_tool_calls(path, response) or _extract_text_tool_calls(path, response)
    return calls, []


def _has_tool_result_in_messages(path: str, body: Json) -> bool:
    """Return True if the request already contains tool_result blocks,
    meaning the client (e.g. Claude Code) already processed tool_use and
    sent back results.  In that case the gateway must NOT re-surface
    planner tool rounds to avoid an infinite loop."""
    messages = body.get("messages") or body.get("input") or []
    if not isinstance(messages, list):
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("type") in ("tool_result", "function_call_output", "custom_tool_call_output"):
            return True
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_result", "function_call_output"):
                    return True
        # Also check for tool role messages (OpenAI chat format)
        if msg.get("role") == "tool":
            return True
    return False


def _collect_local_planner_tool_rounds(path: str, body: Json) -> tuple[list[ToolCall], list[ToolResult]]:
    ctx = body.get("gateway_context") if isinstance(body.get("gateway_context"), dict) else {}
    should_surface = bool(ctx.get("local_planner"))
    if not should_surface and not _gateway_executes_user_side_tools_locally():
        should_surface = _should_build_local_planner_context(path, body)
    if not should_surface:
        return [], []
    # If the client already sent back tool_result blocks, the tools were
    # already surfaced in a previous turn — do not surface again.
    if _has_tool_result_in_messages(path, body):
        return [], []
    user_text = _last_user_text(path, body)
    if not user_text:
        return [], []
    calls: list[ToolCall] = []
    results: list[ToolResult] = []

    def add_user_side_call(name: str, arguments: dict) -> None:
        call = ToolCall(
            f"client_required_{uuid.uuid4().hex}",
            name,
            arguments,
            {"gateway_downstream_tool_request": True},
        )
        calls.append(_normalize_tool_call(call))

    if not _gateway_executes_user_side_tools_locally():
        paths = _extract_mentioned_paths(user_text)
        lowered = user_text.lower()
        read_intent = any(token in lowered for token in ("read", "show", "cat", "open", "查看", "读取", "读", "打开"))
        analyze_intent = any(token in lowered for token in ("分析", "analyze", "review", "审查", "梳理", "check", "inspect"))
        if not paths:
            if any(token in lowered for token in ("current directory", "当前目录", "list files", "列出文件", "目录")):
                add_user_side_call("LS", {"path": "."})
            return calls, results
        for raw_path in paths[: max(1, min(int(_gateway_config().get("local_planner_max_files") or 24), 12))]:
            cleaned = raw_path.rstrip("/") or "."
            looks_file = bool(re.search(r"\.[A-Za-z0-9]{1,12}$", pathlib.PurePosixPath(cleaned).name))
            if read_intent or looks_file:
                add_user_side_call("Read", {"path": cleaned})
            elif analyze_intent:
                add_user_side_call("Tree", {"path": cleaned, "max_depth": 3, "max_entries": 300})
            else:
                add_user_side_call("LS", {"path": cleaned})
        return calls, results

    def run(name: str, arguments: dict) -> None:
        call = ToolCall(f"planner_surfaced_{uuid.uuid4().hex}", name, arguments, {"gateway_local_planner_surface": True})
        result = _execute_tool_call(call, provider="local_planner_surface")
        if result.success:
            calls.append(call)
            results.append(result)
    run("Tree", {"path": ".", "max_depth": 3, "max_entries": 300})
    files = _select_local_planner_files(user_text, max(1, min(int(_gateway_config().get("local_planner_max_files") or 24), 12)))
    if files:
        run("ReadManyFiles", {"paths": files, "max_files": len(files), "max_bytes_per_file": max(2000, min(int(_gateway_config().get("local_planner_max_bytes_per_file") or 24000), 48000))})
    return calls, results


def _tool_schema_name_local(tool: Json) -> str:
    if not isinstance(tool, dict):
        return ""
    func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    return str(func.get("name") or tool.get("name") or "").strip()


def _tool_schema_required_local(tool: Json) -> list[str]:
    if not isinstance(tool, dict):
        return []
    func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    params = func.get("parameters") or func.get("input_schema") or tool.get("parameters") or tool.get("input_schema") or {}
    if not isinstance(params, dict):
        return []
    return [str(item) for item in (params.get("required") or []) if isinstance(item, str)]


def _forced_request_tool_name(body: Json) -> str:
    forced = _forced_tool_name_from_choice(body.get("tool_choice"))
    if forced:
        return forced
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, str) and tool_choice in {"required", "any"}:
        tools = [tool for tool in (body.get("tools") or []) if isinstance(tool, dict)]
        names = [_tool_schema_name_local(tool) for tool in tools]
        names = [name for name in names if name]
        if len(names) == 1:
            return names[0]
    return ""


def _infer_forced_tool_arguments(path: str, name: str, body: Json) -> Json:
    user_text = _last_user_text(path, body)
    normalized = _normalize_tool_name(name)
    if normalized in {"calculator", "calc", "gateway__calculator"}:
        expr = ""
        code_match = re.search(r"`([^`]+)`", user_text)
        if code_match:
            expr = code_match.group(1).strip()
        if not expr:
            matches = re.findall(r"[-+*/%(). 0-9]+", user_text)
            matches = [m.strip() for m in matches if re.search(r"\d", m) and re.search(r"[+*/%-]", m)]
            if matches:
                expr = max(matches, key=len).strip()
        return {"expression": expr or user_text.strip()}
    if normalized in {"get_current_time", "current_time"}:
        tz_match = re.search(r"\b[A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?\b", user_text)
        if tz_match:
            return {"timezone": tz_match.group(0)}
        if any(token in user_text for token in ("上海", "中国", "北京时间", "Asia/Shanghai")):
            return {"timezone": "Asia/Shanghai"}
        return {}
    if normalized in {"Read", "FileInfo", "LS", "Tree", "Glob", "Grep"}:
        paths = _extract_mentioned_paths(user_text)
        if normalized == "Glob":
            return {"pattern": paths[-1] if paths else "*"}
        if normalized == "Grep":
            quoted = re.findall(r"`([^`]+)`|['\"]([^'\"]+)['\"]", user_text)
            pattern = next((a or b for a, b in quoted if (a or b)), "")
            return {"pattern": pattern or user_text.strip(), "path": paths[-1] if paths else "."}
        return {"path": paths[-1] if paths else "."}
    if normalized in {"Bash", "exec_command", "shell", "shell_command"}:
        return {"command": _extract_explicit_shell_command_request(user_text) or user_text.strip()}
    if normalized == "echo_probe":
        match = re.search(r"(?:value|echo|probe)\s+[`'\"]?([A-Za-z0-9_.:-]+)", user_text, flags=re.I)
        return {"value": match.group(1) if match else user_text.strip()}

    tool = next((tool for tool in (body.get("tools") or []) if _tool_schema_name_local(tool) == name), None)
    required = _tool_schema_required_local(tool or {})
    if len(required) == 1:
        return {required[0]: user_text.strip()}
    return {}


def _synthetic_tool_response(path: str, call: ToolCall, model: str = "") -> Json:
    if "/messages" in path:
        return {
            "id": f"msg_gateway_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    if "/responses" in path:
        return {
            "id": f"resp_gateway_{uuid.uuid4().hex}",
            "object": "response",
            "model": model,
            "output": [{
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": json.dumps(call.arguments, ensure_ascii=False),
            }],
            "status": "completed",
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        }
    return {
        "id": f"chatcmpl_gateway_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _forced_gateway_tool_round(path: str, body: Json) -> tuple[ToolCall | None, ToolResult | None, Json | None]:
    if _has_tool_result_in_messages(path, body):
        return None, None, None
    name = _forced_request_tool_name(body)
    if not name:
        return None, None, None
    call = _normalize_tool_call(ToolCall(
        call_id=f"gateway_forced_{uuid.uuid4().hex}",
        name=name,
        arguments=_infer_forced_tool_arguments(path, name, body),
        raw={"gateway_forced_tool_choice": True},
    ))
    if _tool_call_requires_downstream_execution(call, body):
        return call, None, _synthetic_tool_response(path, call, str(body.get("model") or _config_env("UPSTREAM_MODEL", "")))
    if call.name in BUILTIN_TOOLS or _mcp_parse_public_name(call.name) or _http_action_by_name(call.name):
        return call, _execute_tool_call(call, provider="gateway_forced_tool_choice"), None
    # Gateway cannot execute caller-private custom functions.  For forced
    # tool_choice, surface the required protocol-level call to the downstream
    # client instead of pretending the upstream supports native tools.
    return call, None, _synthetic_tool_response(path, call, str(body.get("model") or _config_env("UPSTREAM_MODEL", "")))


def run_tool_orchestration(path: str, body: Json, client: NativeProxyClient | None = None, client_id: str | None = None) -> Json:
    _logger.debug("run_tool_orchestration called for path: %s", path)
    workspace_root = _request_workspace_root(body)
    _logger.debug("Workspace root resolved to: %s", workspace_root)

    # Check if tools are present in body
    tools_in_body = body.get("tools", [])
    _logger.debug("Tools in request body: %d tools", len(tools_in_body))
    if len(tools_in_body) > 0:
        _logger.debug("First 3 tools: %s", [t.get('name', t.get('function', {}).get('name', 'unknown')) for t in tools_in_body[:3]])

    with _workspace_scope(workspace_root):
        return _run_tool_orchestration_scoped(path, body, client, client_id)


def _convert_response_to_path(target_path: str, response: Json) -> Json:
    """Convert a response to the format matching the target path.

    Detects the actual response format and converts if needed.
    """
    from .gateway_protocol import (
        _is_anthropic_response, _is_openai_chat_response, _is_openai_responses_response,
        _from_openai_chat_response, _from_anthropic_response_to_openai,
        _from_openai_chat_to_responses_response, _from_responses_response_to_openai,
    )
    # If already in the target format, return as-is
    if "/chat/completions" in target_path and _is_openai_chat_response(response):
        return response
    if "/responses" in target_path and _is_openai_responses_response(response):
        return response
    if "/messages" in target_path and _is_anthropic_response(response):
        return response
    # Convert to target format
    if "/chat/completions" in target_path:
        if _is_anthropic_response(response):
            return _from_anthropic_response_to_openai(response)
        if _is_openai_responses_response(response):
            return _from_responses_response_to_openai(response)
    if "/responses" in target_path:
        if _is_openai_chat_response(response):
            return _from_openai_chat_to_responses_response(response)
        if _is_anthropic_response(response):
            return _from_openai_chat_to_responses_response(_from_anthropic_response_to_openai(response))
    if "/messages" in target_path:
        if _is_openai_chat_response(response):
            return _from_openai_chat_response(target_path, response)
        if _is_openai_responses_response(response):
            return _from_openai_chat_response(target_path, _from_responses_response_to_openai(response))
    return response


def _run_tool_orchestration_scoped(path: str, body: Json, client: NativeProxyClient | None = None, client_id: str | None = None) -> Json:
    gateway_cfg = _gateway_config()
    mode = str(os.environ.get("GATEWAY_TOOL_MODE") or gateway_cfg.get("tool_mode") or "orchestrate").lower()
    memory_body = _inject_recalled_memories(path, body)
    # Gateway-owned tools may execute in the service. User-machine tools
    # (Read/LS/Bash/Skill/GUI/local agents) are surfaced to the downstream
    # client by default so they run against the user's real workspace/machine.
    direct_response = None
    if _weak_upstream_text_tools_active(mode):
        direct_response = _direct_local_file_read_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_local_skill_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_local_bash_response(path, memory_body)
        if direct_response is None:
            direct_response = _direct_downstream_tool_request_response(path, memory_body)
    if direct_response is not None:
        _remember_conversation_turn(path, body, direct_response)
        return direct_response
    upstream = client or NativeProxyClient()
    from .gateway_config import _upstream_protocol
    upstream_protocol = _upstream_protocol()

    # Convert request to upstream protocol format
    upstream_path, converted_body = _convert_request_to_upstream(path, memory_body, upstream_protocol)

    # Override model with configured upstream model
    upstream_model = _config_env("UPSTREAM_MODEL", "") or _upstream_config().get("model", "")
    if upstream_model and "model" in converted_body:
        converted_body["model"] = upstream_model

    if mode in {"passthrough", "native_passthrough", "proxy"}:
        response = upstream.forward(upstream_path, converted_body)
        # Convert response back to downstream format
        response = _convert_response_to_downstream(path, response, upstream_protocol)
        _verify_native_if_forced(path, memory_body, response)
        _remember_conversation_turn(path, body, response)
        return response
    max_rounds = _configured_max_tool_rounds(gateway_cfg)
    full_cfg = load_config()
    context_cfg = _context_config()
    fanout_response = _run_context_fanout(path, memory_body, upstream, full_cfg)
    if fanout_response is not None:
        _remember_conversation_turn(path, body, fanout_response)
        return fanout_response
    forced_call, forced_result, forced_response = (None, None, None)
    if _weak_upstream_text_tools_active(mode):
        forced_call, forced_result, forced_response = _forced_gateway_tool_round(path, memory_body)
    if forced_response is not None:
        _remember_conversation_turn(path, body, forced_response)
        return forced_response
    request_body = _merge_builtin_tools(path, _apply_local_planner_context(path, _maybe_compact_request_for_upstream(path, memory_body, context_cfg)))

    # --- Intelligence Enhancement ---
    # Analyze the user question and enhance the system prompt with insights.
    # This runs before upstream conversion so the enhanced prompt flows through normally.
    try:
        from .gateway_intelligence import enhance_intelligence, _intelligence_config, get_intelligence_summary
        intel_cfg = _intelligence_config(full_cfg.get("intelligence") if isinstance(full_cfg.get("intelligence"), dict) else None)
        if intel_cfg.enabled:
            intel_result = enhance_intelligence(request_body.get("messages", []), intel_cfg)
            # Build system prompt enhancement
            prompt_parts = []
            if intel_result.system_prompt:
                prompt_parts.append(intel_result.system_prompt)
            if intel_result.should_reflect and intel_result.reflection_prompt:
                prompt_parts.append(intel_result.reflection_prompt)
            if prompt_parts:
                enhancement = "\n\n".join(prompt_parts)
                msgs = request_body.get("messages", [])
                if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
                    existing = str(msgs[0].get("content") or "")
                    if enhancement not in existing:
                        msgs[0]["content"] = existing + "\n\n" + enhancement
                    request_body["messages"] = msgs
            _logger.debug("Intelligence: %s", get_intelligence_summary(intel_result))
    except Exception as exc:
        _logger.debug("Intelligence enhancement skipped: %s", exc)

    # Convert merged request to upstream format
    upstream_path, request_body = _convert_request_to_upstream(path, request_body, upstream_protocol)
    # Override model with configured upstream model
    if upstream_model and "model" in request_body:
        request_body["model"] = upstream_model
    tools_stripped = False
    original_tools = list(request_body.get("tools") or [])
    for _round in range(max_rounds):
        try:
            response = upstream.forward(upstream_path, request_body)
        except UpstreamHTTPError as exc:
            # Tool rejection fallback: if upstream rejects tools (400), strip and retry as text
            if exc.upstream_status == 400 and not tools_stripped and request_body.get("tools"):
                from .gateway_protocol import _inject_tools_as_text_prompt
                request_body = _without_tools(request_body)
                request_body = _inject_tools_as_text_prompt(request_body, original_tools)
                tools_stripped = True
                continue
            raise
        # Convert response to the format matching upstream_path for tool result appending
        upstream_response = _convert_response_to_path(upstream_path, response)
        # Convert response back to downstream format for tool extraction
        downstream_response = _convert_response_to_downstream(path, response, upstream_protocol)
        response_text = _response_text(path, downstream_response)
        if _looks_like_context_rejection(response_text):
            forced_fanout = _run_context_fanout(path, memory_body, upstream, full_cfg, force=True)
            if forced_fanout is not None:
                _remember_conversation_turn(path, body, forced_fanout)
                return forced_fanout
        if forced_call is None:
            _verify_native_if_forced(path, request_body, downstream_response)
        calls = _extract_tool_calls(path, downstream_response)
        text_fallback = False
        if not calls:
            calls = _extract_text_tool_calls(path, downstream_response)
            text_fallback = bool(calls)
        # Intent detection fallback for weak models that can't generate tool calls
        if not calls:
            calls = _detect_intent_tool_calls(path, downstream_response, body)
            text_fallback = bool(calls)
        if not calls:
            if forced_call is not None and forced_result is not None:
                calls = [forced_call]
                results = [forced_result]
                if text_fallback:
                    request_body = _append_text_tool_results(upstream_path, request_body, upstream_response, calls, results)
                else:
                    request_body = _append_tool_results(upstream_path, request_body, upstream_response, results)
                continue
            # If the weak upstream did not emit tool calls, synthesize the
            # protocol-level user-side tool request (default) or surface the
            # explicit legacy local-planner results (opt-in local execution).
            planner_source_body = request_body if _gateway_executes_user_side_tools_locally() else memory_body
            planner_calls, planner_results = _collect_local_planner_tool_rounds(path, planner_source_body)
            if planner_calls:
                synthetic_calls, synthetic_results = _collect_synthetic_upstream_calls(path, downstream_response)
                all_calls = planner_calls + synthetic_calls
                all_results = planner_results + synthetic_results
                response = _build_tool_round_response(path, all_calls, all_results, downstream_response)
                _remember_conversation_turn(path, body, response)
                return response
            _remember_conversation_turn(path, body, downstream_response)
            return downstream_response

        if _calls_require_downstream_execution(calls, memory_body):
            # The upstream asked for a tool that must run on the user's machine
            # (filesystem/shell/GUI/local agent) or for a caller-private custom
            # function.  Surface a protocol-level tool request to Claude Code /
            # Codex instead of executing inside the Gateway service.
            native_response = (
                _convert_text_calls_to_downstream_response(path, calls, downstream_response, upstream_protocol)
                if text_fallback
                else downstream_response
            )
            _remember_conversation_turn(path, body, native_response)
            return native_response

        # --- Key design decision: who executes tools? ---
        # When delegate_tools_to_downstream=true, all remaining text tool calls
        # are converted to native protocol format and returned to the downstream
        # client (Claude Code / Codex) for execution.
        # When false, the gateway executes tools locally (legacy behavior).
        # Auto-detect: delegate when downstream is Claude Code/Codex (path-based)
        # and upstream doesn't support native tools. Config can override.
        cfg_delegate = _gateway_config().get("delegate_tools_to_downstream")
        if cfg_delegate is None:
            # Default: gateway-owned tools execute in Gateway; user-machine
            # tools were already surfaced above. Explicit config can still
            # request legacy "delegate every tool" behavior.
            delegate = False
        else:
            delegate = bool(cfg_delegate)
        if delegate and not text_fallback:
            _remember_conversation_turn(path, body, downstream_response)
            return downstream_response
        if text_fallback and delegate:
            native_response = _convert_text_calls_to_downstream_response(
                path, calls, downstream_response, upstream_protocol,
            )
            _remember_conversation_turn(path, body, native_response)
            return native_response

        # Execute tools locally (either native calls or delegated=false)
        results = [_execute_tool_call(call, client_id=client_id) for call in calls]
        if text_fallback:
            request_body = _append_text_tool_results(upstream_path, request_body, upstream_response, calls, results)
        else:
            request_body = _append_tool_results(upstream_path, request_body, upstream_response, results)
    raise GatewayError("max tool rounds exceeded", detail={"max_tool_rounds": max_rounds})


def _stream_mode_passthrough() -> bool:
    mode = _config_env("GATEWAY_TOOL_MODE", "orchestrate").lower()
    return mode in {"passthrough", "native_passthrough", "proxy"}


def _send_sse_headers(handler: BaseHTTPRequestHandler, status: int = 200) -> None:
    handler.send_response(status)
    handler.send_header("content-type", "text/event-stream; charset=utf-8")
    handler.send_header("cache-control", "no-cache")
    handler.send_header("connection", "close")
    handler.send_header("x-accel-buffering", "no")
    handler.end_headers()
    handler.close_connection = True


def _write_sse(handler: BaseHTTPRequestHandler, payload: Any, *, event: str | None = None) -> None:
    if event:
        handler.wfile.write(f"event: {event}\n".encode("utf-8"))
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for line in data.splitlines() or [""]:
        handler.wfile.write(f"data: {line}\n".encode("utf-8"))
    handler.wfile.write(b"\n")
    handler.wfile.flush()


def _stream_tool_start(handler: BaseHTTPRequestHandler, call_id: str, name: str) -> None:
    """Send SSE event when a tool call starts execution."""
    _write_sse(handler, {
        "type": "tool_start",
        "call_id": call_id,
        "name": name,
    }, event="tool_start")


def _stream_tool_progress(handler: BaseHTTPRequestHandler, call_id: str, name: str, progress: str) -> None:
    """Send SSE event for tool execution progress (for long-running tools)."""
    _write_sse(handler, {
        "type": "tool_progress",
        "call_id": call_id,
        "name": name,
        "progress": progress,
    }, event="tool_progress")


def _stream_tool_end(handler: BaseHTTPRequestHandler, call_id: str, name: str, success: bool, content: str) -> None:
    """Send SSE event when a tool call completes."""
    _write_sse(handler, {
        "type": "tool_end",
        "call_id": call_id,
        "name": name,
        "success": success,
        "content": content,
    }, event="tool_end")


def _stream_tool_error(handler: BaseHTTPRequestHandler, call_id: str, name: str, error: str) -> None:
    """Send SSE event when a tool call fails."""
    _write_sse(handler, {
        "type": "tool_error",
        "call_id": call_id,
        "name": name,
        "error": error,
    }, event="tool_error")


# ---------------------------------------------------------------------------
# Native tool verification
# ---------------------------------------------------------------------------

def _verify_native_if_forced(path: str, body: Json, response: Json) -> None:
    """Verify that native tool calls are present when tool_choice forces them.

    Raises NativeToolVerificationError if the upstream fails to return native
    tool calls when the request requires them.
    """
    from .gateway_errors import NativeToolVerificationError

    tool_choice = body.get("tool_choice")
    if not tool_choice:
        return

    # Only check when tool_choice forces a specific function
    is_forced = False
    if isinstance(tool_choice, str):
        is_forced = tool_choice in {"required", "any"}
    elif isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type", "")
        is_forced = choice_type in {"function", "tool", "required"}

    if not is_forced:
        return

    # Check if response contains native tool calls
    calls = _extract_tool_calls(path, response)
    if not calls:
        # Also check for text-based tool calls as fallback
        text_calls = _extract_text_tool_calls(path, response)
        if not text_calls:
            raise NativeToolVerificationError(
                "upstream failed to return native tool calls when tool_choice forced a function call",
                detail={"path": path, "tool_choice": tool_choice},
            )


def _native_tool_signal(path: str, response: Json) -> bool:
    """Check if a response contains native tool call signals.

    Returns True if the response indicates native tool calls were made.
    """
    if path.startswith("/v1/chat/completions"):
        choices = response.get("choices") or []
        for choice in choices:
            message = choice.get("message") or {}
            if message.get("tool_calls"):
                return True
            if choice.get("finish_reason") == "tool_calls":
                return True
        return False
    elif path.startswith("/v1/responses"):
        output = response.get("output") or []
        for item in output:
            if isinstance(item, dict) and item.get("type") in {"function_call", "tool_call", "custom_tool_call"}:
                return True
        return False
    elif path.startswith("/v1/messages"):
        content = response.get("content") or []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return True
        return False
    return False


def _is_forced_tool_choice(path: str, body: Json) -> bool:
    """Check if tool_choice forces a specific tool call.

    Returns True if the request requires a specific tool to be called.
    """
    tool_choice = body.get("tool_choice")
    if not tool_choice:
        return False

    if path.startswith("/v1/chat/completions"):
        if isinstance(tool_choice, dict):
            return tool_choice.get("type") == "function"
        return tool_choice in {"required", "any"}
    elif path.startswith("/v1/responses"):
        if isinstance(tool_choice, dict):
            return tool_choice.get("type") == "function"
        return tool_choice in {"required", "any"}
    elif path.startswith("/v1/messages"):
        if isinstance(tool_choice, dict):
            return tool_choice.get("type") == "tool"
        return False
    return False


def _probe_body(path: str, model: str | None = None) -> Json:
    """Create a minimal request body for probing native tool support.

    Uses the echo_probe tool to test if the upstream properly handles
    native tool calls.
    """
    if path.startswith("/v1/chat/completions") or path == "/v1/chat/completions":
        return {
            "model": model or "gpt-3.5-turbo",
            "messages": [{"role": "user", "content": "Say hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "echo_probe",
                        "description": "Return the input value. Used to verify real native tool calling.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string", "description": "The value to echo back"},
                            },
                            "required": ["value"],
                        },
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "echo_probe"}},
            "max_tokens": 100,
        }
    elif path.startswith("/v1/responses"):
        return {
            "model": model or "gpt-4o-mini",
            "input": "Say hello",
            "tools": [
                {
                    "type": "function",
                    "name": "echo_probe",
                    "description": "Return the input value. Used to verify real native tool calling.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string", "description": "The value to echo back"},
                        },
                        "required": ["value"],
                    },
                }
            ],
            "tool_choice": {"type": "function", "name": "echo_probe"},
        }
    else:
        # Anthropic Messages format
        return {
            "model": model or "claude-3-haiku-20240307",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Say hello"}],
            "tools": [
                {
                    "name": "echo_probe",
                    "description": "Return the input value. Used to verify real native tool calling.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "value": {"type": "string", "description": "The value to echo back"},
                        },
                        "required": ["value"],
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "echo_probe"},
        }


def run_native_probe(path: str, client: NativeProxyClient | None = None) -> Json:
    """Run a probe to check if the upstream supports native tool calls.

    Returns a status object indicating whether native tools are supported.
    """
    try:
        body = _probe_body(path)
        upstream = client or NativeProxyClient()
        response = upstream.forward(path, body)
        calls = _extract_tool_calls(path, response)
        if calls:
            return {
                "status": "ok",
                "native_tools": True,
                "probe_tool": calls[0].name,
                "message": "Native tool calls working correctly",
            }
        text_calls = _extract_text_tool_calls(path, response)
        if text_calls:
            return {
                "status": "partial",
                "native_tools": False,
                "text_fallback": True,
                "message": "Upstream returned text-based tool calls (no native support)",
            }
        return {
            "status": "unsupported",
            "native_tools": False,
            "message": "Upstream did not return any tool calls",
        }
    except Exception as exc:
        return {
            "status": "error",
            "native_tools": False,
            "error": str(exc),
            "message": f"Probe failed: {exc}",
        }
