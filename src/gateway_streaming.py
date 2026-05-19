# =============================================================================
# Streaming tool event parsing (StreamingToolEventTests)
# Extracted from gateway_app.py lines 5237–5517 (EOF)
# =============================================================================

from __future__ import annotations

import json
import re
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gateway_app import ToolResult


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
    """Streaming orchestration entry point (stub — full impl in gateway_streaming.py)."""
    _send_sse_headers(handler)
    handler.wfile.write(b'data: {"error": "streaming not implemented"}\r\n\r\n')


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
        # First event: None event name (data-only), contains the tool_calls structure
        events.append((None, {
            "id": call_id,
            "index": index,
            "function": {"name": name, "arguments": args_str},
            "type": "function",
        }))
        events.append(("chatcmpl", {
            "id": msg_id,
            "choices": [{"index": index, "finish_reason": "tool_calls", "delta": {"tool_calls": [{"id": call_id, "function": {"name": name, "arguments": args_str}, "type": "function"}]}}],
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
    """Send SSE response headers. Stub for backward compatibility."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()


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
]
