#!/usr/bin/env python3
"""Tool runtime for the gateway.

Handles tool call parsing, normalization, execution, and orchestration.
"""
from __future__ import annotations

import json
import os
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
    usage = {"input_tokens": 0, "output_tokens": _approx_token_count(text)}
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
    limit = int(gateway.get("max_concurrent_requests") or 0)
    if limit <= 0:
        return None
    with _REQUEST_SEMAPHORE_LOCK:
        if _REQUEST_SEMAPHORE is None or _REQUEST_SEMAPHORE_SIZE != limit:
            _REQUEST_SEMAPHORE = threading.BoundedSemaphore(limit)
            _REQUEST_SEMAPHORE_SIZE = limit
        sem = _REQUEST_SEMAPHORE
    timeout = float(gateway.get("concurrency_queue_timeout_seconds") or 0)
    ok = sem.acquire(timeout=timeout) if timeout > 0 else sem.acquire(blocking=False)
    if not ok:
        from .gateway_errors import GatewayBusyError
        raise GatewayBusyError(f"gateway concurrency limit reached ({limit})")
    return sem


def _get_marketplace():
    """Lazy import for marketplace to avoid circular imports."""
    if not hasattr(_get_marketplace, '_cache'):
        try:
            from marketplace import list_mcp_marketplace
            _get_marketplace._cache = list_mcp_marketplace
        except Exception:
            _get_marketplace._cache = lambda: []
    return _get_marketplace._cache


def _request_workspace_root(body: Json) -> pathlib.Path:
    """Extract workspace root from request body or use default."""
    custom_root = body.get("workspace_root") or body.get("gateway_workspace")
    if custom_root:
        return pathlib.Path(custom_root)
    return _workspace_root()


@contextmanager
def _workspace_scope(root: pathlib.Path):
    """Context manager that temporarily changes the workspace root."""
    from . import gateway_builtin_tools as _bt
    old_root = getattr(_bt, '_WORKSPACE_ROOT_OVERRIDE', None)
    _bt._WORKSPACE_ROOT_OVERRIDE = root
    try:
        yield root
    finally:
        if old_root is None:
            if hasattr(_bt, '_WORKSPACE_ROOT_OVERRIDE'):
                delattr(_bt, '_WORKSPACE_ROOT_OVERRIDE')
        else:
            _bt._WORKSPACE_ROOT_OVERRIDE = old_root

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
    # Map common aliases
    alias_map = {
        "command": "cmd",
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
    }
    result = {}
    for key, value in arguments.items():
        mapped_key = alias_map.get(key, key)
        if mapped_key in props:
            result[mapped_key] = value
        else:
            result[key] = value
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

