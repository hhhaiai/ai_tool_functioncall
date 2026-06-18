#!/usr/bin/env python3
"""Protocol conversion between OpenAI and Anthropic formats.

Handles conversion of tools, messages, and responses between different API formats.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
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


def _convert_anthropic_messages_to_openai(messages: list[Json]) -> tuple[list[Json], str | None, str | None]:
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
                            thinking_part = str(item.get("thinking") or "")
                            if thinking_part:
                                reasoning_text = (reasoning_text + "\n" + thinking_part).strip() if reasoning_text else thinking_part
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
    return result, system_text, reasoning_text


def _preserve_anthropic_fields(body: Json, payload: Json) -> None:
    for field in ("metadata", "stop_sequences", "stream"):
        if field in body and field not in payload:
            payload[field] = body[field]
    # Preserve existing gateway_context from the body (e.g. local_planner,
    # planner_evidence_chars) so that downstream conversion stages can still
    # detect gateway-local planner activity.
    existing_ctx = body.get("gateway_context") if isinstance(body.get("gateway_context"), dict) else {}
    gateway_context = payload.setdefault("gateway_context", {})
    for k, v in existing_ctx.items():
        if k not in gateway_context:
            gateway_context[k] = v
    # Preserve Anthropic-specific fields in gateway_context
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
    messages, system_text, reasoning_text = _convert_anthropic_messages_to_openai(body.get("messages") or [])
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
    # Map Anthropic stop_sequences to OpenAI stop
    stop_sequences = body.get("stop_sequences")
    if stop_sequences and "stop" not in payload:
        payload["stop"] = stop_sequences
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


def _anthropic_stop_reason(finish_reason: Any, *, has_tool_use: bool = False) -> str:
    if has_tool_use or finish_reason == "tool_calls":
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "content_filter":
        return "stop_sequence"
    if finish_reason in {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"}:
        return str(finish_reason)
    return "end_turn"


def _anthropic_usage_from_openai(response: Json) -> Json:
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
    }


def _ensure_anthropic_message_response(response: Json, *, fallback_model: str = "") -> Json:
    """Return an Anthropic Messages response shape strict clients accept."""
    if not isinstance(response, dict):
        response = {}
    normalized = dict(response)
    normalized.setdefault("id", f"msg_gateway_{uuid.uuid4().hex}")
    normalized.setdefault("type", "message")
    normalized.setdefault("role", "assistant")
    normalized.setdefault("model", fallback_model or str(response.get("model") or ""))
    normalized.setdefault("content", [])
    if not isinstance(normalized.get("content"), list):
        normalized["content"] = [{"type": "text", "text": str(normalized.get("content") or "")}]
    has_tool_use = any(isinstance(block, dict) and block.get("type") == "tool_use" for block in normalized.get("content") or [])
    normalized["stop_reason"] = _anthropic_stop_reason(normalized.get("stop_reason"), has_tool_use=has_tool_use)
    normalized.setdefault("stop_sequence", None)
    usage = normalized.get("usage")
    if not isinstance(usage, dict):
        normalized["usage"] = {"input_tokens": 0, "output_tokens": 0}
    else:
        normalized["usage"] = {
            "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        }
    return normalized


def _extract_think_blocks(text: str) -> tuple[list[str], str]:
    """Extract <think>...</think> blocks from text. Returns (think_texts, remaining_text)."""
    think_texts: list[str] = []
    remaining = text
    pattern = re.compile(r"<think>(.*?)</think>", re.DOTALL)
    for match in pattern.finditer(text):
        think_texts.append(match.group(1).strip())
    remaining = pattern.sub("", text).strip()
    return think_texts, remaining


def _from_openai_chat_response(path: str, response: Json) -> Json:
    if "/messages" not in path:
        return response
    choices = response.get("choices") or []
    if not choices:
        return _ensure_anthropic_message_response(
            {
                "id": response.get("id") or f"msg_gateway_{uuid.uuid4().hex}",
                "model": response.get("model") or "",
                "content": [],
                "usage": _anthropic_usage_from_openai(response),
            }
        )
    message = choices[0].get("message") or {}
    content_parts = []
    if message.get("reasoning"):
        content_parts.append({"type": "thinking", "thinking": message["reasoning"]})
    if message.get("content"):
        raw_text = message["content"]
        think_texts, remaining = _extract_think_blocks(raw_text)
        for think_text in think_texts:
            content_parts.append({"type": "thinking", "thinking": think_text})
        if remaining:
            content_parts.append({"type": "text", "text": remaining})
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
    has_tool_use = any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content_parts)
    result: Json = {
        "id": response.get("id") or f"msg_gateway_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": response.get("model") or "",
        "content": content_parts,
        "stop_reason": _anthropic_stop_reason(choices[0].get("finish_reason"), has_tool_use=has_tool_use),
        "stop_sequence": None,
        "usage": _anthropic_usage_from_openai(response),
    }
    return _ensure_anthropic_message_response(result, fallback_model=str(response.get("model") or ""))


def _last_user_text(path: str, body: Json) -> str:
    if "/responses" in path and "input" in body:
        raw_input = body.get("input")
        if isinstance(raw_input, str):
            return raw_input.strip()
        if isinstance(raw_input, list):
            for item in reversed(raw_input):
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        return text
                    continue
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                item_type = item.get("type")
                if role and role != "user":
                    continue
                if item_type in {"function_call_output", "custom_tool_call_output"}:
                    continue
                content = item.get("content")
                if content is None:
                    content = item.get("input") or item.get("text")
                text = _text_from_content(content).strip()
                if text and not text.startswith("<system-reminder>"):
                    return text
    messages = body.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for item in reversed(content):
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = str(item.get("text") or "").strip()
                        if text and not text.startswith("<system-reminder>"):
                            return text
                return _text_from_content(content)
            return _text_from_content(content)
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
            reasoning = msg.get("reasoning")
            if reasoning:
                content_parts.append({"type": "thinking", "thinking": str(reasoning)})
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
    # Merge consecutive same-role messages (Anthropic requires strict alternation)
    merged = []
    for msg in result:
        role = msg.get("role")
        if merged and merged[-1].get("role") == role:
            prev = merged[-1]
            prev_content = prev.get("content", "")
            curr_content = msg.get("content", "")
            if isinstance(prev_content, list) and isinstance(curr_content, list):
                prev["content"] = prev_content + curr_content
            elif isinstance(prev_content, str) and isinstance(curr_content, str):
                prev["content"] = prev_content + "\n" + curr_content
            elif isinstance(prev_content, list) and isinstance(curr_content, str):
                prev["content"] = prev_content + [{"type": "text", "text": curr_content}]
            elif isinstance(prev_content, str) and isinstance(curr_content, list):
                prev["content"] = [{"type": "text", "text": prev_content}] + curr_content
            continue
        merged.append(msg)
    return merged, system_text


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
    thinking_parts = []
    tool_calls = []
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                text_parts.append(str(item.get("text") or ""))
            elif item.get("type") == "thinking":
                thinking_parts.append(str(item.get("thinking") or ""))
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
    if thinking_parts:
        message["reasoning"] = "\n".join(thinking_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish_reason = response.get("stop_reason") or "stop"
    # Map Anthropic stop_reason to OpenAI finish_reason
    _anthropic_to_openai_finish = {
        "tool_use": "tool_calls",
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "pause_turn": "stop",
        "refusal": "stop",
    }
    finish_reason = _anthropic_to_openai_finish.get(finish_reason, finish_reason)
    result: Json = {
        "choices": [{"message": message, "finish_reason": finish_reason}],
    }
    # Map usage from Anthropic to OpenAI format
    anthropic_usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    if anthropic_usage:
        prompt_tokens = int(anthropic_usage.get("input_tokens") or 0)
        completion_tokens = int(anthropic_usage.get("output_tokens") or 0)
        result["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return result


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


def _responses_usage_from_openai(response: Json) -> Json:
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return {
        "input_tokens": int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
        "total_tokens": int(
            usage.get("total_tokens")
            or (int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0) + int(usage.get("output_tokens") or usage.get("completion_tokens") or 0))
        ),
    }


def _ensure_openai_responses_response(response: Json) -> Json:
    """Normalize Responses-like payloads to a strict OpenAI Responses shape."""
    normalized = copy.deepcopy(response) if isinstance(response, dict) else {}
    output = normalized.get("output")
    if not isinstance(output, list):
        output = []
    normalized["id"] = str(normalized.get("id") or f"resp_{uuid.uuid4().hex}")
    normalized["object"] = "response"
    normalized.setdefault("model", "")
    normalized["output"] = output
    normalized.setdefault("status", "completed")
    usage = normalized.get("usage")
    if not isinstance(usage, dict):
        normalized["usage"] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    else:
        input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
        normalized["usage"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
        }
    return normalized


def _from_openai_chat_to_responses_response(response: Json) -> Json:
    """Convert OpenAI Chat response to strict OpenAI Responses format."""
    choices = response.get("choices") or []
    if not choices:
        return _ensure_openai_responses_response({"model": response.get("model", ""), "output": [], "usage": _responses_usage_from_openai(response)})
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
    return _ensure_openai_responses_response({
        "id": response.get("id"),
        "model": response.get("model", ""),
        "output": output_items,
        "usage": _responses_usage_from_openai(response),
    })


# =============================================================================
# Unified conversion entry points
# =============================================================================

_GATEWAY_INTERNAL_REQUEST_FIELDS = {
    "workspace_root",
    "gateway_workspace",
    "project_dir",
    "projectDir",
    "cwd",
    "working_directory",
    "primary_working_directory",
    "worktree",
}


def _strip_gateway_internal_mapping_fields(value: Json) -> Json:
    sanitized = copy.deepcopy(value)
    for key in _GATEWAY_INTERNAL_REQUEST_FIELDS:
        sanitized.pop(key, None)
    for nested_key in ("user_id",):
        nested = sanitized.get(nested_key)
        if isinstance(nested, dict):
            nested_sanitized = _strip_gateway_internal_mapping_fields(nested)
            if nested_sanitized:
                sanitized[nested_key] = nested_sanitized
            else:
                sanitized.pop(nested_key, None)
        elif isinstance(nested, str):
            try:
                parsed = json.loads(nested)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                nested_sanitized = _strip_gateway_internal_mapping_fields(parsed)
                if nested_sanitized:
                    sanitized[nested_key] = json.dumps(nested_sanitized, ensure_ascii=False)
                else:
                    sanitized.pop(nested_key, None)
    return sanitized


def _metadata_string_contains_internal_routing(value: str) -> bool:
    lowered = value.lower()
    return any(
        token in lowered
        for token in (
            "workspace_root",
            "gateway_workspace",
            "projectdir",
            "project_dir",
            "working_directory",
            "primary working directory",
            "primary_working_directory",
            "worktree",
            "cwd",
        )
    )


def _strip_gateway_internal_request_fields(body: Json) -> Json:
    """Remove Gateway-only routing fields before forwarding to an upstream LLM."""
    sanitized = copy.deepcopy(body)
    for key in _GATEWAY_INTERNAL_REQUEST_FIELDS:
        sanitized.pop(key, None)
    metadata = sanitized.get("metadata")
    if isinstance(metadata, dict):
        sanitized_metadata = _strip_gateway_internal_mapping_fields(metadata)
        if sanitized_metadata:
            sanitized["metadata"] = sanitized_metadata
        else:
            sanitized.pop("metadata", None)
    elif isinstance(metadata, str):
        try:
            parsed_metadata = json.loads(metadata)
        except json.JSONDecodeError:
            parsed_metadata = None
        if isinstance(parsed_metadata, dict):
            sanitized_metadata = _strip_gateway_internal_mapping_fields(parsed_metadata)
            if sanitized_metadata:
                sanitized["metadata"] = sanitized_metadata
            else:
                sanitized.pop("metadata", None)
        elif _metadata_string_contains_internal_routing(metadata):
            sanitized.pop("metadata", None)
    return sanitized


def _convert_request_to_upstream(downstream_path: str, body: Json, upstream_protocol: str) -> tuple[str, Json]:
    """Convert downstream request to upstream format. Returns (upstream_path, converted_body)."""
    body = _strip_gateway_internal_request_fields(body)
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
    if response.get("object") == "response" and isinstance(output, list):
        return True
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
        return _ensure_anthropic_message_response(response)
    if "/responses" in downstream_path and _is_openai_responses_response(response):
        return _ensure_openai_responses_response(response)
    if "/chat/completions" in downstream_path and _is_openai_chat_response(response):
        return response

    # Convert based on upstream protocol
    if upstream_protocol == "anthropic_messages":
        if "/messages" in downstream_path:
            return response
        openai_chat = _from_anthropic_response_to_openai(response)
        if "/responses" in downstream_path:
            return _from_openai_chat_to_responses_response(openai_chat)
        return openai_chat
    if upstream_protocol == "openai_responses":
        if "/responses" in downstream_path:
            return _ensure_openai_responses_response(response)
        openai_chat = _from_responses_response_to_openai(response)
        if "/messages" in downstream_path:
            return _from_openai_chat_response(downstream_path, openai_chat)
        return openai_chat
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
    # Preserve gateway_context (e.g. local_planner) through protocol conversion
    if isinstance(body.get("gateway_context"), dict):
        payload["gateway_context"] = body["gateway_context"]
    return payload


# =============================================================================
# Tool degradation: inject tool definitions as text for non-tool APIs
# =============================================================================

_TOOL_FORMAT_TAG = "function"
_PARAM_TAG = "parameter"
_TEXT_TOOL_BLOCK_MAX_CHARS = 8000


def _tool_schema_name(tool: Json) -> str:
    if not isinstance(tool, dict):
        return ""
    func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    return str(func.get("name") or tool.get("name") or "").strip()


def _tool_schema_parameters(tool: Json) -> Json:
    if not isinstance(tool, dict):
        return {}
    func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    params = func.get("parameters") or func.get("input_schema") or tool.get("parameters") or tool.get("input_schema") or {}
    return params if isinstance(params, dict) else {}


def _compact_tool_schemas_for_text(tools: list[Json]) -> tuple[list[Json], int]:
    """Deduplicate tool schemas and keep only text-adapter-safe metadata."""
    compacted: list[Json] = []
    seen: set[str] = set()
    omitted = 0
    for tool in tools:
        if not isinstance(tool, dict):
            omitted += 1
            continue
        name = _tool_schema_name(tool)
        if not name:
            omitted += 1
            continue
        lowered = name.lower()
        if lowered in seen:
            omitted += 1
            continue
        seen.add(lowered)
        func = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        params = _tool_schema_parameters(tool)
        props = params.get("properties") if isinstance(params.get("properties"), dict) else {}
        required = [str(item) for item in (params.get("required") or []) if isinstance(item, str)]
        compacted.append(
            {
                "name": name,
                "description": re.sub(r"\s+", " ", str(func.get("description") or ""))[:180],
                "parameters": {
                    "properties": {str(key): value for key, value in list(props.items())[:8]},
                    "required": required[:8],
                },
            }
        )
    return compacted, omitted


def _forced_tool_name_from_choice(tool_choice: Any) -> str:
    if isinstance(tool_choice, dict):
        if isinstance(tool_choice.get("function"), dict) and tool_choice["function"].get("name"):
            return str(tool_choice["function"]["name"])
        if tool_choice.get("name"):
            return str(tool_choice["name"])
    return ""


def _build_tool_text_block(tools: list[Json], *, forced_tool_name: str = "") -> str:
    """Build a text block describing available tools for injection into system prompt."""
    compacted_tools, omitted = _compact_tool_schemas_for_text(tools)
    if forced_tool_name:
        compacted_tools.sort(key=lambda tool: 0 if str(tool.get("name") or "").lower() == forced_tool_name.lower() else 1)
    ft = _TOOL_FORMAT_TAG
    pt = _PARAM_TAG
    lines = [
        "=== Tool Call Gateway adapter ===",
        "",
        "You have access to real tools via this Gateway. When you need to use a tool,",
        "OUTPUT THE TOOL CALL DIRECTLY in one of the formats below. Do NOT describe what",
        "you would do - actually output the tool call so the Gateway can execute it.",
        "",
        "FORMAT 1 (XML - recommended):",
        f"<{ft}=ToolName>",
        f"<{pt}=param_name>value</{pt}>",
        f"</{ft}>",
        "",
        'FORMAT 2 (JSON):',
        '```json',
        '{"name": "ToolName", "arguments": {"param_name": "value"}}',
        '```',
        "",
        "FORMAT 3 (bare command for shell tools):",
        "ls -la",
        "",
        "You may emit multiple tool calls. After the Gateway returns real tool results, continue with the final answer.",
        "",
        "[Available tools]",
        "",
    ]
    if forced_tool_name:
        lines.extend([
            f"Forced tool_choice: you must call `{forced_tool_name}`.",
            "",
        ])
    used = 0
    for tool in compacted_tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        params = tool.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        param_bits = []
        if props:
            for pname, pinfo in props.items():
                ptype = (pinfo or {}).get("type", "string") if isinstance(pinfo, dict) else "string"
                req_str = "*" if pname in required else ""
                param_bits.append(f"{pname}{req_str}:{ptype}")
        rendered = f"- {name}({', '.join(param_bits)})"
        if desc:
            rendered += f": {desc}"
        candidate = "\n".join(lines + [rendered, ""])
        if len(candidate) > _TEXT_TOOL_BLOCK_MAX_CHARS:
            omitted += len(compacted_tools) - used
            break
        lines.append(rendered)
        lines.append("")
        used += 1
    if omitted:
        lines.append(f"... {omitted} duplicate/oversized tools omitted.")
    return "\n".join(lines)

def _build_tool_reminder(forced_tool_name: str = "") -> str:
    """Build a short reminder injected near the last user message."""
    forced = f" Required tool: `{forced_tool_name}`." if forced_tool_name else ""
    return (
        "\n\n[IMPORTANT: Tool Call Gateway adapter is active."
        f"{forced} If a tool is needed, output `<function=ToolName>` with "
        "`<parameter=name>value</parameter>` blocks only; the Gateway will execute it and return real results.]"
    )


def _append_text_adapter_reminder_to_responses_input(raw_input: Any, reminder: str) -> Any:
    if isinstance(raw_input, str):
        return raw_input + reminder
    if isinstance(raw_input, list):
        items = copy.deepcopy(raw_input)
        for i in range(len(items) - 1, -1, -1):
            item = items[i]
            if isinstance(item, str):
                items[i] = item + reminder
                return items
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role and role != "user":
                continue
            content = item.get("content")
            if isinstance(content, str):
                item["content"] = content + reminder
                return items
            if isinstance(content, list):
                item["content"] = list(content) + [{"type": "input_text", "text": reminder}]
                return items
            text = item.get("text") or item.get("input")
            if isinstance(text, str):
                key = "text" if "text" in item else "input"
                item[key] = text + reminder
                return items
        items.append({"role": "user", "content": reminder.strip()})
        return items
    return raw_input


def _inject_tools_as_text_prompt(body: Json, tools: list[Json], *, forced_tool_name: str = "") -> Json:
    """Inject tool definitions and format instructions into the system/user prompt.

    Strategy: prepend the full tool block to the system message AND append a
    short reminder to the last user message so the model sees tool instructions
    right before it needs to generate.
    """
    body = copy.deepcopy(body)
    if not tools:
        return body
    forced_tool_name = forced_tool_name or _forced_tool_name_from_choice(body.get("tool_choice"))
    tool_text = _build_tool_text_block(tools, forced_tool_name=forced_tool_name)
    reminder = _build_tool_reminder(forced_tool_name)

    if "input" in body and not body.get("messages"):
        existing = str(body.get("instructions") or "")
        body["instructions"] = tool_text + ("\n\n" + existing if existing else "")
        body["input"] = _append_text_adapter_reminder_to_responses_input(body.get("input"), reminder)
        return body

    messages = body.get("messages") or []
    if not messages:
        return body
    # Inject full tool text into system message (or first message)
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
    # Inject reminder into last user message so model sees it right before generating
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                msg["content"] = content + reminder
            elif isinstance(content, list):
                msg["content"] = list(content) + [{"type": "text", "text": reminder}]
            break
    body["messages"] = messages
    return body
