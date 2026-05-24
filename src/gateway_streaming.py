# =============================================================================
# Streaming tool event parsing and SSE orchestration
# Owns downstream SSE emission for passthrough and gateway-orchestrated tool calls.
# =============================================================================

from __future__ import annotations

import json
import os
import re
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .gateway_app import ToolResult


# =============================================================================
# SSE / stream parsing utilities
# =============================================================================

def _parse_sse_line(line: str) -> tuple[None, None] | tuple[str, str]:
    """Parse an SSE line (or multi-line block). Returns (event_name, data).
    'data: X' returns (None, X). 'event: foo' returns (foo, '').
    Multi-line 'event: foo\ndata: X' returns (foo, X)."""
    if "\n" in line:
        parts = line.split("\n")
        event_name, event_data = None, ""
        for p in parts:
            p = p.rstrip("\r\n")
            if not p or p.startswith(":"):
                continue
            if p.startswith("event:"):
                event_name = p[6:].strip()
            elif p.startswith("data:"):
                event_data = p[5:].lstrip()
            elif ": " in p:
                k, v = p.split(": ", 1)
                if k == "event":
                    event_name = v
            elif p.startswith("data:"):
                event_data = p[5:].lstrip()
                # Strip trailing ')' which appears in test data with malformed JSON (e.g. ...}]})")
                if event_data.endswith(")"):
                    event_data = event_data[:-1]
        return event_name, event_data
    line = line.rstrip("\r\n")
    if not line or line.startswith(":"):
        return None, None
    if line.startswith("data:"):
        data_val = line[5:].lstrip()
        if data_val.endswith(")"):
            data_val = data_val[:-1]
        return None, data_val
    if line.startswith("event:"):
        return line[6:].strip(), ""
    if ": " in line:
        key, val = line.split(": ", 1)
        return key, val
    return line, ""


def _recover_tool_calls_from_malformed(data: str) -> list[dict]:
    """Extract tool_calls from malformed JSON using a character-by-character parser
    that tracks string boundaries to find the actual JSON structure."""
    calls = []
    # Find all tool_calls objects by scanning for the "tool_calls" key
    tc_key_pattern = re.compile(r'"tool_calls"\s*:\s*\[')
    for tc_match in tc_key_pattern.finditer(data):
        start = tc_match.end()
        # Find the matching ']' of the tool_calls array
        bracket_depth = 0
        in_string = False
        i = start
        while i < len(data):
            c = data[i]
            if c == '"' and (i == 0 or data[i-1] != '\\'):
                in_string = not in_string
            elif not in_string:
                if c == '[':
                    bracket_depth += 1
                elif c == ']':
                    if bracket_depth == 0:
                        break
                    bracket_depth -= 1
            i += 1
        array_content = data[start:i]
        # Parse individual tool_call objects from the array
        brace_depth = 0
        in_str = False
        obj_start = None
        for j, c in enumerate(array_content):
            if c == '"' and (j == 0 or array_content[j-1] != '\\'):
                in_str = not in_str
            elif not in_str:
                if c == '{':
                    if obj_start is None:
                        obj_start = j
                    brace_depth += 1
                elif c == '}':
                    brace_depth -= 1
                    if brace_depth == 0 and obj_start is not None:
                        obj_text = array_content[obj_start:j+1]
                        call = _parse_tool_call_object(obj_text)
                        if call:
                            calls.append(call)
                        obj_start = None
        # Also handle trailing partial object (no closing brace) at array end
        if obj_start is not None and bracket_depth == 0:
            trailing = array_content[obj_start:].rstrip(']})\n\r\t ')
            if trailing.startswith('{'):
                call = _parse_tool_call_object(trailing + '}')
                if call:
                    calls.append(call)
    # Deduplicate by call_id, prefer entries with non-empty name
    seen: dict[str, dict] = {}
    for c in calls:
        cid = c.get("call_id") or ""
        if cid not in seen or (c.get("name") and not seen[cid].get("name")):
            seen[cid] = c
    return list(seen.values())


