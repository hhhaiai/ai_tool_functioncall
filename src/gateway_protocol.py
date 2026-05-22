#!/usr/bin/env python3
"""Protocol conversion between OpenAI and Anthropic formats.

Handles conversion of tools, messages, and responses between different API formats.
"""
from __future__ import annotations

import copy
import json
import re
from typing import Any

Json = dict[str, Any]


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "input_text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "output_text":
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return json.dumps(content, ensure_ascii=False)
    return str(content) if content else ""


def _openai_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text") or "")
    return str(content) if content else ""


def _anthropic_system_to_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for item in system:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(system) if system else ""


def _anthropic_tools_to_openai(tools: list[Json]) -> list[Json]:
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not name:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema") or tool.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return result


def _openai_tools_to_anthropic(tools: list[Json]) -> list[Json]:
    result = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function")
        if not func or not isinstance(func, dict):
            continue
        name = func.get("name")
        if not name:
            continue
        result.append({
            "name": name,
            "description": func.get("description", ""),
            "input_schema": func.get("parameters") or {"type": "object", "properties": {}},
        })
    return result


def _anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return "auto"
        if tool_choice == "none":
            return "none"
        if tool_choice == "required":
            return "required"
        return "auto"
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type")
        if choice_type == "auto":
            return "auto"
        if choice_type == "none":
            return "none"
        if choice_type == "any":
            return "required"
        if choice_type == "tool":
            name = tool_choice.get("name")
            if name:
                return {"type": "function", "function": {"name": name}}
    return "auto"


def _convert_anthropic_messages_to_openai(messages: list[Json]) -> tuple[list[Json], str | None]:
    result = []
    system_text = None
    reasoning_text = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            system_text = _anthropic_system_to_text(content)
            continue
        if role == "user":
            if isinstance(content, str):
                result.append({"role": "user", "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_results = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(str(item.get("text") or ""))
                        elif item.get("type") == "image":
                            text_parts.append("[image]")
                        elif item.get("type") == "tool_result":
                            tool_use_id = item.get("tool_use_id", "")
                            result_text = _text_from_content(item.get("content") or "")
                            tool_results.append({
                                "role": "tool",
                                "tool_call_id": tool_use_id,
                                "content": result_text,
                            })
                    elif isinstance(item, str):
                        text_parts.append(item)
                # Text parts first, then tool results
                if text_parts:
                    result.append({"role": "user", "content": "\n".join(text_parts)})
                result.extend(tool_results)
        elif role == "assistant":
            if isinstance(content, str):
                result.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts = []
                tool_calls = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            text_parts.append(str(item.get("text") or ""))
                        elif item.get("type") == "thinking":
                            reasoning_text = str(item.get("thinking") or "")
                        elif item.get("type") == "tool_use":
                            tool_calls.append({
                                "id": item.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": item.get("name", ""),
                                    "arguments": json.dumps(item.get("input") or {}, ensure_ascii=False),
                                },
                            })
                msg_out: Json = {"role": "assistant"}
                if text_parts:
                    msg_out["content"] = "\n".join(text_parts)
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                result.append(msg_out)
    # Return reasoning_text if present, otherwise system_text
    # System text is added separately by the caller (_to_openai_chat_payload)
    return result, reasoning_text or system_text


def _preserve_anthropic_fields(body: Json, payload: Json) -> None:
    for field in ("metadata", "stop_sequences", "stream"):
        if field in body and field not in payload:
            payload[field] = body[field]
    # Preserve Anthropic-specific fields in gateway_context
    gateway_context = payload.setdefault("gateway_context", {})
    if "thinking" in body:
        gateway_context["anthropic_thinking"] = body["thinking"]
    if "context_management" in body:
        gateway_context["anthropic_context_management"] = body["context_management"]
    if "output_config" in body:
        gateway_context["anthropic_output_config"] = body["output_config"]


def _to_openai_chat_payload(path: str, body: Json, *, stream: bool | None = None) -> Json:
    if "/messages" not in path:
        payload = copy.deepcopy(body)
        if stream is not None:
            payload["stream"] = stream
        return payload
    messages, system_text = _convert_anthropic_messages_to_openai(body.get("messages") or [])
    # Also check top-level "system" field (Anthropic format)
    if not system_text:
        system_text = body.get("system")
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})
    payload: Json = {
        "model": body.get("model") or "",
        "messages": messages,
        "stream": stream if stream is not None else body.get("stream", False),
    }
    if body.get("max_tokens"):
        payload["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        payload["top_p"] = body["top_p"]
    tools = body.get("tools")
    if tools:
        payload["tools"] = _anthropic_tools_to_openai(tools)
    tool_choice = body.get("tool_choice")
    if tool_choice:
        payload["tool_choice"] = _anthropic_tool_choice_to_openai(tool_choice)
    _preserve_anthropic_fields(body, payload)
    return payload


def _openai_tool_calls_from_response(response: Json) -> list[dict]:
    choices = response.get("choices") or []
    for choice in choices:
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            return tool_calls
    return []


def _from_openai_chat_response(path: str, response: Json) -> Json:
    if "/messages" not in path:
        return response
    choices = response.get("choices") or []
    if not choices:
        return {"role": "assistant", "content": []}
    message = choices[0].get("message") or {}
    content_parts = []
    if message.get("reasoning"):
        content_parts.append({"type": "thinking", "thinking": message["reasoning"]})
    if message.get("content"):
        content_parts.append({"type": "text", "text": message["content"]})
    for tc in message.get("tool_calls") or []:
        func = tc.get("function") or {}
        try:
            args = json.loads(func.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        content_parts.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": func.get("name", ""),
            "input": args,
        })
    result: Json = {
        "role": "assistant",
        "content": content_parts,
    }
    finish_reason = choices[0].get("finish_reason")
    if finish_reason:
        result["stop_reason"] = finish_reason
    return result


def _last_user_text(path: str, body: Json) -> str:
    messages = body.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return _text_from_content(msg.get("content"))
    return ""


def _replace_last_user_text(path: str, body: Json, text: str) -> Json:
    body = copy.deepcopy(body)
    messages = body.get("messages") or []
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and messages[i].get("role") == "user":
            if "/messages" in path:
                messages[i]["content"] = [{"type": "text", "text": text}]
            else:
                messages[i]["content"] = text
            break
    body["messages"] = messages
    return body


def _without_tools(body: Json) -> Json:
    body = copy.deepcopy(body)
    body.pop("tools", None)
    body.pop("tool_choice", None)
    return body


# =============================================================================
# Cross-protocol conversion: OpenAI Chat <-> Anthropic Messages
# =============================================================================

def _openai_tool_choice_to_anthropic(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "none":
            return {"type": "none"}
        if tool_choice == "required":
            return {"type": "any"}
    if isinstance(tool_choice, dict):
        if tool_choice.get("type") == "function":
            name = tool_choice.get("function", {}).get("name")
            if name:
                return {"type": "tool", "name": name}
    return {"type": "auto"}


def _openai_messages_to_anthropic(messages: list[Json]) -> tuple[list[Json], str | None]:
    system_text = None
    result = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            system_text = _text_from_content(msg.get("content"))
            continue
        if role == "user":
            content = msg.get("content")
            if isinstance(content, str):
                result.append({"role": "user", "content": content})
            elif isinstance(content, list):
                result.append({"role": "user", "content": content})
        elif role == "assistant":
            content_parts = []
            text = msg.get("content")
            if text:
                content_parts.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                func = tc.get("function") or {}
                try:
                    args = json.loads(func.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                content_parts.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "input": args,
                })
            if content_parts:
                result.append({"role": "assistant", "content": content_parts})
        elif role == "tool":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content", ""),
                }],
            })
    return result, system_text


def _openai_chat_to_anthropic_payload(body: Json, *, stream: bool | None = None) -> Json:
    messages, system_text = _openai_messages_to_anthropic(body.get("messages") or [])
    payload: Json = {
        "model": body.get("model") or "",
        "max_tokens": body.get("max_tokens") or 4096,
        "messages": messages,
    }
    if system_text:
        payload["system"] = system_text
    if stream is not None:
        payload["stream"] = stream
    elif body.get("stream"):
        payload["stream"] = True
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        payload["top_p"] = body["top_p"]
    tools = body.get("tools")
    if tools:
        payload["tools"] = _openai_tools_to_anthropic(tools)
    tool_choice = body.get("tool_choice")
    if tool_choice:
        payload["tool_choice"] = _openai_tool_choice_to_anthropic(tool_choice)
    return payload


def _from_anthropic_response_to_openai(response: Json) -> Json:
    content = response.get("content") or []
    text_parts = []
    tool_calls = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                text_parts.append(str(item.get("text") or ""))
            elif item.get("type") == "tool_use":
                tool_calls.append({
                    "id": item.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": json.dumps(item.get("input") or {}, ensure_ascii=False),
                    },
                })
    message: Json = {"role": "assistant"}
    if text_parts:
        message["content"] = "\n".join(text_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish_reason = response.get("stop_reason") or "stop"
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
    }


# =============================================================================
# Cross-protocol conversion: OpenAI Chat <-> OpenAI Responses
# =============================================================================