def _parse_text_tool_calls(text: str) -> list[ToolCall]:
    """Parse common text-only tool-call fallbacks emitted by weak native-tool providers."""

    if not text or ("<function=" not in text and "<parameter=" not in text):
        return []
    calls: list[ToolCall] = []
    function_re = re.compile(r"<function=([A-Za-z0-9_.:-]+)>\s*(.*?)(?=<function=[A-Za-z0-9_.:-]+>|\Z)", re.S)

    def append_call(name: str, args: Json, raw_text: str) -> None:
        if not name:
            return
        calls.append(
            ToolCall(
                call_id=f"textcall_{uuid.uuid4().hex}",
                name=name,
                arguments=args,
                raw={"gateway_text_tool_call_fallback": True, "text": raw_text[:2000]},
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
    return bool(_gateway_config().get("text_tool_call_fallback_enabled", True))


def _extract_text_tool_calls(path: str, response: Json) -> list[ToolCall]:
    if not _text_tool_call_fallback_enabled():
        return []
    return _parse_text_tool_calls(_response_text(path, response))


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
                **({"is_error": True} if not result.success else {}),
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
        "Gateway 已识别并执行上游文本形式的工具调用。请基于这些真实工具结果继续分析；"
        "如果还需要工具，请优先返回原生 tool_calls/tool_use，不能支持时才继续使用 <function=...> 形式。\n\n"
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
    candidates = re.findall(r"@([A-Za-z0-9_./\\-]+)", text)
    out: list[str] = []
    for candidate in candidates:
        cleaned = candidate.strip().strip(".,;:，。；：）)]}")
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
    analyze_intent = any(token in lowered for token in ("分析", "analyze", "review", "理解", "梳理"))
    code_scope = any(token in lowered for token in ("代码", "code", "项目", "project", "src", ".py", "class", "类", "@"))
    return analyze_intent and code_scope


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
    if isinstance(body.get("gateway_context"), dict) and body["gateway_context"].get("compacted"):
        return body
    if not _should_build_local_planner_context(path, body):
        return body
    user_text = _last_user_text(path, body)
    context = _build_local_planner_context(user_text)
    if not context.strip():
        return body
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




def _execute_tool_call(call: ToolCall, provider: str | None = None) -> ToolResult:
    import time as _time
    _start = _time.time()
    original_name = call.name
    call = _normalize_tool_call(call)
    tool = BUILTIN_TOOLS.get(call.name)
    mcp_target = None if tool else _mcp_parse_public_name(call.name)
    cfg = _gateway_config() if callable(_gateway_config) else _gateway_config
    max_retries = cfg.get("tool_max_retries", 1) if isinstance(cfg, dict) else 1
    provider = provider or "unknown"
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
                        content=result.content[:1000] if result.content else "",
                        execution_ms=_time.time()-_start,
                        retry_count=attempt,
                        provider=provider,
                    )
                    _record_tool_stat(call.name, False, "connector_required")
                    return result
                content = _mcp_call_tool(server, mcp_tool_name, call.arguments)
                _record_tool_stat(call.name, True)
                return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
            http_action = _http_action_by_name(call.name) or _http_action_by_name(original_name)
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
                    content=result.content[:1000] if result.content else "",
                    execution_ms=_time.time()-_start,
                    retry_count=attempt,
                    provider=provider,
                )
                _record_tool_stat(call.name, False, "tool_not_found")
                return result
            content = tool.handler(call.arguments)
            _record_tool_stat(call.name, True)
            return ToolResult(call_id=call.call_id, name=call.name, content=content, success=True)
        except (ToolExecutionError, subprocess.TimeoutExpired, Exception) as exc:
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
            else:
                last_result = ToolResult(
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
        content=last_result.content[:1000] if last_result and last_result.content else "",
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


def run_tool_orchestration(path: str, body: Json, client: NativeProxyClient | None = None) -> Json:
    with _workspace_scope(_request_workspace_root(body)):
        return _run_tool_orchestration_scoped(path, body, client)


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


def _run_tool_orchestration_scoped(path: str, body: Json, client: NativeProxyClient | None = None) -> Json:
    gateway_cfg = _gateway_config()
    mode = str(os.environ.get("GATEWAY_TOOL_MODE") or gateway_cfg.get("tool_mode") or "orchestrate").lower()
    memory_body = _inject_recalled_memories(path, body)
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
    max_rounds = int(_config_env("GATEWAY_MAX_TOOL_ROUNDS", str(DEFAULT_MAX_TOOL_ROUNDS)))
    full_cfg = load_config()
    context_cfg = _context_config()
    fanout_response = _run_context_fanout(path, memory_body, upstream, full_cfg)
    if fanout_response is not None:
        _remember_conversation_turn(path, body, fanout_response)
        return fanout_response
    request_body = _merge_builtin_tools(path, _apply_local_planner_context(path, _maybe_compact_request_for_upstream(path, memory_body, context_cfg)))
    # Convert merged request to upstream format
    upstream_path, request_body = _convert_request_to_upstream(path, request_body, upstream_protocol)
    # Override model with configured upstream model
    if upstream_model and "model" in request_body:
        request_body["model"] = upstream_model
    tools_stripped = False
    for _round in range(max_rounds):
        try:
            response = upstream.forward(upstream_path, request_body)
        except UpstreamHTTPError as exc:
            # Tool rejection fallback: if upstream rejects tools (400), strip and retry as text
            if exc.upstream_status == 400 and not tools_stripped and request_body.get("tools"):
                from .gateway_protocol import _inject_tools_as_text_prompt
                request_body = _without_tools(request_body)
                request_body = _inject_tools_as_text_prompt(request_body, [])
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
        _verify_native_if_forced(path, request_body, downstream_response)
        calls = _extract_tool_calls(path, downstream_response)
        text_fallback = False
        if not calls:
            calls = _extract_text_tool_calls(path, downstream_response)
            text_fallback = bool(calls)
        if not calls:
            _remember_conversation_turn(path, body, downstream_response)
            return downstream_response
        results = [_execute_tool_call(call) for call in calls]
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