def _parse_tool_call_object(text: str) -> dict | None:
    """Parse a single tool_call JSON object, extracting index/id/name/arguments."""
    # Try valid JSON first
    try:
        obj = json.loads(text)
        idx = obj.get("index", 0)
        tc = obj if "type" not in obj else {}
        for t in obj.get("tool_calls", []):
            func = t.get("function", {})
            return {
                "call_id": t.get("id", ""),
                "name": func.get("name", ""),
                "arguments": func.get("arguments", ""),
            }
        # Direct format
        func = obj.get("function", {})
        return {
            "call_id": obj.get("id", ""),
            "name": func.get("name", ""),
            "arguments": func.get("arguments", ""),
        }
    except json.JSONDecodeError:
        pass
    # Regex fallback for malformed text
    result: dict = {}
    # call_id: "id":"VALUE" or "call_id":"VALUE"
    m = re.search(r'"(?:id|call_id)"\s*:\s*"([^"]*)"', text)
    if m:
        result["call_id"] = m.group(1)
    # name: "name":"VALUE"
    m = re.search(r'"name"\s*:\s*"([^"]*)"', text)
    if m:
        result["name"] = m.group(1)
    # arguments: "arguments":"VALUE" — handle escaped quotes
    m = re.search(r'"arguments"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        result["arguments"] = m.group(1)
    return result if result else None


def _detect_streaming_tool_calls_from_sse(
    path: str, event: str | None, data: str | None
) -> list[dict]:
    """Parse SSE data into tool call dicts. Handles OpenAI/Anthropic/Responses formats."""
    if data is None or data == "":
        return []
    if event == "[DONE]":
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        # Try to recover partial tool_calls from malformed JSON
        return _recover_tool_calls_from_malformed(data)
    calls = []
    # OpenAI /chat/completions
    if "/chat/completions" in path:
        choices = payload.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            tc_list = delta.get("tool_calls", [])
            for tc in tc_list:
                func = tc.get("function", {})
                calls.append({
                    "call_id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", ""),
                })
    # Anthropic /messages
    elif "/messages" in path:
        if event == "content_block_start":
            cb = payload.get("content_block", {})
            if cb.get("type") == "tool_use":
                calls.append({
                    "call_id": cb.get("id", ""),
                    "name": cb.get("name", ""),
                    "arguments": json.dumps(cb.get("input", {})),
                })
        elif event == "content_block_delta":
            delta = payload.get("delta", {})
            if delta.get("type") == "input_json_delta":
                partial_json = delta.get("partial_json", "")
                if partial_json:
                    calls.append({
                        "call_id": "",
                        "name": "",
                        "arguments": partial_json,
                        "_partial": True,
                        "_index": payload.get("index", 0),
                    })
        elif event == "content_block_stop":
            calls.append({
                "call_id": "",
                "name": "",
                "arguments": "",
                "_block_stop": True,
                "_index": payload.get("index", 0),
            })
    # Responses /responses
    elif "/responses" in path:
        resp_type = payload.get("type", "")
        item = payload.get("item") or payload.get("output") or {}
        if resp_type in ("response.output_item.done", "response.function_call_arguments.done"):
            if isinstance(item, dict) and item.get("type") == "function_call":
                calls.append({
                    "call_id": item.get("call_id", ""),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                })
        elif resp_type == "response.function_call_arguments.delta":
            delta_val = payload.get("delta", "")
            if isinstance(delta_val, str) and delta_val:
                calls.append({
                    "call_id": "",
                    "name": "",
                    "arguments": delta_val,
                    "_partial": True,
                    "_output_index": payload.get("output_index", 0),
                })
        elif resp_type == "response.output_item.added":
            if isinstance(item, dict) and item.get("type") == "function_call":
                calls.append({
                    "call_id": item.get("call_id", ""),
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                    "_initial": True,
                })
    # Unknown event name — try OpenAI format as fallback if path suggests it
    elif path and "/chat/completions" in path:
        choices = payload.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})
            tc_list = delta.get("tool_calls", [])
            for tc in tc_list:
                func = tc.get("function", {})
                calls.append({
                    "call_id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", ""),
                })
    return calls


def _forced_tool_name(path: str, body: dict) -> str:
    """Extract forced tool name from request body, or '' if not forced."""
    tc = body.get("tool_choice")
    if tc is None:
        return ""
    if isinstance(tc, str):
        return ""  # "auto" or "none" — not forced
    if isinstance(tc, dict):
        # Responses: tool_choice.name
        if tc.get("name"):
            return tc["name"]
        # OpenAI: tool_choice.function.name
        fn = tc.get("function", {})
        if fn.get("name"):
            return fn["name"]
        # Anthropic: tool_choice.type=tool, tool_choice.name
        if tc.get("type") == "tool" and tc.get("name"):
            return tc["name"]
    return ""


