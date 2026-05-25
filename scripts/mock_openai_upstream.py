#!/usr/bin/env python3
"""Tiny local OpenAI-compatible upstream for Gateway smoke tests.

This intentionally mirrors the conservative behavior of a weak/partial tools
upstream:
- /v1/chat/completions, /v1/responses, /v1/messages and /v1/models work.
- /anthropic/* and direct /v1/tools/call or /v1/functions/call return 404.
- Anthropic /v1/messages can emit tool_use when tool_choice is forced.
- Responses does not emit function_call by default, so Gateway adapter mode is
  still the stable Claude Code + Codex path.

Run:
  python3 scripts/mock_openai_upstream.py --port 9001 --model mimo-v2.5-pro
Then configure the Gateway with:
  UPSTREAM_BASE_URL=http://127.0.0.1:9001
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _request_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _text_from_messages(body: dict[str, Any]) -> str:
    messages = body.get("messages") or body.get("input") or []
    if isinstance(messages, str):
        return messages
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text") or block.get("input") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
    return "\n".join(parts)


class MockUpstreamHandler(BaseHTTPRequestHandler):
    server_version = "MockOpenAIUpstream/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    @property
    def model(self) -> str:
        return str(getattr(self.server, "model", "mimo-v2.5-pro"))

    def _read_json(self) -> dict[str, Any]:
        raw = self.rfile.read(int(self.headers.get("content-length", "0") or "0"))
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_not_found(self) -> None:
        self._send_json(404, {"error": {"message": "not found", "type": "mock_not_found"}})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(200, {"ok": True, "model": self.model})
            return
        if path == "/v1/models":
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": self.model, "object": "model"},
                        {"id": self.model.replace("-pro", ""), "object": "model"},
                    ],
                },
            )
            return
        self._send_not_found()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/anthropic/") or path in {"/v1/tools/call", "/v1/functions/call", "/tools/call"}:
            self._send_not_found()
            return

        body = self._read_json()
        prompt = _text_from_messages(body)
        answer = "mock upstream ok" if not prompt else f"mock upstream ok: {prompt[:80]}"

        if path == "/v1/chat/completions":
            self._send_json(
                200,
                {
                    "id": _request_id("chatcmpl_mock"),
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body.get("model") or self.model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": answer},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )
            return

        if path == "/v1/responses":
            self._send_json(
                200,
                {
                    "id": _request_id("resp_mock"),
                    "object": "response",
                    "created_at": int(time.time()),
                    "model": body.get("model") or self.model,
                    "status": "completed",
                    "output": [
                        {
                            "id": _request_id("msg_mock"),
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": answer}],
                        }
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            )
            return

        if path == "/v1/messages":
            tool_choice = body.get("tool_choice")
            forced = isinstance(tool_choice, dict) and tool_choice.get("type") in {"tool", "function"}
            tools = body.get("tools") if isinstance(body.get("tools"), list) else []
            if forced and tools:
                requested = str(tool_choice.get("name") or (tools[0] if isinstance(tools[0], dict) else {}).get("name") or "echo_probe")
                content = [{"type": "tool_use", "id": _request_id("toolu_mock"), "name": requested, "input": {"text": "echo"}}]
                stop_reason = "tool_use"
            else:
                content = [{"type": "text", "text": answer}]
                stop_reason = "end_turn"
            self._send_json(
                200,
                {
                    "id": _request_id("msg_mock"),
                    "type": "message",
                    "role": "assistant",
                    "model": body.get("model") or self.model,
                    "content": content,
                    "stop_reason": stop_reason,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
            return

        self._send_not_found()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--model", default="mimo-v2.5-pro")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), MockUpstreamHandler)
    httpd.model = args.model  # type: ignore[attr-defined]
    httpd.quiet = args.quiet  # type: ignore[attr-defined]
    print(f"mock upstream listening on http://{args.host}:{args.port} model={args.model}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
