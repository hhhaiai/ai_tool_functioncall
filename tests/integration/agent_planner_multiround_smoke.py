#!/usr/bin/env python3
"""Self-contained multi-round smoke for Gateway Agent Planner.

This does not use real upstream credentials. It simulates a downstream client
that executes native tool_use blocks and a chat-only upstream that only emits
text/JSON. The smoke proves the safe remote Agent Planner loop:

  run tests -> failure -> diagnostic Read -> upstream JSON Edit attempt
  -> JSON is returned as text only and is recorded as ignored

The final Edit/verification loop is only allowed when the outer planner or the
downstream client produces an authorized Edit tool_use; chat-only upstream text
does not get tool authority.
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import threading
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.toolcall_gateway as gateway
from src.gateway_builtin_tools import ToolCall
from src.gateway_agent_planner import list_runtime_events
from src.gateway_tool_runtime import _execute_tool_call, run_tool_orchestration


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


def _tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": "Bash",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "timeout": {"type": "number"}},
                "required": ["command"],
                "additionalProperties": False,
            },
        },
        {
            "name": "Read",
            "input_schema": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "Edit",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["file_path", "old_string", "new_string"],
                "additionalProperties": False,
            },
        },
    ]


def _chat_response(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl_fake",
        "object": "chat.completion",
        "model": "weak-chat-only",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _execute_tool_use(block: dict[str, Any]) -> dict[str, Any]:
    call = ToolCall(str(block["id"]), str(block["name"]), dict(block.get("input") or {}), {})
    result = _execute_tool_call(call, provider="agent_planner_multiround_smoke")
    return {
        "type": "tool_result",
        "tool_use_id": call.call_id,
        "content": result.content,
        "is_error": not result.success,
    }


def run_smoke() -> dict[str, Any]:
    old_config = gateway.CONFIG_PATH
    old_cwd = os.getcwd()
    old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
    with tempfile.TemporaryDirectory(prefix="agent-planner-smoke-") as td:
        root = pathlib.Path(td)
        (root / "src").mkdir()
        (root / "tests").mkdir()
        (root / "src" / "app.py").write_text("def ok():\n    return False\n", encoding="utf-8")
        (root / "tests" / "test_app.py").write_text(
            "from src.app import ok\n\ndef test_ok():\n    assert ok() is True\n",
            encoding="utf-8",
        )
        (root / "src" / "__init__.py").write_text("", encoding="utf-8")

        gateway.CONFIG_PATH = root / "gateway.config.json"
        cfg = gateway._default_config()
        cfg["gateway"]["workspace_root"] = str(root)
        cfg["gateway"]["tool_mode"] = "orchestrate"
        cfg["gateway"]["allow_shell_tools"] = True
        cfg["gateway"]["allow_write_tools"] = True
        cfg["upstream"]["tools_enabled"] = "adapter"
        cfg["upstream"]["protocol"] = "openai_chat"
        cfg["upstream"]["capabilities"]["supports_tools"] = False
        cfg["upstream"]["capabilities"]["supports_function_calls"] = False
        gateway.save_config(cfg)
        os.chdir(root)
        os.environ["GATEWAY_WORKSPACE_ROOT"] = str(root)
        os.environ["GATEWAY_RUNTIME_DIR"] = str(root / ".gateway_runtime")
        import src.gateway_agent_planner as planner
        planner._STORE = None

        patch_json = """```json
{"name":"Edit","arguments":{"file_path":"src/app.py","old_string":"return False","new_string":"return True"}}
```"""
        client = FakeClient([_chat_response(patch_json)])
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": "运行测试并修复",
            }
        ]
        steps: list[str] = []
        final_response: dict[str, Any] | None = None

        for _ in range(8):
            body = {"model": "weak-chat-only", "messages": messages, "tools": _tool_schemas(), "max_tokens": 4096}
            response = run_tool_orchestration("/v1/messages", body, client)
            if response.get("stop_reason") != "tool_use":
                final_response = response
                break
            tool_uses = [b for b in response.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
            if not tool_uses:
                raise AssertionError(f"tool_use stop without tool_use blocks: {response}")
            messages.append({"role": "assistant", "content": tool_uses})
            results = []
            for block in tool_uses:
                steps.append(str(block.get("name")))
                results.append(_execute_tool_use(block))
            messages.append({"role": "user", "content": results})
        else:
            raise AssertionError("planner loop did not terminate")

        if final_response is None:
            raise AssertionError("no final response")
        final_payload = json.dumps(final_response, ensure_ascii=False)
        source_after = (root / "src" / "app.py").read_text(encoding="utf-8")
        if source_after != "def ok():\n    return False\n":
            raise AssertionError("chat-only upstream JSON patch must not edit src/app.py")
        if not steps or steps[0] != "Bash" or "Read" not in steps or "Edit" in steps:
            raise AssertionError(f"unexpected planner steps: {steps!r}")
        if "Edit" not in final_payload:
            raise AssertionError(f"ignored patch JSON missing from final text: {final_payload[:1000]}")
        runtime_events = list_runtime_events(10, event_type="upstream_tool_attempt_ignored")
        if not runtime_events:
            raise AssertionError("ignored upstream patch attempt was not recorded")
        if runtime_events[0]["metadata"].get("tool_authority_granted") is not False:
            raise AssertionError("ignored upstream patch event must deny tool authority")
        if len(client.requests) != 1:
            raise AssertionError(f"expected exactly one chat-only upstream call, got {len(client.requests)}")
        first_prompt = json.dumps(client.requests[0][1], ensure_ascii=False)
        if "Gateway Agent Planner evidence" not in first_prompt or "src/app.py" not in first_prompt:
            raise AssertionError("patch synthesis prompt did not include diagnostic evidence: " + first_prompt[:1200])
        return {
            "ok": True,
            "workspace": str(root),
            "steps": steps,
            "upstream_calls": len(client.requests),
            "ignored_upstream_tool_attempt": runtime_events[0]["metadata"].get("calls", [{}])[0].get("name"),
        }
    # unreachable while tempdir context owns workspace


def main() -> int:
    old_config = gateway.CONFIG_PATH
    old_cwd = os.getcwd()
    old_ws = os.environ.get("GATEWAY_WORKSPACE_ROOT")
    old_runtime = os.environ.get("GATEWAY_RUNTIME_DIR")
    try:
        result = run_smoke()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        gateway.CONFIG_PATH = old_config
        os.chdir(old_cwd)
        if old_ws is None:
            os.environ.pop("GATEWAY_WORKSPACE_ROOT", None)
        else:
            os.environ["GATEWAY_WORKSPACE_ROOT"] = old_ws
        if old_runtime is None:
            os.environ.pop("GATEWAY_RUNTIME_DIR", None)
        else:
            os.environ["GATEWAY_RUNTIME_DIR"] = old_runtime


if __name__ == "__main__":
    raise SystemExit(main())
