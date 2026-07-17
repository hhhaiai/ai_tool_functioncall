#!/usr/bin/env python3
"""Verify a remotely deployed Gateway from outside the server process.

The configured upstream may be a chat-only API with no native tool authority.
This smoke proves that the Gateway exposes real downstream protocol calls while
keeping user-machine tools on Codex/Claude Code and executing only
Gateway-owned tools in the service.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from typing import Any


def request_json(
    base_url: str,
    path: str,
    *,
    key: str = "",
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        headers=headers,
        method="GET" if body is None else "POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--model", default="weak-chat-only")
    args = parser.parse_args()

    status, health = request_json(args.base_url, "/healthz")
    assert status == 200 and health.get("ok") is True, (status, health)
    assert health.get("mode") == "orchestrate", health
    assert health.get("fake_prompt_tools") is False, health

    status, missing_auth = request_json(
        args.base_url,
        "/v1/tools/call",
        body={"tool": "calculator", "arguments": {"expression": "20+22"}},
    )
    assert status == 401, (status, missing_auth)

    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    prompt = "Call client_probe with value server-boundary."

    status, chat = request_json(
        args.base_url,
        "/v1/chat/completions",
        key=args.key,
        body={
            "model": args.model,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "client_probe",
                        "description": "Client-owned function",
                        "parameters": schema,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "client_probe"}},
        },
    )
    assert status == 200, (status, chat)
    chat_call = chat["choices"][0]["message"]["tool_calls"][0]
    assert chat_call["function"]["name"] == "client_probe", chat
    assert chat["choices"][0]["finish_reason"] == "tool_calls", chat

    status, responses = request_json(
        args.base_url,
        "/v1/responses",
        key=args.key,
        body={
            "model": args.model,
            "input": prompt,
            "tools": [
                {
                    "type": "function",
                    "name": "client_probe",
                    "description": "Client-owned function",
                    "parameters": schema,
                }
            ],
            "tool_choice": {"type": "function", "name": "client_probe"},
        },
    )
    assert status == 200, (status, responses)
    responses_call = next(item for item in responses["output"] if item.get("type") == "function_call")
    assert responses_call["name"] == "client_probe", responses

    status, messages = request_json(
        args.base_url,
        "/anthropic/v1/messages",
        key=args.key,
        body={
            "model": args.model,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": "client_probe",
                    "description": "Client-owned function",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": "client_probe"},
        },
    )
    assert status == 200, (status, messages)
    messages_call = next(item for item in messages["content"] if item.get("type") == "tool_use")
    assert messages_call["name"] == "client_probe", messages
    assert messages["stop_reason"] == "tool_use", messages

    status, calculator = request_json(
        args.base_url,
        "/v1/tools/call",
        key=args.key,
        body={"tool": "calculator", "arguments": {"expression": "20+22"}},
    )
    assert status == 200 and calculator.get("success") is True, (status, calculator)
    assert str(calculator.get("content")) == "42", calculator

    status, read = request_json(
        args.base_url,
        "/v1/tools/call",
        key=args.key,
        body={"tool": "Read", "arguments": {"file_path": "server-secret.txt"}},
    )
    assert status == 400, (status, read)
    detail = (read.get("error") or {}).get("detail") or {}
    assert detail.get("failure_type") == "direct_user_side_tool_requires_downstream_client", read

    print(
        json.dumps(
            {
                "ok": True,
                "base_url": args.base_url,
                "health": "orchestrate",
                "unauthenticated_request_status": 401,
                "chat_tool_calls": chat_call["function"]["name"],
                "responses_function_call": responses_call["name"],
                "anthropic_tool_use": messages_call["name"],
                "gateway_owned_calculator": 42,
                "user_side_read_status": 400,
                "user_side_read_owner": "downstream_client",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