def _openai_chat_to_responses_payload(body: Json, *, stream: bool | None = None) -> Json:
    messages = body.get("messages") or []
    input_items = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            input_items.append({"role": "system", "content": msg.get("content", "")})
        elif role == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                input_items.append({"role": "user", "content": content})
            elif isinstance(content, list):
                input_items.append({"role": "user", "content": content})
        elif role == "assistant":
            content = msg.get("content", "")
            if content:
                input_items.append({"role": "assistant", "content": content})
            for tc in msg.get("tool_calls") or []:
                func = tc.get("function") or {}
                input_items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", "{}"),
                })
        elif role == "tool":
            input_items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": msg.get("content", ""),
            })
    payload: Json = {
        "model": body.get("model") or "",
        "input": input_items,
    }
    if stream is not None:
        payload["stream"] = stream
    elif body.get("stream"):
        payload["stream"] = True
    if body.get("max_tokens"):
        payload["max_output_tokens"] = body["max_tokens"]
    tools = body.get("tools")
    if tools:
        resp_tools = []
        for t in tools:
            if isinstance(t, dict) and t.get("type") == "function":
                func = t.get("function") or {}
                resp_tools.append({
                    "type": "function",
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters") or {},
                })
        payload["tools"] = resp_tools
    return payload


def _from_responses_response_to_openai(response: Json) -> Json:
    output = response.get("output") or []
    text_parts = []
    tool_calls = []
    for item in output:
        if isinstance(item, dict):
            if item.get("type") == "message":
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        text_parts.append(str(c.get("text") or ""))
            elif item.get("type") == "function_call":
                tool_calls.append({
                    "id": item.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                })
    message: Json = {"role": "assistant"}
    if text_parts:
        message["content"] = "\n".join(text_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
    }


def _from_openai_chat_to_responses_response(response: Json) -> Json:
    """Convert OpenAI Chat response to OpenAI Responses format."""
    choices = response.get("choices") or []
    if not choices:
        return {"output": []}
    message = choices[0].get("message") or {}
    output_items = []
    if message.get("content"):
        output_items.append({
            "type": "message",
            "content": [{"type": "output_text", "text": message["content"]}],
        })
    for tc in message.get("tool_calls") or []:
        func = tc.get("function") or {}
        output_items.append({
            "type": "function_call",
            "call_id": tc.get("id", ""),
            "name": func.get("name", ""),
            "arguments": func.get("arguments", "{}"),
        })
    return {"output": output_items}


# =============================================================================
# Unified conversion entry points
# =============================================================================

def _convert_request_to_upstream(downstream_path: str, body: Json, upstream_protocol: str) -> tuple[str, Json]:
    """Convert downstream request to upstream format. Returns (upstream_path, converted_body)."""
    if upstream_protocol == "anthropic_messages":
        if "/messages" in downstream_path:
            return "/v1/messages", body
        converted = _openai_chat_to_anthropic_payload(body)
        return "/v1/messages", converted
    if upstream_protocol == "openai_responses":
        if "/responses" in downstream_path:
            return "/v1/responses", body
        converted = _openai_chat_to_responses_payload(body)
        return "/v1/responses", converted
    # upstream_protocol == "openai_chat" (default)
    if "/messages" in downstream_path:
        converted = _to_openai_chat_payload(downstream_path, body)
        return "/v1/chat/completions", converted
    if "/responses" in downstream_path:
        # Responses -> Chat: extract messages from input items
        converted = _responses_to_chat_payload(body)
        return "/v1/chat/completions", converted
    return "/v1/chat/completions", body


def _is_anthropic_response(response: Json) -> bool:
    """Detect if a response is in Anthropic Messages format."""
    if not isinstance(response, dict):
        return False
    if "stop_reason" in response:
        return True
    content = response.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict) and first.get("type") in ("text", "tool_use", "thinking"):
            return True
    return False


def _is_openai_chat_response(response: Json) -> bool:
    """Detect if a response is in OpenAI Chat Completions format."""
    if not isinstance(response, dict):
        return False
    return "choices" in response


def _is_openai_responses_response(response: Json) -> bool:
    """Detect if a response is in OpenAI Responses format."""
    if not isinstance(response, dict):
        return False
    output = response.get("output")
    if isinstance(output, list) and output:
        first = output[0]
        if isinstance(first, dict) and first.get("type") in ("function_call", "message", "tool_call"):
            return True
    return False


