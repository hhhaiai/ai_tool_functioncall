#!/usr/bin/env python3
"""Core acceptance: prove real tools work, not just API endpoints.

This is the primary acceptance suite for the Gateway. It verifies that tool
calls execute real local behavior and that model orchestration can consume
native tool calls from an upstream and return a final answer.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> Any:
    proc = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    text = proc.stdout.strip()
    if not text:
        return None
    return json.loads(text)


class ToolCallingUpstream(BaseHTTPRequestHandler):
    calls: list[dict[str, Any]] = []

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802
        return

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))).decode("utf-8"))
        ToolCallingUpstream.calls.append({"path": self.path, "body": body})
        if len(ToolCallingUpstream.calls) == 1:
            payload = {
                "id": "chatcmpl_tool_acceptance_1",
                "object": "chat.completion",
                "model": body.get("model") or "tool-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_calc",
                                    "type": "function",
                                    "function": {"name": "calculator", "arguments": json.dumps({"expression": "20+22"})},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        else:
            payload = {
                "id": "chatcmpl_tool_acceptance_2",
                "object": "chat.completion",
                "model": body.get("model") or "tool-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "工具执行成功，calculator 返回 42"},
                        "finish_reason": "stop",
                    }
                ],
            }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def post_json(url: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"authorization": f"Bearer {key}", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8885")
    parser.add_argument("--key", default="local-gateway-key")
    args = parser.parse_args()

    checks: list[str] = []

    # Direct tool runtime acceptance: many real local tools execute, mutate files,
    # run code, fetch local HTTP, inspect images, run parallel calls, and log to SQLite.
    smoke = run([sys.executable, "tests/integration/smoke_gateway_tools.py", "--base-url", args.base_url, "--key", args.key])
    required = {
        "project_tree",
        "project_glob",
        "python_symbols",
        "write_edit_read",
        "coding_bash",
        "code_interpreter",
        "web_fetch",
        "web_search",
        "vision_image",
        "intent_detect",
        "parallel_tools",
        "arbitrary_project_analyze_modify_run",
        "sqlite_only_logging",
    }
    actual = set(smoke.get("checks") or [])
    missing = sorted(required - actual)
    if missing:
        raise AssertionError(f"tool smoke missing checks: {missing}")
    checks.append("direct_tool_runtime_real_tools")

    # Orchestration acceptance: upstream emits native tool_calls; gateway executes
    # calculator locally, appends tool result, calls upstream again, and returns final answer.
    ToolCallingUpstream.calls = []
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ToolCallingUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            env = {
                **dict(__import__("os").environ),
                "GATEWAY_CONFIG_PATH": str(pathlib.Path(td) / "config.json"),
                "GATEWAY_SQLITE_LOG_PATH": str(pathlib.Path(td) / "gateway.sqlite3"),
                "UPSTREAM_BASE_URL": f"http://127.0.0.1:{upstream.server_address[1]}",
                "UPSTREAM_API_KEY": "test-upstream-key",
                "UPSTREAM_MODEL": "tool-test",
                "DOWNSTREAM_API_KEY": "tool-acceptance-key",
                "GATEWAY_TOOLS_ENABLED": "on",
                "UPSTREAM_SUPPORTS_TOOLS": "1",
                "UPSTREAM_SUPPORTS_FUNCTION_CALLS": "1",
                "GATEWAY_ALLOW_WRITE_TOOLS": "1",
                "GATEWAY_ALLOW_SHELL_TOOLS": "1",
                "GATEWAY_CONTEXT_ENABLED": "0",
                "GATEWAY_CONTEXT_FANOUT_ENABLED": "0",
                "GATEWAY_UPSTREAM_STREAM_AGGREGATE": "0",
            }
            port = "8897"
            env["GATEWAY_PORT"] = port
            env["GATEWAY_START_METHOD"] = "nohup"
            subprocess.run([str(ROOT / "scripts/mimo_gateway.sh"), "stop"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            start = subprocess.run([str(ROOT / "scripts/mimo_gateway.sh"), "start"], cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
            if start.returncode != 0:
                raise AssertionError(f"start tool acceptance gateway failed\n{start.stdout}\n{start.stderr}")
            try:
                response = post_json(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    "tool-acceptance-key",
                    {"model": "tool-test", "messages": [{"role": "user", "content": "请用 calculator 计算 20+22"}]},
                )
                text = response["choices"][0]["message"]["content"]
                if "42" not in text:
                    raise AssertionError(f"final answer does not include tool result: {response}")
                if len(ToolCallingUpstream.calls) != 2:
                    raise AssertionError(f"expected 2 upstream calls, got {len(ToolCallingUpstream.calls)}")
                second_body = ToolCallingUpstream.calls[1]["body"]
                tool_messages = [m for m in second_body.get("messages", []) if m.get("role") == "tool"]
                if not tool_messages or "42" not in json.dumps(tool_messages, ensure_ascii=False):
                    raise AssertionError(f"tool result was not appended to upstream request: {second_body}")
                checks.append("native_tool_call_orchestration_executes_real_tool")
            finally:
                subprocess.run([str(ROOT / "scripts/mimo_gateway.sh"), "stop"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=2)

    print(json.dumps({"ok": True, "acceptance": "tools", "checks": checks}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "acceptance": "tools", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise
