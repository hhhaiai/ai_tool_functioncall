#!/usr/bin/env python3
"""Canonical bounded state for upstream SSE response aggregation."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import Any


Json = dict[str, Any]


@dataclass(frozen=True)
class StreamDelta:
    kind: str
    text: str
    index: int = 0
    content_index: int = 0


class UpstreamResponseAccumulator:
    """Build a normal provider response while exposing incremental deltas."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.response_id = ""
        self.model = ""
        self.created_at = int(time.time())
        self.usage: Json = {}
        self.stop_reason: str | None = None
        self._chat_choices: dict[int, Json] = {}
        self._anthropic_blocks: dict[int, Json] = {}
        self._responses_items: dict[int, Json] = {}
        self._completed_response: Json | None = None
        self.total_events = 0

    def metadata(self) -> Json:
        return {
            "id": self.response_id,
            "model": self.model,
            "created_at": self.created_at,
            "usage": dict(self.usage),
            "stop_reason": self.stop_reason,
        }

    @staticmethod
    def _payload(event: str | None, data: str) -> Json | None:
        if not data or data == "[DONE]":
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def feed(self, event: str | None, data: str) -> list[StreamDelta]:
        self.total_events += 1
        payload = self._payload(event, data)
        if payload is None:
            return []
        if isinstance(payload.get("response"), dict) and str(payload.get("type") or event) == "response.completed":
            self._completed_response = dict(payload["response"])
        if "choices" in payload:
            return self._feed_chat(payload)
        payload_type = str(payload.get("type") or event or "")
        if payload.get("object") == "response" and isinstance(payload.get("output"), list):
            self._completed_response = dict(payload)
            return []
        if payload_type == "message" and isinstance(payload.get("content"), list):
            self._completed_response = dict(payload)
            return []
        if payload_type.startswith("response.") or "/responses" in self.path:
            return self._feed_responses(payload_type, payload)
        if payload_type.startswith(("message_", "content_block_")) or "/messages" in self.path:
            return self._feed_anthropic(payload_type, payload)
        # Some adapters return a complete non-streaming JSON response despite
        # stream=true. Keep it as the authoritative final object.
        self._completed_response = dict(payload)
        return []

    def _feed_chat(self, payload: Json) -> list[StreamDelta]:
        self.response_id = str(payload.get("id") or self.response_id or f"chatcmpl_{uuid.uuid4().hex}")
        self.model = str(payload.get("model") or self.model)
        if isinstance(payload.get("usage"), dict):
            self.usage = dict(payload["usage"])
        deltas: list[StreamDelta] = []
        for choice_payload in payload.get("choices") or []:
            if not isinstance(choice_payload, dict):
                continue
            index = int(choice_payload.get("index") or 0)
            state = self._chat_choices.setdefault(
                index,
                {"role": "assistant", "content": [], "reasoning": [], "tool_calls": {}, "finish_reason": None},
            )
            message = choice_payload.get("message") if isinstance(choice_payload.get("message"), dict) else None
            if message is not None:
                if message.get("role"):
                    state["role"] = message["role"]
                if isinstance(message.get("content"), str):
                    state["content"].append(message["content"])
                reasoning = message.get("reasoning") or message.get("reasoning_content")
                if isinstance(reasoning, str):
                    state["reasoning"].append(reasoning)
                for call_index, raw_call in enumerate(message.get("tool_calls") or []):
                    if isinstance(raw_call, dict):
                        state["tool_calls"][call_index] = dict(raw_call)
            delta = choice_payload.get("delta") if isinstance(choice_payload.get("delta"), dict) else {}
            if delta.get("role"):
                state["role"] = delta["role"]
            if isinstance(delta.get("content"), str):
                state["content"].append(delta["content"])
                deltas.append(StreamDelta("text", delta["content"], index))
            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
            if isinstance(reasoning, str):
                state["reasoning"].append(reasoning)
                deltas.append(StreamDelta("reasoning", reasoning, index))
            for raw_call in delta.get("tool_calls") or []:
                if not isinstance(raw_call, dict):
                    continue
                call_index = int(raw_call.get("index") or 0)
                call = state["tool_calls"].setdefault(
                    call_index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if raw_call.get("id"):
                    call["id"] = raw_call["id"]
                if raw_call.get("type"):
                    call["type"] = raw_call["type"]
                fn = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
                if fn.get("name"):
                    call["function"]["name"] += str(fn["name"])
                if fn.get("arguments"):
                    call["function"]["arguments"] += str(fn["arguments"])
            legacy = delta.get("function_call") if isinstance(delta.get("function_call"), dict) else None
            if legacy is not None:
                call = state["tool_calls"].setdefault(
                    0,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                call["function"]["name"] += str(legacy.get("name") or "")
                call["function"]["arguments"] += str(legacy.get("arguments") or "")
            if choice_payload.get("finish_reason") is not None:
                state["finish_reason"] = choice_payload.get("finish_reason")
        return deltas

    def _feed_anthropic(self, payload_type: str, payload: Json) -> list[StreamDelta]:
        deltas: list[StreamDelta] = []
        if payload_type == "message_start":
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            self.response_id = str(message.get("id") or self.response_id or f"msg_{uuid.uuid4().hex}")
            self.model = str(message.get("model") or self.model)
            if isinstance(message.get("usage"), dict):
                self.usage.update(message["usage"])
        elif payload_type == "content_block_start":
            index = int(payload.get("index") or 0)
            block = payload.get("content_block") if isinstance(payload.get("content_block"), dict) else {}
            stored = dict(block)
            if stored.get("type") == "text":
                initial = str(stored.get("text") or "")
                stored["text"] = initial
                if initial:
                    deltas.append(StreamDelta("text", initial, index))
            elif stored.get("type") == "thinking":
                initial = str(stored.get("thinking") or "")
                stored["thinking"] = initial
                if initial:
                    deltas.append(StreamDelta("reasoning", initial, index))
            elif stored.get("type") == "tool_use":
                stored["_partial_json"] = ""
            self._anthropic_blocks[index] = stored
        elif payload_type == "content_block_delta":
            index = int(payload.get("index") or 0)
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
            block = self._anthropic_blocks.setdefault(index, {"type": "text", "text": ""})
            if delta.get("type") == "text_delta":
                text = str(delta.get("text") or "")
                block["text"] = str(block.get("text") or "") + text
                if text:
                    deltas.append(StreamDelta("text", text, index))
            elif delta.get("type") == "thinking_delta":
                text = str(delta.get("thinking") or "")
                block["thinking"] = str(block.get("thinking") or "") + text
                if text:
                    deltas.append(StreamDelta("reasoning", text, index))
            elif delta.get("type") == "input_json_delta":
                block["_partial_json"] = str(block.get("_partial_json") or "") + str(delta.get("partial_json") or "")
        elif payload_type == "message_delta":
            delta = payload.get("delta") if isinstance(payload.get("delta"), dict) else {}
            self.stop_reason = str(delta.get("stop_reason") or self.stop_reason or "end_turn")
            if isinstance(payload.get("usage"), dict):
                self.usage.update(payload["usage"])
        return deltas

    def _responses_message(self, output_index: int, item_id: str = "") -> Json:
        item = self._responses_items.get(output_index)
        if not isinstance(item, dict) or item.get("type") != "message":
            item = {
                "id": item_id or f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            self._responses_items[output_index] = item
        return item

    @staticmethod
    def _responses_content(item: Json, content_index: int) -> Json:
        content = item.setdefault("content", [])
        while len(content) <= content_index:
            content.append({"type": "output_text", "text": "", "annotations": []})
        if not isinstance(content[content_index], dict):
            content[content_index] = {"type": "output_text", "text": "", "annotations": []}
        return content[content_index]

    def _feed_responses(self, payload_type: str, payload: Json) -> list[StreamDelta]:
        deltas: list[StreamDelta] = []
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        if response:
            self.response_id = str(response.get("id") or self.response_id or f"resp_{uuid.uuid4().hex}")
            self.model = str(response.get("model") or self.model)
            self.created_at = int(response.get("created_at") or self.created_at)
            if isinstance(response.get("usage"), dict):
                self.usage = dict(response["usage"])
        output_index = int(payload.get("output_index") or 0)
        if payload_type == "response.output_item.added":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            self._responses_items[output_index] = dict(item)
        elif payload_type == "response.content_part.added":
            item = self._responses_message(output_index, str(payload.get("item_id") or ""))
            content_index = int(payload.get("content_index") or 0)
            part = payload.get("part") if isinstance(payload.get("part"), dict) else {}
            content = item.setdefault("content", [])
            while len(content) <= content_index:
                content.append({"type": "output_text", "text": "", "annotations": []})
            content[content_index] = dict(part)
        elif payload_type == "response.output_text.delta":
            content_index = int(payload.get("content_index") or 0)
            item = self._responses_message(output_index, str(payload.get("item_id") or ""))
            part = self._responses_content(item, content_index)
            text = str(payload.get("delta") or "")
            part["text"] = str(part.get("text") or "") + text
            if text:
                deltas.append(StreamDelta("text", text, output_index, content_index))
        elif payload_type == "response.function_call_arguments.delta":
            item = self._responses_items.setdefault(
                output_index,
                {"id": f"fc_{uuid.uuid4().hex}", "type": "function_call", "call_id": "", "name": "", "arguments": ""},
            )
            item["arguments"] = str(item.get("arguments") or "") + str(payload.get("delta") or "")
        elif payload_type == "response.output_item.done":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            self._responses_items[output_index] = dict(item)
        elif payload_type == "response.completed" and response:
            self._completed_response = dict(response)
        return deltas

    def finalize(self) -> Json:
        if self._completed_response is not None:
            return dict(self._completed_response)
        if self._chat_choices:
            choices = []
            for index, state in sorted(self._chat_choices.items()):
                tool_calls = [call for _, call in sorted(state["tool_calls"].items())]
                content = "".join(state["content"])
                message: Json = {"role": state["role"], "content": content if content else None}
                reasoning = "".join(state["reasoning"])
                if reasoning:
                    message["reasoning"] = reasoning
                if tool_calls:
                    message["tool_calls"] = tool_calls
                choices.append(
                    {
                        "index": index,
                        "message": message,
                        "finish_reason": state["finish_reason"] or ("tool_calls" if tool_calls else "stop"),
                    }
                )
            return {
                "id": self.response_id or f"chatcmpl_{uuid.uuid4().hex}",
                "object": "chat.completion",
                "model": self.model,
                "choices": choices,
                "usage": dict(self.usage),
            }
        if self._anthropic_blocks or "/messages" in self.path:
            content = []
            for _, block in sorted(self._anthropic_blocks.items()):
                item = {key: value for key, value in block.items() if not key.startswith("_")}
                if item.get("type") == "tool_use":
                    partial = str(block.get("_partial_json") or "")
                    if partial:
                        try:
                            item["input"] = json.loads(partial)
                        except json.JSONDecodeError:
                            item["input"] = {}
                    else:
                        item["input"] = item.get("input") if isinstance(item.get("input"), dict) else {}
                content.append(item)
            return {
                "id": self.response_id or f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": content,
                "stop_reason": self.stop_reason or ("tool_use" if any(i.get("type") == "tool_use" for i in content) else "end_turn"),
                "usage": dict(self.usage),
            }
        output = [item for _, item in sorted(self._responses_items.items())]
        return {
            "id": self.response_id or f"resp_{uuid.uuid4().hex}",
            "object": "response",
            "created_at": self.created_at,
            "status": "completed",
            "model": self.model,
            "output": output,
            "usage": dict(self.usage),
        }


__all__ = ["StreamDelta", "UpstreamResponseAccumulator"]