def _convert_response_to_downstream(downstream_path: str, response: Json, upstream_protocol: str) -> Json:
    """Convert upstream response back to downstream format.

    Detects the actual response format to handle cases where the upstream
    returns a response in a different format than expected (e.g., test doubles,
    misconfigured upstreams).
    """
    # If response is already in the target downstream format, return as-is
    if "/messages" in downstream_path and _is_anthropic_response(response):
        return response
    if "/responses" in downstream_path and _is_openai_responses_response(response):
        return response
    if "/chat/completions" in downstream_path and _is_openai_chat_response(response):
        return response

    # Convert based on upstream protocol
    if upstream_protocol == "anthropic_messages":
        if "/messages" in downstream_path:
            return response
        return _from_anthropic_response_to_openai(response)
    if upstream_protocol == "openai_responses":
        if "/responses" in downstream_path:
            return response
        return _from_responses_response_to_openai(response)
    # upstream_protocol == "openai_chat"
    if "/messages" in downstream_path:
        return _from_openai_chat_response(downstream_path, response)
    if "/responses" in downstream_path:
        return _from_openai_chat_to_responses_response(response)
    return response


def _responses_to_chat_payload(body: Json) -> Json:
    """Convert OpenAI Responses format request to Chat format."""
    raw_input = body.get("input")
    # Handle string input (simple message)
    if isinstance(raw_input, str):
        messages = [{"role": "user", "content": raw_input}]
    else:
        input_items = raw_input or []
        messages = []
        for item in input_items:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role in ("system", "user", "assistant"):
                content = item.get("content", "")
                if isinstance(content, str):
                    messages.append({"role": role, "content": content})
                elif isinstance(content, list):
                    messages.append({"role": role, "content": content})
            elif item.get("type") == "function_call":
                messages.append({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": item.get("call_id", ""),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }],
                })
            elif item.get("type") == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": item.get("output", ""),
                })
    # Add instructions as system message
    instructions = body.get("instructions")
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    payload: Json = {
        "model": body.get("model") or "",
        "messages": messages,
    }
    if body.get("max_output_tokens"):
        payload["max_tokens"] = body["max_output_tokens"]
    tools = body.get("tools")
    if tools:
        chat_tools = []
        for t in tools:
            if isinstance(t, dict) and t.get("type") == "function":
                chat_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters") or {},
                    },
                })
        payload["tools"] = chat_tools
    return payload


# =============================================================================
# Tool degradation: inject tool definitions as text for non-tool APIs
# =============================================================================

_TOOL_FORMAT_TAG = "function"
_PARAM_TAG = "parameter"


def _build_tool_text_block(tools: list[Json]) -> str:
    """Build a text block describing available tools for injection into system prompt."""
    lines = [
        "Tool Call Gateway - 你正在通过 Tool Call Gateway 服务 Claude Code/Codex/OpenCode/DeepSeek-TUI。",
        "如果上游不能稳定返回原生工具调用，可以使用文本形式：<function=ToolName>\\n<parameter=name>value。",
        "Gateway 会在本地执行真实工具并把结果回填。",
        "",
        "[Available Tools]",
        "You have access to the following tools. To call a tool, output exactly this format:",
        "",
        "<" + _TOOL_FORMAT_TAG + "=TOOL_NAME>",
        "  <" + _PARAM_TAG + "=param1>value1</" + _PARAM_TAG + ">",
        "  <" + _PARAM_TAG + "=param2>value2</" + _PARAM_TAG + ">",
        "</" + _TOOL_FORMAT_TAG + ">",
        "",
    ]
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        func = tool.get("function") or tool
        name = func.get("name", "")
        desc = func.get("description", "")
        params = func.get("parameters") or func.get("input_schema") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        lines.append(f"Tool: {name}")
        if desc:
            lines.append(f"  Description: {desc}")
        if props:
            lines.append("  Parameters:")
            for pname, pinfo in props.items():
                ptype = (pinfo or {}).get("type", "string")
                pdesc = (pinfo or {}).get("description", "")
                req_str = " (required)" if pname in required else ""
                lines.append(f"    - {pname} ({ptype}){req_str}: {pdesc}")
        lines.append("")
    lines.append("Output ONLY the tool call tags when you need to use a tool. Do not explain the format.")
    return "\n".join(lines)


def _inject_tools_as_text_prompt(body: Json, tools: list[Json]) -> Json:
    """Inject tool definitions and format instructions into the system/user prompt."""
    body = copy.deepcopy(body)
    if not tools:
        return body
    tool_text = _build_tool_text_block(tools)
    messages = body.get("messages") or []
    if not messages:
        return body
    first = messages[0]
    if isinstance(first, dict):
        if first.get("role") == "system":
            first["content"] = tool_text + "\n\n" + str(first.get("content") or "")
        elif first.get("role") == "user":
            content = first.get("content")
            if isinstance(content, str):
                first["content"] = tool_text + "\n\n" + content
            elif isinstance(content, list):
                first["content"] = [{"type": "text", "text": tool_text}] + list(content)
    body["messages"] = messages
    return body
