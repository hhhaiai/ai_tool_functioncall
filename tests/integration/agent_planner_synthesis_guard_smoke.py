#!/usr/bin/env python3
"""Final-synthesis guard smoke for the Gateway Agent Planner.

This smoke proves a weak/chat-only upstream cannot override planner-owned
evidence at the final synthesis boundary with refusal text, stale
session/workspace drift, or a non-answer placeholder.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.toolcall_gateway as gateway
from src.gateway_tool_runtime import run_tool_orchestration


class FakeClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.lock = threading.Lock()

    def forward(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.requests.append((path, body))
            if not self.responses:
                raise AssertionError("no fake upstream response left")
            return self.responses.pop(0)


def _chat_response(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl_synthesis_guard_fake",
        "object": "chat.completion",
        "model": "weak-chat-only",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _tool_schema() -> list[dict[str, Any]]:
    return [{
        "name": "Bash",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    }]


def _body(case: str) -> dict[str, Any]:
    return {
        "model": "weak-chat-only",
        "metadata": {"tenant": f"synthesis-guard-{case}", "session_id": f"{case}-session"},
        "messages": [
            {"role": "user", "content": "分析这套项目"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"{case}_bash_1", "name": "Bash", "input": {"command": "find . -maxdepth 2"}},
            ]},
            {"role": "user", "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"{case}_bash_1",
                    "content": "--- files ---\nREADME.md\nsrc/gateway_agent_planner.py\nPROJECT-SYNTHESIS-GUARD-OK",
                },
            ]},
        ],
        "tools": _tool_schema(),
        "max_tokens": 4096,
    }


def _assert_guarded(case: str, bad_text: str, expected_flag: str) -> dict[str, Any]:
    client = FakeClient([_chat_response(bad_text)])
    result = run_tool_orchestration("/v1/messages", _body(case), client)
    content = result.get("content") or []
    text = "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    meta = result.get("gateway_agent_planner") if isinstance(result.get("gateway_agent_planner"), dict) else {}
    if bad_text in text:
        raise AssertionError(f"{case}: bad upstream text leaked: {text[:1000]}")
    if "PROJECT-SYNTHESIS-GUARD-OK" not in text:
        raise AssertionError(f"{case}: fallback did not include planner evidence: {text[:1000]}")
    if not meta.get(expected_flag):
        raise AssertionError(f"{case}: missing expected {expected_flag} metadata: {meta}")
    if len(client.requests) != 1:
        raise AssertionError(f"{case}: expected exactly one upstream synthesis call, got {len(client.requests)}")
    return {
        "case": case,
        "expected_flag": expected_flag,
        "metadata": meta,
        "fallback_text_prefix": text[:160],
    }


def run_smoke() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agent-planner-synthesis-guard-") as td:
        root = pathlib.Path(td)
        (root / "README.md").write_text("# GuardSmoke\n", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "gateway_agent_planner.py").write_text("# planner\n", encoding="utf-8")

        gateway.CONFIG_PATH = root / "gateway.config.json"
        cfg = gateway._default_config()
        cfg["gateway"]["workspace_root"] = str(root)
        cfg["gateway"]["tool_mode"] = "orchestrate"
        cfg["upstream"]["tools_enabled"] = "adapter"
        cfg["upstream"]["protocol"] = "openai_chat"
        cfg["upstream"]["capabilities"]["supports_tools"] = False
        cfg["upstream"]["capabilities"]["supports_function_calls"] = False
        gateway.save_config(cfg)
        os.chdir(root)
        os.environ["GATEWAY_WORKSPACE_ROOT"] = str(root)

        cases = [
            (
                "refusal",
                "Hello, I can't answer this question for now. Let's talk about something else.",
                "synthesis_refusal_fallback",
            ),
            (
                "scope",
                "根据上一个 session 的记录，正确的路径是 /Users/sanbo/Desktop/old-project。",
                "synthesis_scope_fallback",
            ),
            (
                "nonanswer",
                "Let me first see what's actually in that directory.",
                "synthesis_nonanswer_fallback",
            ),
        ]
        results = [_assert_guarded(case, bad_text, flag) for case, bad_text, flag in cases]
        return {"ok": True, "workspace": str(root), "cases": results}


def main() -> int:
    old_config = gateway.CONFIG_PATH
    old_cwd = os.getcwd()
    old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    try:
        print(json.dumps(run_smoke(), ensure_ascii=False, indent=2))
        return 0
    finally:
        gateway.CONFIG_PATH = old_config
        os.chdir(old_cwd)
        if old_ws is None:
            os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
        else:
            os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws


if __name__ == "__main__":
    raise SystemExit(main())