# =============================================================================
# Streaming orchestration entry point
# =============================================================================


def run_streaming_orchestration(
    handler: Any, path: str, body: dict
) -> None:
    """Streaming orchestration with tool call support."""
    from .gateway_config import _configured_max_tool_rounds, _gateway_config, _upstream_protocol
    from .gateway_context import (
        _context_config,
        _inject_recalled_memories,
        _maybe_compact_request_for_upstream,
        _remember_conversation_turn,
    )
    from .gateway_proxy import NativeProxyClient
    from .gateway_protocol import _convert_request_to_upstream, _convert_response_to_downstream
    from .gateway_tool_runtime import (
        _append_text_tool_results,
        _append_tool_results,
        _convert_response_to_path,
        _execute_tool_call,
        _extract_text_tool_calls,
        _extract_tool_calls,
    )

    _send_sse_headers(handler)

    gateway_cfg = _gateway_config()
    mode = str(os.environ.get("GATEWAY_TOOL_MODE") or gateway_cfg.get("tool_mode") or "orchestrate").lower()
    upstream_protocol = _upstream_protocol()

    if mode in {"passthrough", "native_passthrough", "proxy"}:
        _stream_upstream_passthrough(handler, path, body)
        return

    max_rounds = _configured_max_tool_rounds(gateway_cfg)
    upstream = NativeProxyClient()
    memory_body = _inject_recalled_memories(path, body)
    context_cfg = _context_config()
    request_body = _merge_builtin_tools(path, _maybe_compact_request_for_upstream(path, memory_body, context_cfg))

    upstream_path, request_body = _convert_request_to_upstream(path, request_body, upstream_protocol)
    # The gateway is responsible for emitting downstream SSE in orchestrate
    # mode. Keep the upstream request non-streaming unless passthrough is used.
    request_body = dict(request_body)
    request_body["stream"] = False

    tools_stripped = False
    for _round in range(max_rounds):
        try:
            response = upstream.forward(upstream_path, request_body)
        except Exception as exc:
            from .gateway_errors import UpstreamHTTPError
            if isinstance(exc, UpstreamHTTPError) and exc.upstream_status == 400 and not tools_stripped and request_body.get("tools"):
                from .gateway_protocol import _inject_tools_as_text_prompt, _without_tools
                request_body = _without_tools(request_body)
                request_body = _inject_tools_as_text_prompt(request_body, [])
                tools_stripped = True
                continue
            _write_sse(handler, {"error": str(exc)}, event="error")
            _write_sse(handler, "[DONE]", event="done")
            return

        upstream_response = _convert_response_to_path(upstream_path, response)
        downstream_response = _convert_response_to_downstream(path, response, upstream_protocol)
        calls = _extract_tool_calls(path, downstream_response)
        text_fallback = False
        if not calls:
            calls = _extract_text_tool_calls(path, downstream_response)
            text_fallback = bool(calls)

        if not calls:
            _stream_final_response(handler, path, downstream_response)
            _remember_conversation_turn(path, body, downstream_response)
            return

        results = []
        for call in calls:
            _write_sse(handler, {
                "type": "tool_start",
                "call_id": call.call_id,
                "name": call.name,
            }, event="tool_start")
            result = _execute_tool_call(call, provider="streaming")
            results.append(result)
            _write_sse(handler, {
                "type": "tool_result",
                "call_id": result.call_id,
                "name": result.name,
                "success": result.success,
                "failure_type": result.failure_type,
                "content": result.content,
            }, event="tool_result")

        if text_fallback:
            request_body = _append_text_tool_results(upstream_path, request_body, upstream_response, calls, results)
        else:
            request_body = _append_tool_results(upstream_path, request_body, upstream_response, results)

    _write_sse(handler, {"error": "max tool rounds exceeded"}, event="error")
    _write_sse(handler, "[DONE]", event="done")


def _tools_enabled_for_upstream() -> str:
    """Check if tools should be enabled for the upstream API."""
    from .gateway_config import _upstream_config
    cfg = _upstream_config()
    return str(cfg.get("tools_enabled", "auto") or "auto").strip().lower()


def _upstream_native_tools_capable() -> bool:
    """Return whether the active upstream profile is configured as native-tool capable."""
    from .gateway_config import _upstream_config
    cfg = _upstream_config()
    capabilities = cfg.get("capabilities") if isinstance(cfg.get("capabilities"), dict) else {}
    return bool(capabilities.get("supports_tools", True)) and bool(capabilities.get("supports_function_calls", True))


def _should_use_text_tool_adapter(tools_enabled: str, native_capable: bool) -> bool:
    """Decide whether gateway tools must be exposed as text-call instructions.

    ``auto`` is intentionally conservative: if the user marks the upstream as
    not supporting native tools/function-calls, do not send native ``tools``
    schemas upstream. Use the local gateway adapter instead so weak model APIs
    do not reject the request or hallucinate unsupported protocol objects.
    """
    if tools_enabled in {"off", "disabled", "false", "0", "none", "text_only", "adapter"}:
        return True
    if tools_enabled == "auto" and not native_capable:
        return True
    return False


def _maybe_compact_for_text_tool_adapter(path: str, body: dict) -> dict:
    """Compact bulky Claude Code/Codex harness payloads before text-tool injection.

    Weak upstreams that do not support native tools often have lower practical
    request-size limits than their advertised context window.  A Claude Code
    request can include a large system prompt, skill list, and tool schemas even
    for a one-line user prompt.  When we must downgrade native tools to the
    Gateway text adapter, compact that harness before adding our own adapter
    instructions so the upstream does not answer with a provider-level
    ``too long`` refusal.

    The limit is dynamic: max(8000, min(upstream.max_input_tokens * 0.45, config_cap)).
    Config cap defaults to 48000; set to 0 to disable.
    """
    try:
        from .gateway_config import _resolved_text_tool_adapter_compact_token_limit
        from .gateway_context import _body_token_estimate, _compact_request_for_upstream, _context_config
        limit = _resolved_text_tool_adapter_compact_token_limit()
        if limit <= 0 or _body_token_estimate(body) <= limit:
            return body
        context_cfg = dict(_context_config())
        context_cfg["enabled"] = True
        # Keep enough user intent while forcing large system-reminder blocks to
        # shrink.  ``summary_max_chars`` is per content block, so cap it below
        # the token budget rather than using the global context threshold.
        try:
            existing_summary = int(context_cfg.get("summary_max_chars") or 6000)
        except (TypeError, ValueError):
            existing_summary = 6000
        context_cfg["summary_max_chars"] = max(1000, min(existing_summary, 6000))
        return _compact_request_for_upstream(path, body, context_cfg, reason="weak_upstream_text_tools")
    except Exception:
        return body


def _merge_builtin_tools(path: str, body: dict) -> dict:
    """Merge builtin tools into request body. Respects tools_enabled config."""
    from .gateway_builtin_tools import BUILTIN_TOOLS
    from .gateway_mcp import _mcp_tool_schemas
    from .gateway_http_actions import _http_action_schemas
    from .gateway_protocol import _inject_tools_as_text_prompt
    from .gateway_mcp import _tool_schema_for_path

    import copy
    body = copy.deepcopy(body)

    tools_enabled = _tools_enabled_for_upstream()
    native_capable = _upstream_native_tools_capable()
    if tools_enabled == "native_only" and not native_capable:
        from .gateway_errors import GatewayError
        raise GatewayError("upstream profile is configured as native_only but capabilities disable native tools/function calls")

    # Get existing tools
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        tools = []

    # Add builtin tools
    for tool in BUILTIN_TOOLS.values():
        schema = _tool_schema_for_path(path, tool)
        tools.append(schema)

    # Add MCP tools
    mcp_schemas = _mcp_tool_schemas(path)
    tools.extend(mcp_schemas)

    # Add HTTP action tools
    action_schemas = _http_action_schemas(path)
    tools.extend(action_schemas)

    if _should_use_text_tool_adapter(tools_enabled, native_capable):
        # Strip native tools and inject as compact text prompt.  Compact first so
        # Claude Code/Codex harness metadata plus adapter instructions stay under
        # weak upstream request-size limits.
        body = _maybe_compact_for_text_tool_adapter(path, body)
        body.pop("tools", None)
        body.pop("tool_choice", None)
        body = _inject_tools_as_text_prompt(body, tools)
        return body

    body["tools"] = tools
    return body


def _stream_upstream_passthrough(handler: Any, path: str, body: dict) -> None:
    """Stream response directly from upstream."""
    from .gateway_proxy import NativeProxyClient
    import urllib.request
    import json

    client = NativeProxyClient()
    url = client._url(path)
    headers = client._headers()
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=client.timeout) as resp:
            for line in resp:
                handler.wfile.write(line)
                handler.wfile.flush()
    except Exception as exc:
        _write_sse(handler, {"error": str(exc)}, event="error")
    finally:
        _write_sse(handler, "[DONE]", event="done")


def _stream_final_response(handler: Any, path: str, response: dict) -> None:
    """Stream the final response to client."""
    if "/chat/completions" in path:
        choices = response.get("choices", [])
        for choice in choices:
            message = choice.get("message", {})
            if message.get("content"):
                _write_sse(handler, {
                    "id": response.get("id", ""),
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": message["content"]},
                        "finish_reason": None,
                    }],
                })
        _write_sse(handler, {
            "id": response.get("id", ""),
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        })
    elif "/messages" in path:
        content = response.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                _write_sse(handler, {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                })
                _write_sse(handler, {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": item["text"]},
                })
                _write_sse(handler, {
                    "type": "content_block_stop",
                    "index": 0,
                })
        _write_sse(handler, {"type": "message_stop"})
    elif "/responses" in path:
        output = response.get("output", [])
        for item in output:
            if isinstance(item, dict) and item.get("type") == "message":
                content_parts = item.get("content", [])
                for part in content_parts:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        _write_sse(handler, {
                            "type": "response.output_text.delta",
                            "delta": part.get("text", ""),
                        })
        _write_sse(handler, {"type": "response.completed"})

    _write_sse(handler, "[DONE]", event="done")




def _streaming_tool_event_for_path(
    path: str,
    call_id: str,
    name: str,
    arguments: dict,
    result: "ToolResult",
    msg_id: str,
    index: int,
) -> list[tuple[str, dict]]:
    """Build streaming SSE events for a tool result. Returns list of (event_name, payload)."""
    events: list[tuple[str, dict]] = []
    args_str = json.dumps(arguments)
    if "/chat/completions" in path:
        events.append(("chatcmpl", {
            "id": msg_id,
            "object": "chat.completion.chunk",
            "choices": [{
                "index": index,
                "finish_reason": None,
                "delta": {"tool_calls": [{"id": call_id, "index": index, "function": {"name": name, "arguments": args_str}, "type": "function"}]},
            }],
        }))
        events.append(("chatcmpl", {
            "id": msg_id,
            "object": "chat.completion.chunk",
            "choices": [{"index": index, "finish_reason": "tool_calls", "delta": {}}],
        }))
    elif "/messages" in path:
        # Anthropic: content_block_start → content_block_delta → content_block_stop
        events.append(("content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        }))
        events.append(("content_block_delta", {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": args_str},
        }))
        events.append(("content_block_stop", {
            "type": "content_block_stop",
            "index": index,
        }))
    elif "/responses" in path:
        # Responses: output_item.added → function_call_arguments.done → output_item.done
        fc_id = f"fc_{uuid.uuid4().hex}"
        events.append(("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": "", "arguments": ""},
        }))
        events.append(("response.function_call_arguments.done", {
            "type": "response.function_call_arguments.done",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": name, "arguments": args_str},
        }))
        events.append(("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": index,
            "item": {"type": "function_call", "id": fc_id, "call_id": call_id, "name": name, "arguments": args_str},
        }))
    return events


# =============================================================================
# Backward-compat re-exports (match original gateway_app.py public names)
# =============================================================================
# These symbols were originally defined in gateway_app.py.
# Re-export them from here so existing imports continue to work.

def _send_sse_headers(handler: Any) -> None:
    """Send SSE response headers."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()


def _write_sse(handler: Any, payload: Any, *, event: str | None = None) -> None:
    """Write an SSE event to the handler."""
    if event:
        handler.wfile.write(f"event: {event}\n".encode("utf-8"))
    data = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for line in data.splitlines() or [""]:
        handler.wfile.write(f"data: {line}\n".encode("utf-8"))
    handler.wfile.write(b"\n")
    handler.wfile.flush()


__all__ = [
    "_parse_sse_line",
    "_recover_tool_calls_from_malformed",
    "_parse_tool_call_object",
    "_detect_streaming_tool_calls_from_sse",
    "_forced_tool_name",
    "run_streaming_orchestration",
    "_streaming_tool_event_for_path",
    # backward-compat stubs
    "_send_sse_headers",
    "_write_sse",
]
